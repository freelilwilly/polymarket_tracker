"""Trade monitoring for selected traders."""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from api_client import PolymarketAPIClient
from config import Config
from utils import trade_key

logger = logging.getLogger(__name__)


class TradeMonitor:
    """Monitors traders for new trades."""
    
    def __init__(self, api_client: PolymarketAPIClient, slug_converter=None):
        """Initialize trade monitor."""
        self.api_client = api_client
        self.slug_converter = slug_converter
        self.wallet_labels: dict[str, str] = {}
        
        # Track seen trades per wallet to avoid duplicates
        self.seen_trades: dict[str, set[str]] = defaultdict(set)
        
        # Track trade size history per wallet for normalization
        self.size_history: dict[str, list[float]] = defaultdict(list)

        # Adaptive fetch depth during duplicate storms.
        self.fetch_pages_by_wallet: dict[str, int] = defaultdict(
            lambda: max(1, Config.TRADE_MAX_PAGES_PER_POLL)
        )

        # Wallet-level checkpoint per market+side for observability and gap detection.
        self.market_side_checkpoint: dict[str, dict[str, datetime]] = defaultdict(dict)

        # Wallets currently in startup bootstrap mode.
        self.bootstrap_wallets: set[str] = set()

    @staticmethod
    def _short_wallet(wallet: str) -> str:
        value = str(wallet or "").strip().lower()
        return f"{value[:8]}..." if value else "UNKNOWN_TRADER"

    def set_wallet_label(self, wallet: str, label: str) -> None:
        wallet_key = str(wallet or "").strip().lower()
        display = str(label or "").strip()
        if wallet_key and display:
            self.wallet_labels[wallet_key] = display

    def _wallet_label(self, wallet: str) -> str:
        wallet_key = str(wallet or "").strip().lower()
        return self.wallet_labels.get(wallet_key) or self._short_wallet(wallet_key)

    def set_bootstrap_mode(self, wallet: str, enabled: bool) -> None:
        wallet_key = str(wallet or "").strip().lower()
        if not wallet_key:
            return
        if enabled:
            self.bootstrap_wallets.add(wallet_key)
        else:
            self.bootstrap_wallets.discard(wallet_key)

    @staticmethod
    def _parse_trade_timestamp(trade: dict[str, Any]) -> datetime | None:
        for field in ("timestamp", "createdAt", "created_at", "time"):
            raw = trade.get(field)
            if raw is None:
                continue

            if isinstance(raw, (int, float)):
                value = float(raw)
                if value <= 0:
                    continue
                if value > 1e12:
                    value = value / 1000.0
                try:
                    return datetime.fromtimestamp(value, tz=timezone.utc)
                except (ValueError, OSError):
                    continue

            if isinstance(raw, str):
                text = raw.strip()
                if not text:
                    continue
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                try:
                    parsed = datetime.fromisoformat(text)
                    if parsed.tzinfo is None:
                        return parsed.replace(tzinfo=timezone.utc)
                    return parsed.astimezone(timezone.utc)
                except ValueError:
                    continue

        return None

    @staticmethod
    def _normalize_side(side_value: Any) -> str:
        side_raw = str(side_value or "").strip().upper()
        if side_raw in ("B", "BUY", "BOUGHT"):
            return "BUY"
        if side_raw in ("S", "SELL", "SOLD"):
            return "SELL"
        return side_raw

    @classmethod
    def is_executed_trade_event(cls, trade: dict[str, Any]) -> bool:
        """Heuristic filter for execution-like events from activity/trades feeds."""
        if not isinstance(trade, dict):
            return False

        side = cls._normalize_side(trade.get("side") or trade.get("type"))
        if side not in ("BUY", "SELL"):
            return False

        status_text = str(trade.get("status") or trade.get("state") or "").strip().lower()
        if any(token in status_text for token in ("cancel", "reject", "fail", "expire", "open", "pending")):
            return False

        type_text = str(trade.get("type") or trade.get("activityType") or "").strip().lower()
        if any(token in type_text for token in ("cancel", "reject", "fail", "order_open", "order_created")):
            return False

        size = trade.get("size") if trade.get("size") is not None else trade.get("amount")
        try:
            if float(size or 0) <= 0:
                return False
        except (TypeError, ValueError):
            return False

        # Execution records typically carry either transaction hash or stable row id.
        tx_hash = str(trade.get("transactionHash") or trade.get("txHash") or "").strip()
        row_id = trade.get("id")
        return bool(tx_hash or row_id is not None)
    
    def initialize_wallet(self, wallet: str, historical_trades: list[dict[str, Any]]) -> None:
        """
        Initialize monitoring for a wallet with historical trades.
        
        Args:
            wallet: Wallet address
            historical_trades: Recent trades to initialize seen set and size history
        """
        # Mark historical trades as seen
        for trade in historical_trades:
            t_key = trade_key(trade)
            self.seen_trades[wallet].add(t_key)
            
            # Track trade size for normalization
            size = float(trade.get("size") or 0)
            if size > 0:
                self.size_history[wallet].append(size)
        
        logger.debug(
            f"Initialized {self._wallet_label(wallet)}: {len(historical_trades)} historical trades, "
            f"{len(self.size_history[wallet])} sizes tracked"
        )
    
    async def collect_new_trades(self, wallet: str) -> list[dict[str, Any]]:
        """
        Collect new trades for a wallet since last check.
        
        Args:
            wallet: Wallet address to monitor
            
        Returns:
            List of new trades (not previously seen)
        """
        try:
            # Get recent trades
            wallet_key = str(wallet or "").strip().lower()
            bootstrap_mode = wallet_key in self.bootstrap_wallets
            base_pages = max(1, Config.TRADE_MAX_PAGES_PER_POLL)
            burst_pages = max(base_pages, Config.TRADE_MAX_PAGES_PER_POLL_BURST)
            pages = max(1, int(self.fetch_pages_by_wallet.get(wallet_key, base_pages)))
            fetch_limit = max(1, Config.TRADE_PAGE_SIZE) * pages
            trades = await self.api_client.get_user_trades(
                wallet=wallet,
                limit=fetch_limit
            )
            
            if not trades:
                return []
            
            # Filter to only new actionable trades
            new_trades: list[dict[str, Any]] = []
            duplicates_filtered = 0
            non_actionable_filtered = 0
            
            # Track trades without txHash for monitoring
            missing_txhash_count = 0

            latest_timestamp_by_market_side: dict[str, datetime] = {}
            
            for trade in trades:
                t_key = trade_key(trade)
                trade_ts = self._parse_trade_timestamp(trade)
                trade_side = self._normalize_side(trade.get("side") or trade.get("type"))
                trade_market = (
                    trade.get("market_slug")
                    or trade.get("marketSlug")
                    or trade.get("slug")
                    or trade.get("eventSlug")
                )
                if trade_market and trade_side in ("BUY", "SELL") and trade_ts is not None:
                    checkpoint_key = f"{str(trade_market).strip().lower()}|{trade_side}"
                    latest = latest_timestamp_by_market_side.get(checkpoint_key)
                    if latest is None or trade_ts > latest:
                        latest_timestamp_by_market_side[checkpoint_key] = trade_ts
                
                # Track missing txHash scenarios
                if not trade.get("transactionHash") and not trade.get("id"):
                    missing_txhash_count += 1
                
                if t_key not in self.seen_trades[wallet]:
                    self.seen_trades[wallet].add(t_key)

                    if trade_side not in ("BUY", "SELL"):
                        non_actionable_filtered += 1
                        logger.debug(
                            f"Filtered non-actionable activity: {self._wallet_label(wallet)} | "
                            f"type={trade.get('type') or 'UNKNOWN'} | "
                            f"side={trade.get('side') or 'UNKNOWN'} | "
                            f"tx={trade.get('transactionHash') or 'NO_TX'}"
                        )
                        continue

                    new_trades.append(trade)
                    
                    # Log first few trade keys for validation (only in debug mode)
                    if len(new_trades) <= 5:
                        has_txhash = "✓" if trade.get("transactionHash") else "✗"
                        logger.debug(
                            f"New trade key: {t_key} | "
                            f"market={trade.get('market_slug') or trade.get('asset', 'unknown')} | "
                            f"outcome={trade.get('outcome', 'unknown')} | "
                            f"txHash={has_txhash} | "
                            f"size={trade.get('size', 'N/A')}"
                        )
                else:
                    duplicates_filtered += 1
                    logger.debug(f"Duplicate trade filtered: {t_key}")
            
            # Only log at debug level - suspicious patterns caught by main_live.py monitoring
            if duplicates_filtered > 0:
                logger.debug(
                    f"Filtered {duplicates_filtered} duplicate(s) from {self._wallet_label(wallet)}: "
                    f"{len(new_trades)} new trade(s) remain"
                )

            if non_actionable_filtered > 0:
                logger.debug(
                    f"Filtered {non_actionable_filtered} non-actionable event(s) from "
                    f"{self._wallet_label(wallet)}"
                )

            total_seen = len(trades)
            duplicate_ratio = (duplicates_filtered / total_seen) if total_seen > 0 else 0.0
            if Config.TRADE_ADAPTIVE_FETCH_ENABLED:
                if (
                    not bootstrap_mode
                    and duplicate_ratio >= max(0.0, Config.TRADE_DUPLICATE_ANOMALY_RATIO)
                    and pages < burst_pages
                ):
                    new_pages = min(burst_pages, pages + 1)
                    if new_pages != pages:
                        self.fetch_pages_by_wallet[wallet_key] = new_pages
                        logger.debug(
                            f"Duplicate anomaly detected for {self._wallet_label(wallet)}; "
                            f"increasing fetch depth: pages={pages} -> {new_pages}"
                        )
                elif (
                    not bootstrap_mode
                    and duplicate_ratio < (max(0.0, Config.TRADE_DUPLICATE_ANOMALY_RATIO) * 0.5)
                    and pages > base_pages
                ):
                    new_pages = max(base_pages, pages - 1)
                    if new_pages != pages:
                        self.fetch_pages_by_wallet[wallet_key] = new_pages
                        logger.debug(
                            f"Duplicate pressure normalized for {self._wallet_label(wallet)}; "
                            f"reducing fetch depth: pages={pages} -> {new_pages}"
                        )

            if latest_timestamp_by_market_side:
                checkpoints = self.market_side_checkpoint[wallet_key]
                for checkpoint_key, timestamp in latest_timestamp_by_market_side.items():
                    previous = checkpoints.get(checkpoint_key)
                    if previous is None or timestamp > previous:
                        checkpoints[checkpoint_key] = timestamp
            
            # Warn if many trades are missing txHash (may indicate API data quality issue)
            if missing_txhash_count > 5 and len(trades) > 0:
                logger.warning(
                    f"{missing_txhash_count}/{len(trades)} trades from {self._wallet_label(wallet)} "
                    f"missing transactionHash (using size-based deduplication fallback)"
                )
            
            return new_trades
            
        except Exception as e:
            logger.exception(f"Error collecting trades for {self._wallet_label(wallet)}: {e}")
            return []

    def _normalize_trade(self, trade: dict[str, Any]) -> dict[str, Any]:
        """Normalize raw API trade payload to internal bot shape."""
        side_raw = str(trade.get("side") or trade.get("type") or "").strip().upper()
        if side_raw in ("B", "BUY", "BOUGHT"):
            side = "BUY"
        elif side_raw in ("S", "SELL", "SOLD"):
            side = "SELL"
        else:
            side = side_raw

        market_slug = (
            trade.get("market_slug")
            or trade.get("marketSlug")
            or trade.get("slug")
            or trade.get("eventSlug")
        )
        original_market_slug = market_slug

        if market_slug and self.slug_converter is not None:
            learned = self.slug_converter.get_learned_mapping(str(market_slug))
            if learned and str(learned).strip().lower() != str(market_slug).strip().lower():
                logger.info(
                    f"Applied learned slug mapping for incoming trade: {market_slug} -> {learned}"
                )
                market_slug = learned
        
        # Log full trade object for debugging unknown market slugs
        if market_slug and not any(x in str(market_slug).lower() for x in ["nba", "nhl", "nfl", "mlb", "ncaab", "ncaaf"]):
            logger.debug(f"Trade data keys: {list(trade.keys())}")
            logger.debug(f"Market slug: {market_slug}")

        return {
            **trade,
            "market_slug": market_slug,
            "original_market_slug": original_market_slug,
            "outcome": trade.get("outcome"),
            "side": side,
            "price": trade.get("price"),
            "size": trade.get("size") or trade.get("amount"),
        }

    async def get_new_trades(self, wallet: str) -> list[dict[str, Any]]:
        """Compatibility wrapper used by main entry points."""
        raw_trades = await self.collect_new_trades(wallet)
        return [self._normalize_trade(t) for t in raw_trades]
    
    def update_size_history(self, wallet: str, size: float) -> None:
        """
        Update size history for a wallet.
        
        Args:
            wallet: Wallet address
            size: Trade size (notional) to add to history
        """
        if size > 0:
            self.size_history[wallet].append(size)
            
            # Keep history from growing unbounded (keep last 1000)
            if len(self.size_history[wallet]) > 1000:
                self.size_history[wallet] = self.size_history[wallet][-1000:]
    
    def get_size_history(self, wallet: str) -> list[float]:
        """
        Get size history for a wallet.
        
        Args:
            wallet: Wallet address
            
        Returns:
            List of historical trade sizes
        """
        return self.size_history.get(wallet, [])

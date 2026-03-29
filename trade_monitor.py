"""Trade monitoring for selected traders."""
import asyncio
import logging
from collections import defaultdict
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
        
        logger.info(
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
            fetch_limit = max(1, Config.TRADE_PAGE_SIZE) * max(1, Config.TRADE_MAX_PAGES_PER_POLL)
            trades = await self.api_client.get_user_trades(
                wallet=wallet,
                limit=fetch_limit
            )
            
            if not trades:
                return []
            
            # Filter to only new trades
            new_trades: list[dict[str, Any]] = []
            duplicates_filtered = 0
            
            # Track trades without txHash for monitoring
            missing_txhash_count = 0
            
            for trade in trades:
                t_key = trade_key(trade)
                
                # Track missing txHash scenarios
                if not trade.get("transactionHash") and not trade.get("id"):
                    missing_txhash_count += 1
                
                if t_key not in self.seen_trades[wallet]:
                    new_trades.append(trade)
                    self.seen_trades[wallet].add(t_key)
                    
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

"""
Live trading mode - REAL MONEY.

Monitors top traders and automatically copies their trades on sports markets.
"""
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from api_client import PolymarketAPIClient
from config import Config
from excel_tracker import ExcelTracker
from liquidation_manager import LiquidationManager
from position_manager import PositionManager
from slug_converter import SlugConverter
from sports_filter import is_sports_market
from trade_executor import TradeExecutor
from trade_monitor import TradeMonitor
from trader_selector import TraderSelector
from utils import calculate_multiplier, calculate_percentile, median, to_float

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(Config.LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)


class LiveTradingBot:
    """Live trading bot for copying top traders."""
    
    def __init__(self):
        """Initialize bot components."""
        self.api_client = PolymarketAPIClient()
        self.position_manager = PositionManager(
            self.api_client,
            state_file=Config.LIVE_POSITION_STATE_FILE,
        )
        self.slug_converter = SlugConverter()
        self.trader_selector = TraderSelector(self.api_client)
        self.trade_monitor = TradeMonitor(self.api_client, self.slug_converter)
        self.trade_executor = TradeExecutor(
            self.api_client,
            test_mode=False,
        )
        self.liquidation_manager = LiquidationManager(
            self.api_client,
            self.position_manager,
        )
        self.excel_tracker = ExcelTracker(Config.LIVE_EXCEL_WORKBOOK)
        
        self.google_tracker = None
        if Config.GOOGLE_SHEETS_ENABLED and Config.GOOGLE_SHEET_ID:
            from google_sheets_tracker import GoogleSheetsTracker
            self.google_tracker = GoogleSheetsTracker(
                Config.GOOGLE_SHEETS_CREDENTIALS, Config.GOOGLE_SHEET_ID
            )
        
        self.running = False
        self.selected_traders: list[dict] = []
        self._last_trader_refresh: datetime | None = None
        self._non_sports_untradable_until: dict[str, datetime] = {}
        self._us_untradable_until: dict[str, datetime] = {}
        self._us_untradable_reasons: dict[str, list[str]] = {}
        self._pending_buy_orders: dict[str, dict[str, Any]] = {}
        self._trader_display_names: dict[str, str] = {}
        self._startup_bootstrap_pending_wallets: set[str] = set()
        self._selected_at_by_wallet_epoch: dict[str, float] = {}
        self._copied_position_shares_cache: dict[str, dict[str, Any]] = {}

    def _is_us_market_temporarily_untradable(self, market_slug: str) -> bool:
        normalized_slug = self._normalize_market_slug(market_slug)
        expires_at = self._us_untradable_until.get(normalized_slug)
        if not expires_at:
            return False
        if datetime.now(timezone.utc) >= expires_at:
            self._us_untradable_until.pop(normalized_slug, None)
            self._us_untradable_reasons.pop(normalized_slug, None)
            return False
        return True

    def _is_non_sports_market_cached(self, market_slug: str) -> bool:
        normalized_slug = self._normalize_market_slug(market_slug)
        expires_at = self._non_sports_untradable_until.get(normalized_slug)
        if not expires_at:
            return False
        if datetime.now(timezone.utc) >= expires_at:
            self._non_sports_untradable_until.pop(normalized_slug, None)
            return False
        return True

    def _cache_non_sports_market(self, market_slug: str) -> None:
        normalized_slug = self._normalize_market_slug(market_slug)
        now = datetime.now(timezone.utc)
        existing_expires = self._non_sports_untradable_until.get(normalized_slug)
        if isinstance(existing_expires, datetime) and now < existing_expires:
            return

        ttl_seconds = max(1, int(Config.NON_SPORTS_SKIP_CACHE_SECONDS))
        expires_at = now.replace(microsecond=0)
        expires_at = expires_at + timedelta(seconds=ttl_seconds)
        self._non_sports_untradable_until[normalized_slug] = expires_at
        logger.info(f"Caching non-sports skip: {market_slug} | ttl={ttl_seconds}s")

    def _mark_us_market_untradable(
        self,
        market_slug: str,
        reasons: list[str] | None = None,
        unavailable_markets_logged: set[str] | None = None,
    ):
        base_ttl = max(1, int(Config.US_UNTRADABLE_CACHE_SECONDS))
        reason_set = {str(r or "").strip().upper() for r in (reasons or []) if str(r or "").strip()}

        # Market-not-found can be transient due to slug-variant mismatches.
        # Use shorter cache to avoid long false-negative lockouts.
        if reason_set and reason_set <= {"MARKET_NOT_FOUND"}:
            ttl_seconds = min(base_ttl, 120)
        else:
            ttl_seconds = base_ttl

        expires_at = datetime.now(timezone.utc).replace(microsecond=0)
        expires_at = expires_at + timedelta(seconds=ttl_seconds)
        normalized_slug = self._normalize_market_slug(market_slug)
        self._us_untradable_until[normalized_slug] = expires_at
        self._us_untradable_reasons[normalized_slug] = sorted(reason_set)
        self._log_unavailable_market_once(
            market_slug,
            "unavailable_us",
            unavailable_markets_logged,
        )

    def _clear_us_market_untradable(self, market_slug: str):
        normalized_slug = self._normalize_market_slug(market_slug)
        if normalized_slug in self._us_untradable_until:
            self._us_untradable_until.pop(normalized_slug, None)
            self._us_untradable_reasons.pop(normalized_slug, None)
            logger.info(f"Removed US-untradable cache for market: {market_slug}")

    @staticmethod
    def _normalize_market_slug(slug: str) -> str:
        value = str(slug or "").strip().lower()
        if not value:
            return ""
        parts = [p for p in value.split("-") if p]
        while len(parts) > 1 and parts[0] in {"aec", "asc", "asm", "acm", "acx"}:
            parts = parts[1:]
        return "-".join(parts)

    @staticmethod
    def _short_wallet(wallet: str | None) -> str:
        value = str(wallet or "").strip().lower()
        if not value:
            return "UNKNOWN_TRADER"
        return f"{value[:8]}..."

    def _trader_label(self, wallet: str | None) -> str:
        value = str(wallet or "").strip().lower()
        if not value:
            return "UNKNOWN_TRADER"
        label = str(self._trader_display_names.get(value) or "").strip()
        return label or self._short_wallet(value)

    def _get_trader_frequency(self, wallet: str) -> float | None:
        """Get avg_trades_per_day for a trader from selected_traders metadata."""
        normalized = str(wallet or "").strip().lower()
        if not normalized:
            return None
        
        for trader in self.selected_traders:
            trader_wallet = str(trader.get("wallet") or "").strip().lower()
            if trader_wallet == normalized:
                return to_float(trader.get("avg_trades_per_day"), default=None)
        
        return None

    def _log_unavailable_market_once(
        self,
        market_slug: str,
        marker: str,
        unavailable_markets_logged: set[str] | None = None,
    ) -> None:
        if unavailable_markets_logged is None:
            logger.info(f"market: {market_slug} | {marker}")
            return

        normalized_slug = self._normalize_market_slug(market_slug)
        if normalized_slug in unavailable_markets_logged:
            return

        unavailable_markets_logged.add(normalized_slug)
        logger.info(f"market: {market_slug} | {marker}")

    @staticmethod
    def _trade_timestamp_epoch(trade: dict[str, Any]) -> float:
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
                return value

            if isinstance(raw, str):
                text = raw.strip()
                if not text:
                    continue
                if text.endswith("Z"):
                    text = text[:-1] + "+00:00"
                try:
                    parsed = datetime.fromisoformat(text)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    else:
                        parsed = parsed.astimezone(timezone.utc)
                    return parsed.timestamp()
                except ValueError:
                    continue

        return 0.0

    def _filter_largest_buy_per_cycle(self, trades: list[dict[str, Any]], wallet: str) -> list[dict[str, Any]]:
        if not trades or not Config.COPY_LARGEST_BUY_PER_CYCLE_ENABLED:
            return trades

        indexed_trades = list(enumerate(trades))
        grouped_buys: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
        kept_indices: set[int] = set()

        for idx, trade in indexed_trades:
            side = str(trade.get("side") or "").strip().upper()
            if side != "BUY":
                kept_indices.add(idx)
                continue

            market_slug = str(trade.get("market_slug") or "").strip()
            outcome = str(trade.get("outcome") or "").strip()
            if not market_slug or not outcome:
                # Keep malformed BUY records for existing downstream validation/logging.
                kept_indices.add(idx)
                continue

            key = (market_slug.lower(), outcome.lower())
            grouped_buys.setdefault(key, []).append((idx, trade))

        filtered_groups = 0
        for (market_slug, outcome), entries in grouped_buys.items():
            if len(entries) == 1:
                kept_indices.add(entries[0][0])
                continue

            winner_idx = entries[0][0]
            winner_trade = entries[0][1]
            winner_size = max(0.0, to_float(winner_trade.get("size"), default=0.0))
            winner_ts = self._trade_timestamp_epoch(winner_trade)

            for idx, trade in entries[1:]:
                size = max(0.0, to_float(trade.get("size"), default=0.0))
                ts = self._trade_timestamp_epoch(trade)
                if size > winner_size or (size == winner_size and ts > winner_ts):
                    winner_idx = idx
                    winner_trade = trade
                    winner_size = size
                    winner_ts = ts

            kept_indices.add(winner_idx)
            filtered_groups += len(entries) - 1
            winner_trade["_largest_buy_filter_meta"] = {
                "market_slug": market_slug,
                "outcome": outcome,
                "kept_size": winner_size,
                "filtered_count": len(entries) - 1,
                "trader_label": self._trader_label(wallet),
            }

        if filtered_groups <= 0:
            return trades

        return [trade for idx, trade in indexed_trades if idx in kept_indices]

    def _aggregate_sell_signals_per_cycle(
        self,
        trades: list[dict[str, Any]],
        wallet: str,
    ) -> tuple[list[dict[str, Any]], int]:
        """Aggregate duplicate SELL signals per market/outcome within one poll cycle."""
        if not trades:
            return trades, 0

        indexed_trades = list(enumerate(trades))
        grouped_sells: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
        kept_indices: set[int] = set()
        replacements: dict[int, dict[str, Any]] = {}

        for idx, trade in indexed_trades:
            side = str(trade.get("side") or "").strip().upper()
            if side != "SELL":
                kept_indices.add(idx)
                continue

            market_slug = str(trade.get("market_slug") or "").strip()
            outcome = str(trade.get("outcome") or "").strip()
            if not market_slug or not outcome:
                kept_indices.add(idx)
                continue

            key = (market_slug.lower(), outcome.lower())
            grouped_sells.setdefault(key, []).append((idx, trade))

        aggregated_count = 0
        for (market_slug, outcome), entries in grouped_sells.items():
            if len(entries) == 1:
                kept_indices.add(entries[0][0])
                continue

            winner_idx = entries[0][0]
            winner_trade = entries[0][1]
            winner_ts = self._trade_timestamp_epoch(winner_trade)
            merged_size = 0.0
            for idx, trade in entries:
                merged_size += max(0.0, to_float(trade.get("size"), default=0.0))
                ts = self._trade_timestamp_epoch(trade)
                if ts > winner_ts:
                    winner_idx = idx
                    winner_trade = trade
                    winner_ts = ts

            merged_trade = dict(winner_trade)
            if merged_size > 0:
                merged_trade["size"] = merged_size
            merged_trade["_aggregated_sell_meta"] = {
                "market_slug": market_slug,
                "outcome": outcome,
                "aggregated_count": len(entries),
                "merged_size": merged_size,
                "trader_label": self._trader_label(wallet),
            }
            replacements[winner_idx] = merged_trade
            kept_indices.add(winner_idx)
            aggregated_count += len(entries) - 1

        if aggregated_count <= 0:
            return trades, 0

        filtered = [
            replacements.get(idx, trade)
            for idx, trade in indexed_trades
            if idx in kept_indices
        ]
        return filtered, aggregated_count

    def _filter_startup_history_trades(
        self,
        trades: list[dict[str, Any]],
        wallet_key: str,
    ) -> list[dict[str, Any]]:
        if wallet_key not in self._startup_bootstrap_pending_wallets:
            return trades

        selected_at_epoch = self._selected_at_by_wallet_epoch.get(wallet_key)
        if not selected_at_epoch:
            self._startup_bootstrap_pending_wallets.discard(wallet_key)
            self.trade_monitor.set_bootstrap_mode(wallet_key, False)
            return trades

        incoming: list[dict[str, Any]] = []
        for trade in trades:
            trade_epoch = self._trade_timestamp_epoch(trade)
            if trade_epoch > selected_at_epoch:
                incoming.append(trade)

        if incoming:
            self._startup_bootstrap_pending_wallets.discard(wallet_key)
            self.trade_monitor.set_bootstrap_mode(wallet_key, False)

        return incoming

    async def _get_copied_position_shares(
        self,
        trader_wallet: str,
        market_slug: str,
        normalized_outcome: str,
    ) -> tuple[float | None, str]:
        """Fetch copied trader shares for market/outcome with cache fallback on API failure."""
        cache_key = "|".join(
            (
                str(trader_wallet or "").strip().lower(),
                self._normalize_market_slug(market_slug),
                str(normalized_outcome or "").strip().lower(),
            )
        )

        now = datetime.now(timezone.utc)
        cache_ttl = max(1, int(Config.COPIED_POSITIONS_CACHE_TTL_SECONDS))
        cached = self._copied_position_shares_cache.get(cache_key)

        def _cache_value(value: float) -> None:
            self._copied_position_shares_cache[cache_key] = {
                "shares": max(0.0, to_float(value, default=0.0)),
                "expires_at": now + timedelta(seconds=cache_ttl),
            }

        positions = await self.api_client.get_user_positions(
            wallet=trader_wallet,
            limit=Config.TRADE_PAGE_SIZE,
        )
        if positions is None:
            if isinstance(cached, dict) and isinstance(cached.get("expires_at"), datetime):
                if now < cached["expires_at"]:
                    return max(0.0, to_float(cached.get("shares"), default=0.0)), "positions_api_cache_fallback"
            return None, "positions_api_unavailable"

        if not positions:
            _cache_value(0.0)
            return 0.0, "positions_api_empty_success"

        target_slug = self._normalize_market_slug(market_slug)
        target_outcome = str(normalized_outcome or "").strip().lower()
        total_shares = 0.0
        matched = False

        for pos in positions:
            if not isinstance(pos, dict):
                continue

            pos_slug = self._normalize_market_slug(
                str(pos.get("slug") or pos.get("marketSlug") or pos.get("market_slug") or "")
            )
            if not pos_slug or pos_slug != target_slug:
                continue

            pos_outcome_raw = str(pos.get("outcome") or "").strip()
            if not pos_outcome_raw:
                continue

            pos_outcome = pos_outcome_raw.lower()
            if pos_outcome not in ("yes", "no"):
                normalized = await self.api_client.normalize_outcome_to_yes_no(
                    market_slug,
                    pos_outcome_raw,
                    caller_context="startup_position_normalization",
                )
                if normalized:
                    pos_outcome = normalized.lower()

            if pos_outcome != target_outcome:
                continue

            matched = True
            total_shares += max(0.0, to_float(pos.get("size"), default=0.0))

        if not matched:
            _cache_value(0.0)
            return 0.0, "positions_api_no_match"

        resolved = max(0.0, total_shares)
        _cache_value(resolved)
        return resolved, "positions_api_live"

    @staticmethod
    def _parse_order_execution(details: dict[str, Any] | None) -> tuple[str, float, float]:
        payload = details if isinstance(details, dict) else {}
        state = str(
            payload.get("state") or payload.get("status") or payload.get("orderState") or ""
        ).upper()

        cum_data = (
            payload.get("cumQuantity")
            or payload.get("cumQty")
            or payload.get("filledQuantity")
            or payload.get("executedQuantity")
            or 0
        )
        if isinstance(cum_data, dict):
            cum_qty = to_float(cum_data.get("value"), default=0.0)
        else:
            cum_qty = to_float(cum_data, default=0.0)

        avg_data = payload.get("avgPx") or payload.get("avgPrice") or payload.get("averagePrice") or 0
        if isinstance(avg_data, dict):
            avg_px = to_float(avg_data.get("value"), default=0.0)
        else:
            avg_px = to_float(avg_data, default=0.0)

        return state, max(0.0, cum_qty), max(0.0, avg_px)

    def _track_pending_buy(self, order_id: str, record: dict[str, Any]):
        self._pending_buy_orders[str(order_id)] = {
            **record,
            "created_at": datetime.now(timezone.utc),
            "next_check_at": datetime.now(timezone.utc),
            "checks": 0,
        }

    async def _reconcile_pending_buys(self):
        if not self._pending_buy_orders:
            return

        now = datetime.now(timezone.utc)
        terminal_states = {
            "ORDER_STATE_CANCELLED",
            "CANCELLED",
            "CANCELED",
            "ORDER_STATE_EXPIRED",
            "EXPIRED",
            "ORDER_STATE_REJECTED",
            "REJECTED",
        }

        for order_id, record in list(self._pending_buy_orders.items()):
            next_check_at = record.get("next_check_at")
            if isinstance(next_check_at, datetime) and now < next_check_at:
                continue

            created_at = record.get("created_at") if isinstance(record.get("created_at"), datetime) else now
            age_seconds = (now - created_at).total_seconds()
            if age_seconds > max(30, Config.BUY_PENDING_RECONCILE_SECONDS):
                # Before abandoning, attempt to recover owner link for unattributed shares
                market_slug = str(record.get("market_slug") or "")
                outcome = str(record.get("outcome") or "")
                trader_wallet = str(record.get("trader_wallet") or "")
                
                recovery_attempted = False
                if market_slug and outcome and trader_wallet:
                    position = self.position_manager.get_position(market_slug, outcome)
                    if position:
                        total_shares = max(0.0, to_float(position.get("shares"), default=0.0))
                        trader_shares = position.get("trader_shares", {})
                        attributed_shares = sum(to_float(s, default=0.0) for s in trader_shares.values())
                        unattributed_shares = total_shares - attributed_shares
                        
                        if unattributed_shares > 0.01:  # Small threshold for float precision
                            linked = self.position_manager.set_position_monitored_trader(
                                market_slug, outcome, trader_wallet
                            )
                            if linked:
                                logger.info(
                                    f"Recovered owner link on reconciliation timeout: {market_slug} | {outcome} | "
                                    f"unattributed_shares={unattributed_shares:.2f} | order_id={order_id}"
                                )
                                recovery_attempted = True
                        else:
                            logger.debug(
                                f"Position fully attributed, skipping timeout recovery: {market_slug} | {outcome} | "
                                f"order_id={order_id}"
                            )
                
                if not recovery_attempted:
                    logger.warning(
                        f"Pending BUY reconciliation timeout: {record.get('market_slug')} | {record.get('outcome')} | "
                        f"order_id={order_id}"
                    )
                
                self._pending_buy_orders.pop(order_id, None)
                continue

            # Fetch order details with exception handling to prevent reconciliation loop failure
            try:
                details = await self.api_client.get_order_details(order_id)
            except Exception as e:
                logger.warning(
                    f"Error fetching order details during reconciliation: {order_id} | {e}"
                )
                # Treat as still pending; will retry on next reconciliation cycle
                checks = int(record.get("checks") or 0) + 1
                record["checks"] = checks
                record["next_check_at"] = now + timedelta(seconds=max(5, Config.BUY_PENDING_RECHECK_SECONDS))
                self._pending_buy_orders[order_id] = record
                continue
            
            state, cum_qty, avg_px = self._parse_order_execution(details)

            market_slug = str(record.get("market_slug") or "")
            outcome = str(record.get("outcome") or "")
            trader_wallet = str(record.get("trader_wallet") or "")

            if cum_qty > 0:
                position = self.position_manager.get_position(market_slug, outcome)
                if position:
                    if trader_wallet:
                        self.position_manager.set_position_monitored_trader(market_slug, outcome, trader_wallet)
                else:
                    fill_price = avg_px if avg_px > 0 else to_float(record.get("current_price"), default=0.0)
                    self.position_manager.open_position(
                        market_slug=market_slug,
                        outcome=outcome,
                        shares=cum_qty,
                        price=fill_price if fill_price > 0 else max(0.01, to_float(record.get("current_price"), default=0.5)),
                        monitored_trader=trader_wallet or None,
                    )

                logger.info(
                    f"Reconciled pending BUY as filled: {market_slug} | {outcome} | "
                    f"shares={cum_qty:.2f} | order_id={order_id}"
                )
                self._pending_buy_orders.pop(order_id, None)
                continue

            if state in terminal_states:
                logger.warning(
                    f"Pending BUY closed without fill: {market_slug} | {outcome} | order_id={order_id} | state={state}"
                )
                self._pending_buy_orders.pop(order_id, None)
                continue

            checks = int(record.get("checks") or 0) + 1
            record["checks"] = checks
            record["next_check_at"] = now + timedelta(seconds=max(5, Config.BUY_PENDING_RECHECK_SECONDS))
            self._pending_buy_orders[order_id] = record

    async def _refresh_selected_traders(self):
        """Refresh monitored traders and warm up monitor state."""
        logger.info("Refreshing selected traders...")
        refreshed_traders = await self.trader_selector.select_top_traders()
        self._last_trader_refresh = datetime.now(timezone.utc)
        
        if not refreshed_traders:
            if self.selected_traders:
                logger.warning(
                    f"Trader refresh returned 0 traders; retaining previous selection "
                    f"({len(self.selected_traders)} traders)"
                )
            else:
                logger.warning("No traders selected during refresh")
            return

        self.selected_traders = refreshed_traders

        selected_at_epoch = datetime.now(timezone.utc).timestamp()
        for index, trader in enumerate(self.selected_traders, start=1):
            wallet = trader.get("wallet")
            if not wallet:
                continue

            wallet_key = str(wallet).strip().lower()
            self._startup_bootstrap_pending_wallets.add(wallet_key)
            self._selected_at_by_wallet_epoch[wallet_key] = selected_at_epoch
            self.trade_monitor.set_bootstrap_mode(wallet_key, True)
            display_name = str(trader.get("display_name") or "").strip()
            if wallet_key and display_name:
                self._trader_display_names[wallet_key] = display_name
                self.trade_monitor.set_wallet_label(wallet_key, display_name)

            logger.info(
                f"Selected trader {index}: {self._trader_label(wallet)} | wallet={wallet_key}"
            )

            historical = await self.api_client.get_user_trades(wallet=wallet, limit=Config.TRADE_PAGE_SIZE)
            self.trade_monitor.initialize_wallet(wallet, historical)

        logger.info(f"Monitoring {len(self.selected_traders)} traders")
    
    async def start(self):
        """Start the bot."""
        logger.info("=" * 80)
        logger.info("LIVE TRADING MODE - REAL MONEY")
        logger.info("=" * 80)
        
        # Verify configuration
        if not Config.POLYMARKET_KEY_ID or not Config.POLYMARKET_SECRET_KEY:
            logger.error("Missing API credentials. Set POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY")
            return
        
        logger.info(f"Max position size per market: {Config.MAX_POSITION_SIZE_PER_MARKET * 100}%")
        logger.info(f"Base risk percent: {Config.BASE_RISK_PERCENT * 100:.2f}%")
        logger.info(f"Sports-only: YES (US API limitation)")
        logger.info(f"Allow BUY SHORT (NO-side): {Config.ALLOW_BUY_SHORT}")
        logger.info(f"Auto-liquidation: {Config.ENABLE_AUTO_LIQUIDATION}")
        logger.info(f"Price range: ${Config.MIN_BUY_PRICE} - ${Config.MAX_BUY_PRICE}")
        
        # Initialize API client
        await self.api_client.initialize()

        # Ensure credentials are usable for authenticated endpoints.
        if self.api_client.private_key is None:
            logger.error("API credentials are invalid or placeholders; live mode cannot start")
            return
        
        # Fetch initial balance
        balance = await self.position_manager.update_balance()
        if balance is None:
            logger.error("Failed to fetch balance. Exiting.")
            return
        
        # Sync positions with API
        await self.position_manager.sync_positions_with_api()

        # Use the same equity calculation as the recurring account-status log
        # so startup values are consistent (cash + live position value).
        summary = self.position_manager.get_summary()
        startup_cash = summary.get("buying_power")
        if startup_cash is None:
            startup_cash = max(0.0, balance - summary["total_invested"])
        startup_positions_value = await self.position_manager.get_total_positions_value()
        startup_equity = startup_cash + startup_positions_value
        logger.info(
            f"Starting account: cash=${startup_cash:.2f} | "
            f"positions=${startup_positions_value:.2f} | "
            f"equity=${startup_equity:.2f} | "
            f"{summary['total_positions']} open"
        )
        
        # Select top traders
        logger.info("Selecting top traders...")
        await self._refresh_selected_traders()
        
        if not self.selected_traders:
            logger.error("No traders selected. Exiting.")
            return
        
        # Log initial state to Excel
        self.excel_tracker.log_balance(
            balance=startup_equity,
            invested=summary['total_invested'],
            total_positions=summary['total_positions'],
        )
        if self.google_tracker:
            self.google_tracker.log_balance(
                balance=balance,
                invested=summary['total_invested'],
                total_positions=summary['total_positions'],
            )
        
        # Update positions in Excel
        positions = self.position_manager.get_all_positions()
        self.excel_tracker.update_positions(positions)
        if self.google_tracker:
            self.google_tracker.update_positions(positions)
        
        # Start loops
        self.running = True
        await asyncio.gather(
            self._trade_poll_loop(),
            self._maintenance_loop(),
            self._account_status_log_loop(),
        )
    
    async def _poll_single_trader(self, trader: dict, semaphore: asyncio.Semaphore):
        """Poll one tracked trader and process any new trades immediately."""
        wallet = trader.get("wallet")
        if not wallet:
            return
        wallet_key = str(wallet).strip().lower()

        async with semaphore:
            trades = await self.trade_monitor.get_new_trades(wallet)

        if not trades:
            return

        polled_trade_count = len(trades)

        trades = self._filter_startup_history_trades(trades, wallet_key)
        if not trades:
            return

        skipped_reasons: list[str] = []
        logger_summary_parts: list[str] = []

        trades = self._filter_largest_buy_per_cycle(trades, wallet)

        trade_key_counts: dict[str, int] = {}
        for trade in trades:
            market_key = str(trade.get("market_slug") or "").strip().lower()
            outcome_key = str(trade.get("outcome") or "").strip().lower()
            if not market_key or not outcome_key:
                continue
            combined_key = f"{market_key}|{outcome_key}"
            trade_key_counts[combined_key] = trade_key_counts.get(combined_key, 0) + 1

        for market_key, count in trade_key_counts.items():
            if count >= Config.POTENTIAL_DUPLICATE_ALERT_THRESHOLD:
                logger.warning(
                    f"Possible duplicate trade processing detected: {market_key} | "
                    f"Count={count} trades in single poll | Trader={self._trader_label(wallet)}"
                )

        trades, aggregated_sell_count = self._aggregate_sell_signals_per_cycle(trades, wallet)
        if aggregated_sell_count > 0:
            logger.info(
                f"Aggregated SELL signals in cycle: trader={self._trader_label(wallet)} | "
                f"collapsed={aggregated_sell_count}"
            )

        unavailable_markets_logged: set[str] = set()

        async def process_trade_with_reason(trade, wallet, unavailable_markets_logged, skipped_reasons):
            # Wraps _process_trade to capture skip reasons
            try:
                # Patch points: add hooks for skip reasons in _handle_buy_signal/_handle_sell_signal
                trade['_skip_reason_hook'] = skipped_reasons
                await self._process_trade(trade, wallet, unavailable_markets_logged)
            finally:
                trade.pop('_skip_reason_hook', None)

        for trade in trades:
            await process_trade_with_reason(trade, wallet, unavailable_markets_logged, skipped_reasons)

        # Summarize skipped reasons
        from collections import Counter
        reason_counter = Counter(skipped_reasons)
        for reason, count in reason_counter.items():
            logger_summary_parts.append(f"{count} {reason}")

        summary_str = " | ".join(logger_summary_parts)
        processed_count = len(trades)
        if summary_str:
            logger.info(
                f"Found {polled_trade_count} new trade(s) from {self._trader_label(wallet)} | "
                f"processed={processed_count} | {summary_str}"
            )
        else:
            logger.info(
                f"Found {polled_trade_count} new trade(s) from {self._trader_label(wallet)} | "
                f"processed={processed_count}"
            )

    async def _trade_poll_loop(self):
        """High-frequency tracked-account polling loop."""
        cycle = 0
        semaphore = asyncio.Semaphore(max(1, Config.TRADE_POLL_CONCURRENCY))

        while self.running:
            loop_started = asyncio.get_running_loop().time()
            try:
                cycle += 1

                # Refresh trader set at configured interval.
                if self._last_trader_refresh is None:
                    await self._refresh_selected_traders()
                else:
                    elapsed = (datetime.now(timezone.utc) - self._last_trader_refresh).total_seconds()
                    if elapsed >= Config.DAILY_POLL_SECONDS:
                        await self._refresh_selected_traders()

                if self.selected_traders:
                    tasks = [
                        self._poll_single_trader(trader, semaphore)
                        for trader in self.selected_traders
                    ]
                    await asyncio.gather(*tasks)

            except Exception as e:
                logger.exception(f"Error in trade poll loop: {e}")

            elapsed = asyncio.get_running_loop().time() - loop_started
            sleep_for = max(0.0, float(Config.TRADE_POLL_SECONDS) - elapsed)
            await asyncio.sleep(sleep_for)

    async def _maintenance_loop(self):
        """Slower personal-account maintenance loop."""
        while self.running:
            try:
                await self._reconcile_pending_buys()
                await self.position_manager.sync_positions_with_api()

                if Config.ENABLE_AUTO_LIQUIDATION:
                    await self.liquidation_manager.manage_liquidation_orders()

                balance = await self.position_manager.update_balance()
                if balance is not None:
                    summary = self.position_manager.get_summary()
                    self.excel_tracker.log_balance(
                        balance=balance,
                        invested=summary['total_invested'],
                        total_positions=summary['total_positions'],
                    )
                    if self.google_tracker:
                        self.google_tracker.log_balance(
                            balance=balance,
                            invested=summary['total_invested'],
                            total_positions=summary['total_positions'],
                        )

                    positions = self.position_manager.get_all_positions()
                    position_pnl_map = {}
                    for position in positions:
                        market_slug = position["market_slug"]
                        outcome = position["outcome"]
                        pnl_data = await self.position_manager.get_position_pnl(market_slug, outcome)
                        if pnl_data:
                            position_key = self.position_manager.get_position_key(market_slug, outcome)
                            position_pnl_map[position_key] = pnl_data

                    self.excel_tracker.update_positions(positions, position_pnl_map)
                    if self.google_tracker:
                        self.google_tracker.update_positions(positions, position_pnl_map)

            except Exception as e:
                logger.exception(f"Error in maintenance loop: {e}")

            await asyncio.sleep(max(1, Config.SCAN_INTERVAL_SECONDS))

    async def _account_status_log_loop(self):
        """Independent once-per-minute account status logger."""
        while self.running:
            loop_started = asyncio.get_running_loop().time()
            try:
                balance = self.position_manager.balance
                if balance is not None:
                    # CRITICAL: Sync with API first to ensure accurate position count
                    # This prevents showing stale cached position data in account status
                    await self.position_manager.sync_positions_with_api()
                    
                    summary = self.position_manager.get_summary()
                    cash = summary.get("buying_power")
                    if cash is None:
                        cash = max(0.0, balance - summary["total_invested"])
                    
                    # Calculate actual position value from current market prices (already uses fresh API data)
                    positions_value = await self.position_manager.get_total_positions_value()
                    
                    # Total equity should be cash + positions
                    total_equity = cash + positions_value
                    
                    logger.info(
                        f"Account: cash=${cash:.2f} | "
                        f"positions=${positions_value:.2f} | "
                        f"equity=${total_equity:.2f} | "
                        f"{summary['total_positions']} open"
                    )
            except Exception as e:
                logger.exception(f"Error in account status log loop: {e}")

            elapsed = asyncio.get_running_loop().time() - loop_started
            sleep_for = max(0.0, 60.0 - elapsed)
            await asyncio.sleep(sleep_for)
    
    async def _process_trade(
        self,
        trade: dict,
        trader_wallet: str,
        unavailable_markets_logged: set[str] | None = None,
    ):
        """
        Process a monitored trader's trade.
        
        Args:
            trade: Trade dict
            trader_wallet: Trader wallet address
        """
        try:
            market_slug = trade.get("market_slug")
            outcome = trade.get("outcome")
            side = trade.get("side")  # "BUY" or "SELL"

            side_upper = str(side or "").strip().upper()
            if side_upper not in ("BUY", "SELL"):
                # Ignore wallet activity that is not an actionable trade signal
                # (e.g., REDEEM, TRANSFER) to keep logs clean.
                logger.debug(
                    f"Skipping non-actionable activity: {market_slug or 'UNKNOWN_MARKET'} | "
                    f"side={side_upper or 'UNKNOWN'} | trader={self._trader_label(trader_wallet)}"
                )
                return
            
            if not market_slug or not outcome or not side:
                logger.warning(f"Incomplete trade data: {trade}")
                return
            
            # Verify it's a sports market (US API limitation)
            if not is_sports_market(market_slug):
                skip_hook = trade.get('_skip_reason_hook')
                if not self._is_non_sports_market_cached(market_slug):
                    self._cache_non_sports_market(market_slug)
                    if skip_hook is not None:
                        skip_hook.append("BUY non-sports market")
                else:
                    if skip_hook is not None:
                        skip_hook.append("BUY non-sports market (cached)")
                return
            
            # Handle based on side
            if side_upper == "BUY":
                observed_price = to_float(trade.get("price"), default=0.0)
                observed_size = to_float(trade.get("size"), default=0.0)
                if observed_size > 0:
                    self.trade_monitor.update_size_history(trader_wallet, observed_size)
                await self._handle_buy_signal(
                    market_slug,
                    outcome,
                    trader_wallet,
                    trade,
                    observed_price=observed_price if observed_price > 0 else None,
                    observed_size=observed_size if observed_size > 0 else None,
                    unavailable_markets_logged=unavailable_markets_logged,
                    largest_buy_meta=trade.get("_largest_buy_filter_meta"),
                )
            elif side_upper == "SELL":
                observed_size = to_float(trade.get("size"), default=0.0)
                await self._handle_sell_signal(
                    market_slug,
                    outcome,
                    trader_wallet=trader_wallet,
                    trade=trade,
                    observed_size=observed_size if observed_size > 0 else None,
                )
            
        except Exception as e:
            logger.exception(f"Error processing trade: {e}")
    
    async def _handle_buy_signal(
        self,
        market_slug: str,
        outcome: str,
        trader_wallet: str,
        trade: dict,
        observed_price: float | None = None,
        observed_size: float | None = None,
        unavailable_markets_logged: set[str] | None = None,
        largest_buy_meta: dict[str, Any] | None = None,
    ):
        """
        Handle a BUY signal from monitored trader.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            trader_wallet: Trader wallet
        """
        normalized_outcome = await self.api_client.normalize_outcome_to_yes_no(
            market_slug,
            outcome,
            strict=False,
            allow_fuzzy=False,
            caller_context="buy_signal",
        )
        if not normalized_outcome:
            logger.warning(f"Cannot normalize outcome for buy: {market_slug} | {outcome}")
            return

        if observed_price is not None and observed_price > 0:
            if observed_price < Config.MIN_BUY_PRICE or observed_price > Config.MAX_BUY_PRICE:
                skip_hook = trade.get('_skip_reason_hook')
                if skip_hook is not None:
                    skip_hook.append("BUY outside range")
                return

        if self._is_us_market_temporarily_untradable(market_slug):
            self._log_unavailable_market_once(
                market_slug,
                "unavailable_us_cached",
                unavailable_markets_logged,
            )
            skip_hook = trade.get('_skip_reason_hook')
            if skip_hook is not None:
                skip_hook.append("BUY unavailable market")
            return

        self.position_manager.reconcile_outcome_alias(
            market_slug=market_slug,
            canonical_outcome=normalized_outcome,
            alias_outcome=outcome,
        )

        # Additional buys are allowed for existing positions; sizing/exposure checks
        # below enforce risk limits for incremental adds.
        
        # Execute buy
        # Determine sizing from configured notional and available quote price.
        current_buy_price = await self.api_client.get_best_price(market_slug, "buy", normalized_outcome)
        if current_buy_price is None or current_buy_price <= 0:
            logger.warning(f"Cannot get buy price for {market_slug} | {normalized_outcome}")
            skip_hook = trade.get('_skip_reason_hook')
            if skip_hook is not None:
                skip_hook.append("BUY unavailable market")
            return

        if current_buy_price < Config.MIN_BUY_PRICE or current_buy_price > Config.MAX_BUY_PRICE:
            skip_hook = trade.get('_skip_reason_hook')
            if skip_hook is not None:
                skip_hook.append("BUY outside range")
            return

        balance = self.position_manager.balance
        if balance is None or balance <= 0:
            logger.warning("Balance unavailable for risk sizing, skipping")
            return

        base_notional = balance * Config.BASE_RISK_PERCENT
        
        # Apply frequency-based scaling to equalize daily allocation across traders
        if Config.FREQUENCY_WEIGHTING_ENABLED:
            trader_frequency = self._get_trader_frequency(trader_wallet)
            if trader_frequency and trader_frequency > 0:
                frequency_factor = Config.FREQUENCY_REFERENCE_TRADES_PER_DAY / trader_frequency
                frequency_factor = max(
                    Config.FREQUENCY_MIN_SCALING_FACTOR,
                    min(Config.FREQUENCY_MAX_SCALING_FACTOR, frequency_factor)
                )
                base_notional = base_notional * frequency_factor
                logger.debug(
                    f"Frequency-adjusted base: {trader_wallet[:8]} | "
                    f"freq={trader_frequency:.1f}/day | factor={frequency_factor:.2f}x | "
                    f"base=${base_notional:.2f}"
                )
        
        history = self.trade_monitor.get_size_history(trader_wallet)
        effective_observed_size = observed_size if (observed_size and observed_size > 0) else base_notional
        wallet_median = median(history)
        percentile = calculate_percentile(history, effective_observed_size) if history else 0.5
        multiplier = calculate_multiplier(
            percentile=percentile,
            observed_size=effective_observed_size,
            wallet_median_size=wallet_median,
            min_multiplier=Config.TAIL_MIN_MULTIPLIER,
            max_multiplier=Config.TAIL_MAX_MULTIPLIER,
            curve_power=Config.TAIL_MULTIPLIER_CURVE_POWER,
            low_size_threshold_ratio=Config.TAIL_LOW_SIZE_THRESHOLD_RATIO,
            low_size_haircut_power=Config.TAIL_LOW_SIZE_HAIRCUT_POWER,
            low_size_haircut_min_factor=Config.TAIL_LOW_SIZE_HAIRCUT_MIN_FACTOR,
        )
        trade_notional_cap = balance * Config.TAIL_MAX_TRADE_NOTIONAL_PCT
        investment_amount = min(base_notional * multiplier, trade_notional_cap)
        if investment_amount <= 0:
            logger.warning("Computed non-positive investment; skipping")
            skip_hook = trade.get('_skip_reason_hook')
            if skip_hook is not None:
                skip_hook.append("BUY outside range")
            return

        can_open, reason = self.position_manager.can_open_position(market_slug, investment_amount)
        if not can_open:
            # Market cap protection - log at debug level, summary will show "market cap exceeded"
            current_exposure = self.position_manager.get_market_exposure(market_slug)
            market_cap = balance * Config.MAX_POSITION_SIZE_PER_MARKET
            logger.debug(
                f"Market cap protection blocked BUY: {market_slug} | {normalized_outcome} | "
                f"Current=${current_exposure:.2f} | Proposed=${investment_amount:.2f} | "
                f"Cap=${market_cap:.2f} | Reason: {reason}"
            )
            skip_hook = trade.get('_skip_reason_hook')
            if skip_hook is not None:
                skip_hook.append("market cap exceeded")
            return

        target_shares = investment_amount / current_buy_price
        # US API limit prices are cent-based in this integration path; sub-penny
        # quotes would be rounded/floored to $0.01 during order placement.
        # Size from executable price floor to prevent accidental notional blowups
        # (for example sizing at $0.002 but executing at $0.01).
        executable_buy_price = max(0.01, current_buy_price)
        if executable_buy_price > current_buy_price:
            logger.warning(
                f"Adjusted BUY sizing to executable price floor: {market_slug} | {normalized_outcome} | "
                f"book=${current_buy_price:.4f} -> exec_floor=${executable_buy_price:.4f}"
            )

        target_shares = investment_amount / executable_buy_price
        effective_observed_price = observed_price if (observed_price and observed_price > 0) else current_buy_price

        if largest_buy_meta and to_float(largest_buy_meta.get("filtered_count"), default=0.0) > 0:
            logger.info(
                f"Largest BUY selected for cycle: {largest_buy_meta.get('market_slug')} | "
                f"{largest_buy_meta.get('outcome')} | "
                f"kept_size={to_float(largest_buy_meta.get('kept_size'), default=0.0):.4f} | "
                f"filtered={int(to_float(largest_buy_meta.get('filtered_count'), default=0.0))} | "
                f"trader={largest_buy_meta.get('trader_label') or self._trader_label(trader_wallet)}"
            )

        logger.info(
            f"Processing BUY: {market_slug} | {outcome.upper()} | "
            f"Trader: {self._trader_label(trader_wallet)} | ${effective_observed_price:.4f}"
        )

        result = await self.trade_executor.execute_buy(
            market_slug=market_slug,
            observed_price=effective_observed_price,
            target_shares=target_shares,
            outcome=normalized_outcome,
        )

        if result and result.get("skipped") and result.get("reason") == "US_MARKET_UNAVAILABLE":
            # Only cache for definitive unavailable reasons.
            reasons = result.get("market_unavailable_reasons") or []
            definitive_reasons = {"MARKET_NOT_FOUND", "NOT_TRADABLE", "ASSET_NOT_FOUND"}
            if any(r in definitive_reasons for r in reasons):
                self._mark_us_market_untradable(
                    market_slug,
                    reasons=reasons,
                    unavailable_markets_logged=unavailable_markets_logged,
                )
            else:
                self._log_unavailable_market_once(
                    market_slug,
                    "unavailable_us_nondefinitive",
                    unavailable_markets_logged,
                )
            return

        if result and result.get("submitted") and result.get("reason") == "BUY_PENDING":
            pending_order_id = str(result.get("order_id") or "").strip()
            if pending_order_id:
                self._track_pending_buy(
                    pending_order_id,
                    {
                        "market_slug": market_slug,
                        "outcome": normalized_outcome,
                        "trader_wallet": trader_wallet,
                        "target_shares": target_shares,
                        "current_price": to_float(result.get("current_price"), default=current_buy_price),
                    },
                )
            logger.info(
                f"Copied BUY submitted; awaiting reconciliation: {market_slug} | {normalized_outcome} | "
                f"order_id={pending_order_id or 'UNKNOWN'} | state={result.get('state') or 'UNKNOWN'}"
            )
            return
        
        if result and result.get("success"):
            filled_price = to_float(result.get("current_price"), default=current_buy_price)
            filled_shares = to_float(result.get("shares"), default=target_shares)

            self.position_manager.open_position(
                market_slug=market_slug,
                outcome=normalized_outcome,
                shares=filled_shares,
                price=filled_price,
                monitored_trader=trader_wallet,
            )

            # Log to Excel
            self.excel_tracker.log_trade(
                market_slug=market_slug,
                outcome=normalized_outcome,
                side="BUY",
                shares=filled_shares,
                price=filled_price,
                trader=self._trader_label(trader_wallet),
                status="executed",
            )
            if self.google_tracker:
                self.google_tracker.log_trade(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    side="BUY",
                    shares=filled_shares,
                    price=filled_price,
                    trader=self._trader_label(trader_wallet),
                    status="executed",
                )
            
            # If auto-liquidation enabled, order will be created in next cycle
            if Config.ENABLE_AUTO_LIQUIDATION:
                logger.info(f"Auto-liquidation order will be created in next cycle")
    
    async def _handle_sell_signal(
        self,
        market_slug: str,
        outcome: str,
        trader_wallet: str | None = None,
        trade: dict | None = None,
        observed_size: float | None = None,
    ):
        """
        Handle a SELL signal from monitored trader.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
        """
        normalized_outcome = await self.api_client.normalize_outcome_to_yes_no(
            market_slug,
            outcome,
            caller_context="sell_signal",
        )
        if not normalized_outcome:
            logger.warning(f"Cannot normalize outcome for sell: {market_slug} | {outcome}")
            return

        self.position_manager.reconcile_outcome_alias(
            market_slug=market_slug,
            canonical_outcome=normalized_outcome,
            alias_outcome=outcome,
        )

        # Check if we have a position
        if not self.position_manager.has_position(market_slug, normalized_outcome):
            logger.debug(f"No position in {market_slug} | {normalized_outcome}, skipping sell signal")
            skip_hook = trade.get('_skip_reason_hook') if trade else None
            if skip_hook is not None:
                skip_hook.append("Unowned SELL")
            return
        
        position = self.position_manager.get_position(market_slug, normalized_outcome)
        if not position:
            logger.debug(f"No position in {market_slug} | {normalized_outcome}, skipping sell signal")
            return

        held_shares = to_float(position.get("shares"), default=0.0)
        if held_shares <= 0:
            logger.warning(f"Position has no shares for {market_slug} | {normalized_outcome}, skipping")
            return

        # Safety: copied SELLs should only manage positions that were opened by the
        # same monitored trader. This prevents cross-trader/cross-side liquidation
        # and synthetic short creation when API payloads are ambiguous.
        monitored_trader = str(position.get("monitored_trader") or "").strip().lower()
        incoming_trader = str(trader_wallet or "").strip().lower()

        if not incoming_trader:
            logger.warning(
                f"Skipping SELL with missing trader identity: {market_slug} | {normalized_outcome}"
            )
            return

        trader_local_shares = self.position_manager.get_trader_attributed_shares(
            market_slug,
            normalized_outcome,
            incoming_trader,
        )

        if trader_local_shares <= 0 and not monitored_trader:
            recovered_owner = self.position_manager.get_recent_owner_candidate(
                market_slug,
                normalized_outcome,
                Config.POSITION_OWNER_RECOVERY_TTL_SECONDS,
            )
            if (
                Config.SELL_OWNER_CONDITIONAL_ALLOW_ENABLED
                and recovered_owner
                and recovered_owner == incoming_trader
            ):
                self.position_manager.set_position_monitored_trader(
                    market_slug,
                    normalized_outcome,
                    incoming_trader,
                )
                monitored_trader = incoming_trader
                logger.info(
                    f"Recovered SELL owner link and allowing copied SELL: {market_slug} | "
                    f"{normalized_outcome} | trader={self._trader_label(incoming_trader)}"
                )
                trader_local_shares = self.position_manager.get_trader_attributed_shares(
                    market_slug,
                    normalized_outcome,
                    incoming_trader,
                )
            else:
                logger.info(
                    f"Skipping SELL for unlinked/manual position: {market_slug} | {normalized_outcome} | "
                    f"incoming trader={self._trader_label(incoming_trader)}"
                )
                return

        if trader_local_shares <= 0 and monitored_trader != incoming_trader:
            logger.info(
                f"Skipping SELL from non-owning trader: {market_slug} | {normalized_outcome} | "
                f"position owner={self._trader_label(monitored_trader)}, "
                f"signal trader={self._trader_label(incoming_trader)}"
            )
            return

        if trader_local_shares <= 0 and monitored_trader == incoming_trader:
            trader_local_shares = held_shares

        if trader_local_shares <= 0:
            logger.info(
                f"Skipping SELL from trader with no attributed local shares: {market_slug} | "
                f"{normalized_outcome} | trader={self._trader_label(incoming_trader)}"
            )
            return

        shares_to_sell = trader_local_shares
        copied_sell_shares = max(0.0, to_float(observed_size, default=0.0))

        if Config.SELL_PERCENT_SIZING_ENABLED:
            if copied_sell_shares <= 0:
                logger.warning(
                    f"Skipping SELL: missing copied sell size for percent scaling: {market_slug} | "
                    f"{normalized_outcome} | trader={self._trader_label(incoming_trader)}"
                )
                return

            copied_current_shares, denominator_mode = await self._get_copied_position_shares(
                incoming_trader,
                market_slug,
                normalized_outcome,
            )
            if copied_current_shares is None:
                logger.warning(
                    f"Skipping SELL: copied position denominator unavailable from positions API: "
                    f"{market_slug} | {normalized_outcome} | trader={self._trader_label(incoming_trader)}"
                )
                return

            # Positions snapshot may be post-trade; denominator uses current+sold
            # to conservatively approximate copied pre-sell position.
            copied_denominator = copied_current_shares + copied_sell_shares
            if copied_denominator <= 0:
                logger.warning(
                    f"Skipping SELL: invalid copied denominator from positions API: {market_slug} | "
                    f"{normalized_outcome} | current={copied_current_shares:.4f}, sold={copied_sell_shares:.4f}"
                )
                return

            sell_ratio = copied_sell_shares / copied_denominator
            sizing_reason = denominator_mode

            sell_ratio = max(0.0, min(1.0, sell_ratio))
            shares_to_sell = trader_local_shares * sell_ratio

            near_full_threshold = max(0.5, min(1.0, Config.SELL_NEAR_FULL_RATIO_THRESHOLD))
            if (
                int(max(0.0, float(shares_to_sell))) <= 0
                and sell_ratio >= near_full_threshold
                and trader_local_shares >= 1.0
            ):
                shares_to_sell = trader_local_shares
                sizing_reason = f"{sizing_reason}+near_full_integer_override"
                logger.info(
                    f"SELL near-full override applied: {market_slug} | {normalized_outcome} | "
                    f"ratio={sell_ratio:.4f} >= {near_full_threshold:.2f}, "
                    f"forcing full attributed liquidation={shares_to_sell:.2f}"
                )

            logger.info(
                f"SELL percent sizing: {market_slug} | {normalized_outcome} | "
                f"copied_current={copied_current_shares:.2f}, copied_sell={copied_sell_shares:.2f}, "
                f"ratio={sell_ratio:.4f}, local_trader_held={trader_local_shares:.2f}, local_sell={shares_to_sell:.2f}, "
                f"mode={sizing_reason}"
            )

        if shares_to_sell > trader_local_shares:
            logger.info(
                f"Oversell signal capped to trader-attributed size: requested={shares_to_sell:.2f}, "
                f"attributed={trader_local_shares:.2f}"
            )
            shares_to_sell = trader_local_shares

        if shares_to_sell > held_shares:
            logger.info(
                f"Oversell signal capped to total held size: requested={shares_to_sell:.2f}, held={held_shares:.2f}"
            )
            shares_to_sell = held_shares

        if shares_to_sell <= 0:
            logger.warning(
                f"Computed non-positive SELL size for {market_slug} | {normalized_outcome}; skipping"
            )
            return

        # Cancel liquidation order if exists
        if Config.ENABLE_AUTO_LIQUIDATION:
            await self.liquidation_manager.cancel_liquidation_order(market_slug, normalized_outcome)
        
        logger.info(
            f"Processing SELL: {market_slug} | {outcome.upper()} | "
            f"Trader: {self._trader_label(trader_wallet)} | {shares_to_sell:.2f} shares"
        )
        
        # Execute sell
        result = await self.trade_executor.execute_sell(
            market_slug=market_slug,
            shares=shares_to_sell,
            outcome=normalized_outcome,
            allow_full_liquidation_on_oversell=True,
            treat_as_market=True,
        )

        if result and result.get("skipped") and result.get("reason") == "IOC_UNFILLED":
            logger.warning(
                f"Copied SELL did not execute (IOC unfilled): {market_slug} | {normalized_outcome} | "
                f"requested={shares_to_sell:.2f} | order_ids={result.get('order_ids') or []}"
            )
            return
        
        if result and result.get("success"):
            exit_price = to_float(result.get("price"), default=0.0)
            sold_shares = to_float(result.get("shares"), default=shares_to_sell)

            logger.info(
                f"Copied SELL executed (market-style IOC): {market_slug} | {normalized_outcome} | "
                f"{sold_shares:.2f} shares @ ${exit_price:.4f}"
            )

            if sold_shares >= held_shares - 1e-9:
                closed_position = self.position_manager.close_position(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    exit_price=exit_price if exit_price > 0 else 0.0,
                    reason="trader_signal",
                )

                if closed_position:
                    pnl = to_float(closed_position.get("pnl"), default=0.0)
                    pnl_pct = to_float(closed_position.get("pnl_pct"), default=0.0)
                    logger.info(f"P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
            else:
                remaining_shares = max(0.0, held_shares - sold_shares)
                self.position_manager.update_position_shares(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    new_shares=remaining_shares,
                    trader_wallet=incoming_trader,
                )
                logger.info(
                    f"Partial SELL applied: {market_slug} | {normalized_outcome} | "
                    f"sold={sold_shares:.2f}, remaining={remaining_shares:.2f}"
                )

            # Log to Excel
            self.excel_tracker.log_trade(
                market_slug=market_slug,
                outcome=normalized_outcome,
                side="SELL",
                shares=sold_shares if sold_shares > 0 else shares_to_sell,
                price=exit_price if exit_price > 0 else 0.0,
                status="executed",
            )
            if self.google_tracker:
                self.google_tracker.log_trade(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    side="SELL",
                    shares=sold_shares if sold_shares > 0 else shares_to_sell,
                    price=exit_price if exit_price > 0 else 0.0,
                    status="submitted",
                )

            await self._cancel_non_liquidation_sell_limits(market_slug, normalized_outcome)

    async def _cancel_non_liquidation_sell_limits(self, market_slug: str, outcome: str):
        """
        Cancel stale non-liquidation resting sell limits for this market/outcome.

        Copied sells are submitted IOC and should not remain as GTC orders. Keep only
        configured auto-liquidation GTC order(s) at Config.LIQUIDATION_PRICE.
        """
        orders = await self.api_client.get_orders()
        if not orders:
            return

        expected_intent = "ORDER_INTENT_SELL_LONG" if outcome.lower() == "yes" else "ORDER_INTENT_SELL_SHORT"
        target_price = float(Config.LIQUIDATION_PRICE)

        for order in orders:
            if not isinstance(order, dict):
                continue

            slug = str(order.get("marketSlug") or order.get("market_slug") or "").strip()
            if slug != market_slug:
                continue

            intent = str(order.get("intent") or "").upper()
            if intent != expected_intent:
                continue

            tif = str(order.get("tif") or "").upper()
            if tif != "TIME_IN_FORCE_GOOD_TILL_CANCEL":
                continue

            price_data = order.get("price")
            if isinstance(price_data, dict):
                order_price = to_float(price_data.get("value"), default=-1.0)
            else:
                order_price = to_float(price_data, default=-1.0)

            # Keep only liquidation-price GTC orders.
            if abs(order_price - target_price) <= 0.001:
                continue

            order_id = str(order.get("id") or order.get("orderId") or order.get("order_id") or "").strip()
            if not order_id:
                continue

            canceled = await self.api_client.cancel_order(order_id, market_slug=market_slug)
            if canceled:
                logger.info(
                    f"Canceled stale copied-sell GTC order: {market_slug} | {outcome} | "
                    f"Order ID: {order_id} | price={order_price:.4f}"
                )
    
    async def shutdown(self):
        """Shutdown the bot gracefully."""
        logger.info("\nShutting down...")
        
        self.running = False
        
        # Cancel all liquidation orders
        if Config.ENABLE_AUTO_LIQUIDATION:
            await self.liquidation_manager.cancel_all_liquidation_orders()
        
        # Save slug converter mappings
        self.slug_converter.save_mappings()
        
        # Close Excel tracker
        self.excel_tracker.close()
        if self.google_tracker:
            self.google_tracker.close()
        
        # Shutdown API client
        await self.api_client.shutdown()
        
        logger.info("Shutdown complete")


async def main():
    """Main entry point."""
    bot = LiveTradingBot()
    
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
    finally:
        await bot.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nExiting...")

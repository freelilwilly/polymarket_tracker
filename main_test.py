"""
Test mode - SIMULATED trading (NO REAL MONEY).

Monitors top traders and simulates copying their trades.
Uses public APIs only (no authentication required).
"""
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from api_client import PolymarketAPIClient
from config import Config
from excel_tracker import ExcelTracker
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


class TestTradingBot:
    """Test mode bot for simulated trading."""
    
    def __init__(self):
        """Initialize bot components."""
        # Hard safety invariant: test mode must never place real orders.
        self.api_client = PolymarketAPIClient(allow_order_execution=False)
        
        # Position manager without real API authentication
        self.position_manager = PositionManager(
            self.api_client,
            state_file=Config.TEST_POSITION_STATE_FILE,
        )
        self.trade_executor = TradeExecutor(
            self.api_client,
            test_mode=True,
        )
        # Override balance with simulated value
        self.position_manager.balance = Config.STARTING_BANKROLL
        self.position_manager._save_state()
        
        self.slug_converter = SlugConverter()
        self.trader_selector = TraderSelector(self.api_client)
        self.trade_monitor = TradeMonitor(self.api_client, self.slug_converter)
        self.excel_tracker = ExcelTracker(Config.TEST_EXCEL_WORKBOOK)
        
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
        self._sell_execution_locks: dict[str, asyncio.Lock] = {}
        self._recent_sell_signal_at: dict[str, datetime] = {}

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

    def _mark_us_market_untradable(
        self,
        market_slug: str,
        reasons: list[str] | None = None,
        unavailable_markets_logged: set[str] | None = None,
    ):
        base_ttl = max(1, int(Config.US_UNTRADABLE_CACHE_SECONDS))
        reason_set = {str(r or "").strip().upper() for r in (reasons or []) if str(r or "").strip()}

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
    def _normalize_market_slug(slug: str) -> str:
        value = str(slug or "").strip().lower()
        if not value:
            return ""
        parts = [p for p in value.split("-") if p]
        while len(parts) > 1 and parts[0] in {"aec", "asc", "asm", "acm", "acx"}:
            parts = parts[1:]
        return "-".join(parts)

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
                logger.warning(
                    f"Pending BUY reconciliation timeout: {record.get('market_slug')} | {record.get('outcome')} | "
                    f"order_id={order_id}"
                )
                self._pending_buy_orders.pop(order_id, None)
                continue

            details = await self.api_client.get_order_details(order_id)
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
        """Start the bot in test mode."""
        logger.info("=" * 80)
        logger.info("TEST MODE - SIMULATED TRADING (NO REAL MONEY)")
        logger.info("=" * 80)
        
        logger.info(f"Simulated balance: ${Config.STARTING_BANKROLL:.2f}")
        logger.info(f"Max position size per market: {Config.MAX_POSITION_SIZE_PER_MARKET * 100}%")
        logger.info(f"Base risk percent: {Config.BASE_RISK_PERCENT * 100:.2f}%")
        logger.info("Sports-only filter: True (live-mode parity)")
        logger.info(f"Allow BUY SHORT (NO-side): {Config.ALLOW_BUY_SHORT}")
        logger.info(f"Price range: ${Config.MIN_BUY_PRICE} - ${Config.MAX_BUY_PRICE}")
        logger.info("Order execution safety guard: ENABLED (all order placement blocked)")
        
        # Initialize API client (no authentication needed for test mode)
        await self.api_client.initialize()
        
        # Get position summary
        summary = self.position_manager.get_summary()
        
        # Select top traders
        logger.info("Selecting top traders...")
        await self._refresh_selected_traders()
        
        if not self.selected_traders:
            logger.error("No traders selected. Exiting.")
            return
        
        # Log initial state to Excel
        self.excel_tracker.log_balance(
            balance=self.position_manager.balance,
            invested=summary['total_invested'],
            total_positions=summary['total_positions'],
        )
        if self.google_tracker:
            self.google_tracker.log_balance(
                balance=self.position_manager.balance,
                invested=summary['total_invested'],
                total_positions=summary['total_positions'],
            )
        
        # Update positions in Excel
        positions = self.position_manager.get_all_positions()
        self.excel_tracker.update_positions(positions)
        if self.google_tracker:
            self.google_tracker.update_positions(positions)
        
        # Start main loop
        self.running = True
        await asyncio.gather(
            self._trade_poll_loop(),
            self._maintenance_loop(),
        )
    
    async def _poll_single_trader(self, trader: dict, semaphore: asyncio.Semaphore):
        wallet = trader.get("wallet")
        if not wallet:
            return
        wallet_key = str(wallet).strip().lower()

        async with semaphore:
            trades = await self.trade_monitor.get_new_trades(wallet)

        if not trades:
            return

        polled_trade_count = len(trades)
        executed_trade_count = sum(
            1 for trade in trades if self.trade_monitor.is_executed_trade_event(trade)
        )

        trades = self._filter_startup_history_trades(trades, wallet_key)
        if not trades:
            return

        logger.info(f"Found {len(trades)} new trade(s) from {self._trader_label(wallet)}")
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
            try:
                trade['_skip_reason_hook'] = skipped_reasons
                await self._process_trade(trade, wallet, unavailable_markets_logged)
            finally:
                trade.pop('_skip_reason_hook', None)

        for trade in trades:
            await process_trade_with_reason(trade, wallet, unavailable_markets_logged, skipped_reasons)

        from collections import Counter
        reason_counter = Counter(skipped_reasons)
        for reason, count in reason_counter.items():
            logger_summary_parts.append(f"{count} {reason}")

        summary_str = " | ".join(logger_summary_parts)
        if summary_str:
            logger.info(
                f"Found {executed_trade_count} executed trade(s) from {self._trader_label(wallet)} | "
                f"{summary_str}"
            )
        else:
            logger.info(
                f"Found {executed_trade_count} executed trade(s) from {self._trader_label(wallet)}"
            )

    async def _trade_poll_loop(self):
        """High-frequency tracked-account polling loop (test mode)."""
        semaphore = asyncio.Semaphore(max(1, Config.TRADE_POLL_CONCURRENCY))

        while self.running:
            loop_started = asyncio.get_running_loop().time()
            try:
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
        """Slower maintenance loop for balance/position reporting (test mode)."""
        while self.running:
            try:
                await self._reconcile_pending_buys()

                summary = self.position_manager.get_summary()
                logger.info(
                    f"Balance: ${self.position_manager.balance:.2f} | "
                    f"Invested: ${summary['total_invested']:.2f} | "
                    f"Positions: {summary['total_positions']}"
                )

                self.excel_tracker.log_balance(
                    balance=self.position_manager.balance,
                    invested=summary['total_invested'],
                    total_positions=summary['total_positions'],
                )
                if self.google_tracker:
                    self.google_tracker.log_balance(
                        balance=self.position_manager.balance,
                        invested=summary['total_invested'],
                        total_positions=summary['total_positions'],
                    )

                positions = self.position_manager.get_all_positions()
                position_pnl_map = {}
                for position in positions:
                    market_slug = position["market_slug"]
                    outcome = position["outcome"]
                    pnl_data = await self._calculate_simulated_pnl(position)
                    if pnl_data:
                        position_key = self.position_manager.get_position_key(market_slug, outcome)
                        position_pnl_map[position_key] = pnl_data

                self.excel_tracker.update_positions(positions, position_pnl_map)
                if self.google_tracker:
                    self.google_tracker.update_positions(positions, position_pnl_map)

            except Exception as e:
                logger.exception(f"Error in maintenance loop: {e}")

            await asyncio.sleep(max(1, Config.SCAN_INTERVAL_SECONDS))
    
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
                return
            
            if not market_slug or not outcome or not side:
                logger.warning(f"Incomplete trade data: {trade}")
                return
            
            # Keep test mode behavior aligned with live-mode US constraints.
            if not is_sports_market(market_slug):
                if not self._is_non_sports_market_cached(market_slug):
                    self._cache_non_sports_market(market_slug)
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
                    observed_price=observed_price if observed_price > 0 else None,
                    observed_size=observed_size if observed_size > 0 else None,
                    largest_buy_meta=trade.get("_largest_buy_filter_meta"),
                    unavailable_markets_logged=unavailable_markets_logged,
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
        observed_price: float | None = None,
        observed_size: float | None = None,
        largest_buy_meta: dict[str, Any] | None = None,
        unavailable_markets_logged: set[str] | None = None,
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
                logger.debug(
                    f"Skipping copied BUY outside configured range: {market_slug} | {outcome} | "
                    f"observed=${observed_price:.4f} not in "
                    f"${Config.MIN_BUY_PRICE:.2f}-${Config.MAX_BUY_PRICE:.2f}"
                )
                return

        if self._is_us_market_temporarily_untradable(market_slug):
            self._log_unavailable_market_once(
                market_slug,
                "unavailable_us_cached",
                unavailable_markets_logged,
            )
            return

        market_info = await self.api_client.get_market_info(market_slug)
        if not market_info:
            logger.warning(f"Skipping BUY: market metadata unavailable for {market_slug}")
            return

        market_closed = bool(market_info.get("closed", False))
        accepting_orders = bool(market_info.get("acceptingOrders", True))
        if market_closed or not accepting_orders:
            reasons = ["MARKET_CLOSED"] if market_closed else ["NOT_TRADABLE"]
            self._mark_us_market_untradable(
                market_slug,
                reasons=reasons,
                unavailable_markets_logged=unavailable_markets_logged,
            )
            return

        self.position_manager.reconcile_outcome_alias(
            market_slug=market_slug,
            canonical_outcome=normalized_outcome,
            alias_outcome=outcome,
        )

        current_buy_price = await self.api_client.get_best_price(market_slug, "buy", normalized_outcome)
        if current_buy_price is None or current_buy_price <= 0:
            logger.warning(f"Cannot get buy price for {market_slug} | {normalized_outcome}")
            return

        if current_buy_price < Config.MIN_BUY_PRICE or current_buy_price > Config.MAX_BUY_PRICE:
            logger.debug(
                f"Price ${current_buy_price:.4f} out of range "
                f"(${Config.MIN_BUY_PRICE:.2f}-${Config.MAX_BUY_PRICE:.2f}), skipping"
            )
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
        
        # Use observed trade-size percentile normalization from monitored wallet history.
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
            return
        
        # Check market exposure cap
        can_open, reason = self.position_manager.can_open_position(market_slug, investment_amount)
        if not can_open:
            logger.warning(f"Cannot open position: {reason}")
            return

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
            f"Processing BUY (SIM): {market_slug} | {outcome.upper()} | "
            f"Trader: {self._trader_label(trader_wallet)} | ${effective_observed_price:.4f}"
        )

        result = await self.trade_executor.execute_buy(
            market_slug=market_slug,
            observed_price=effective_observed_price,
            target_shares=target_shares,
            outcome=normalized_outcome,
        )

        if result and result.get("skipped") and result.get("reason") == "US_MARKET_UNAVAILABLE":
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

        if not (result and result.get("success")):
            return

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

            copied_denominator = copied_current_shares + copied_sell_shares
            if copied_denominator <= 0:
                logger.warning(
                    f"Skipping SELL: invalid copied denominator from positions API: {market_slug} | "
                    f"{normalized_outcome} | current={copied_current_shares:.4f}, sold={copied_sell_shares:.4f}"
                )
                return

            sell_ratio = copied_sell_shares / copied_denominator
            sell_ratio = max(0.0, min(1.0, sell_ratio))
            shares_to_sell = trader_local_shares * sell_ratio

            near_full_threshold = max(0.5, min(1.0, Config.SELL_NEAR_FULL_RATIO_THRESHOLD))
            sizing_mode = denominator_mode
            if (
                int(max(0.0, float(shares_to_sell))) <= 0
                and sell_ratio >= near_full_threshold
                and trader_local_shares >= 1.0
            ):
                shares_to_sell = trader_local_shares
                sizing_mode = f"{sizing_mode}+near_full_integer_override"
                logger.info(
                    f"SELL near-full override applied: {market_slug} | {normalized_outcome} | "
                    f"ratio={sell_ratio:.4f} >= {near_full_threshold:.2f}, "
                    f"forcing full attributed liquidation={shares_to_sell:.2f}"
                )

            logger.info(
                f"SELL percent sizing: {market_slug} | {normalized_outcome} | "
                f"copied_current={copied_current_shares:.2f}, copied_sell={copied_sell_shares:.2f}, "
                f"ratio={sell_ratio:.4f}, local_trader_held={trader_local_shares:.2f}, local_sell={shares_to_sell:.2f}, "
                f"mode={sizing_mode}"
            )

        if shares_to_sell > trader_local_shares:
            logger.info(
                f"Oversell signal capped to trader-attributed size: requested={shares_to_sell:.2f}, "
                f"attributed={trader_local_shares:.2f}"
            )
            shares_to_sell = trader_local_shares

        if shares_to_sell > held_shares:
            logger.info(
                f"Oversell signal capped to held size: requested={shares_to_sell:.2f}, held={held_shares:.2f}"
            )
            shares_to_sell = held_shares

        if shares_to_sell <= 0:
            logger.warning(
                f"Computed non-positive SELL size for {market_slug} | {normalized_outcome}; skipping"
            )
            return

        lock_key = f"{self._normalize_market_slug(market_slug)}|{normalized_outcome}"
        sell_lock = self._sell_execution_locks.setdefault(lock_key, asyncio.Lock())
        dedupe_key = f"{lock_key}|{incoming_trader}"
        dedupe_window = max(1, int(Config.SELL_DEDUPE_WINDOW_SECONDS))

        async with sell_lock:
            now = datetime.now(timezone.utc)
            recent_at = self._recent_sell_signal_at.get(dedupe_key)
            if isinstance(recent_at, datetime):
                age_seconds = (now - recent_at).total_seconds()
                if age_seconds < dedupe_window:
                    logger.warning(
                        f"Duplicate SELL detected within dedupe window, skipping: {market_slug} | "
                        f"{normalized_outcome} | trader={self._trader_label(incoming_trader)} | "
                        f"age={age_seconds:.2f}s < {dedupe_window}s"
                    )
                    return
            self._recent_sell_signal_at[dedupe_key] = now

            logger.info(
                f"Processing SELL (SIM): {market_slug} | {outcome.upper()} | "
                f"Trader: {self._trader_label(trader_wallet)} | {shares_to_sell:.2f} shares"
            )

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
                    f"requested={shares_to_sell:.2f}"
                )
                return

            if result and result.get("skipped") and result.get("reason") == "IOC_UNFILLED_OR_ALREADY_CLOSED":
                close_epsilon = max(0.0, to_float(Config.SELL_CLOSE_EPSILON_SHARES, default=0.01))
                live_remaining = await self.trade_executor._get_live_position_size(market_slug, normalized_outcome)
                if live_remaining is not None and live_remaining <= close_epsilon:
                    self.position_manager.close_position(
                        market_slug=market_slug,
                        outcome=normalized_outcome,
                        exit_price=0.0,
                        reason="trader_signal_reconciled",
                    )
                    logger.info(
                        f"SELL reconciled as externally closed after uncertain IOC visibility (SIM): "
                        f"{market_slug} | {normalized_outcome} | live_remaining={live_remaining:.4f}"
                    )
                else:
                    logger.warning(
                        f"SELL remained open after uncertain IOC visibility (SIM): {market_slug} | {normalized_outcome} | "
                        f"live_remaining={(live_remaining if live_remaining is not None else 'UNKNOWN')}"
                    )
                return

            if not (result and result.get("success")):
                return

            exit_price = to_float(result.get("price"), default=0.0)
            sold_shares = to_float(result.get("shares"), default=shares_to_sell)
            close_epsilon = max(0.0, to_float(Config.SELL_CLOSE_EPSILON_SHARES, default=0.01))
            live_remaining = await self.trade_executor._get_live_position_size(market_slug, normalized_outcome)

            if (live_remaining is not None and live_remaining <= close_epsilon) or sold_shares >= held_shares - 1e-9:
                closed_position = self.position_manager.close_position(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    exit_price=exit_price if exit_price > 0 else 0.0,
                    reason="trader_signal",
                )

                if closed_position:
                    pnl = closed_position.get("pnl", 0)
                    pnl_pct = closed_position.get("pnl_pct", 0)
                    logger.info(f"P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
            else:
                remaining_shares = max(0.0, held_shares - sold_shares)
                effective_remaining = min(remaining_shares, max(0.0, live_remaining)) if live_remaining is not None else remaining_shares
                self.position_manager.update_position_shares(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    new_shares=effective_remaining,
                    trader_wallet=incoming_trader,
                )
                logger.info(
                    f"Partial SELL applied: {market_slug} | {normalized_outcome} | "
                    f"sold={sold_shares:.2f}, remaining={effective_remaining:.2f}"
                )

            # Log to trackers even when partial.
            self.excel_tracker.log_trade(
                market_slug=market_slug,
                outcome=normalized_outcome,
                side="SELL",
                shares=sold_shares if sold_shares > 0 else shares_to_sell,
                price=exit_price if exit_price > 0 else 0.0,
                trader=self._trader_label(trader_wallet),
                status="executed",
            )
            if self.google_tracker:
                self.google_tracker.log_trade(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    side="SELL",
                    shares=sold_shares if sold_shares > 0 else shares_to_sell,
                    price=exit_price if exit_price > 0 else 0.0,
                    trader=self._trader_label(trader_wallet),
                    status="executed",
                )
    
    async def _calculate_simulated_pnl(self, position: dict) -> dict[str, Any]:
        """
        Calculate simulated P&L for a position.
        
        Args:
            position: Position dict
            
        Returns:
            P&L data dict
        """
        market_slug = position["market_slug"]
        outcome = position["outcome"]
        shares = position["shares"]
        invested = position.get("invested", shares * position["entry_price"])
        
        # Use the same valuation path as live mode for parity.
        pnl_data = await self.position_manager.get_position_pnl(market_slug, outcome)
        if not pnl_data:
            return {}

        current_value = shares * to_float(pnl_data.get("current_price"), default=0.0)
        return {
            "current_value": current_value,
            "pnl": to_float(pnl_data.get("pnl"), default=0.0),
            "pnl_pct": to_float(pnl_data.get("pnl_pct"), default=0.0),
            "invested": invested,
        }
    
    async def shutdown(self):
        """Shutdown the bot gracefully."""
        logger.info("\nShutting down...")
        
        self.running = False
        
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
    bot = TestTradingBot()
    
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

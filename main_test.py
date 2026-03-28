"""
Test mode - SIMULATED trading (NO REAL MONEY).

Monitors top traders and simulates copying their trades.
Uses public APIs only (no authentication required).
"""
import asyncio
import logging
import sys
from datetime import datetime, timezone

from api_client import PolymarketAPIClient
from config import Config
from excel_tracker import ExcelTracker
from position_manager import PositionManager
from slug_converter import SlugConverter
from sports_filter import is_sports_market
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
        self.api_client = PolymarketAPIClient()
        
        # Position manager without real API authentication
        self.position_manager = PositionManager(self.api_client)
        # Override balance with simulated value
        self.position_manager.balance = Config.STARTING_BANKROLL
        self.position_manager._save_state()
        
        self.slug_converter = SlugConverter()
        self.trader_selector = TraderSelector(self.api_client)
        self.trade_monitor = TradeMonitor(self.api_client, self.slug_converter)
        self.excel_tracker = ExcelTracker(Config.TEST_EXCEL_WORKBOOK)
        
        self.running = False
        self.selected_traders: list[dict] = []
        self._last_trader_refresh: datetime | None = None

    async def _refresh_selected_traders(self):
        """Refresh monitored traders and warm up monitor state."""
        logger.info("Refreshing selected traders...")
        self.selected_traders = await self.trader_selector.select_top_traders()
        self._last_trader_refresh = datetime.now(timezone.utc)
        
        if not self.selected_traders:
            logger.warning("No traders selected during refresh")
            return

        logger.info(f"Monitoring {len(self.selected_traders)} traders")
        for trader in self.selected_traders:
            wallet = trader.get("wallet")
            if not wallet:
                continue
            historical = await self.api_client.get_user_trades(wallet=wallet, limit=Config.TRADE_PAGE_SIZE)
            self.trade_monitor.initialize_wallet(wallet, historical)
    
    async def start(self):
        """Start the bot in test mode."""
        logger.info("=" * 80)
        logger.info("TEST MODE - SIMULATED TRADING (NO REAL MONEY)")
        logger.info("=" * 80)
        
        logger.info(f"Simulated balance: ${Config.STARTING_BANKROLL:.2f}")
        logger.info(f"Max position size per market: {Config.MAX_POSITION_SIZE_PER_MARKET * 100}%")
        logger.info(f"Base risk percent: {Config.BASE_RISK_PERCENT * 100:.2f}%")
        logger.info(f"Sports-only filter: {Config.SPORTS_ONLY}")
        logger.info(f"Allow BUY SHORT (NO-side): {Config.ALLOW_BUY_SHORT}")
        logger.info(f"Price range: ${Config.MIN_BUY_PRICE} - ${Config.MAX_BUY_PRICE}")
        
        # Initialize API client (no authentication needed for test mode)
        await self.api_client.initialize()
        
        # Get position summary
        summary = self.position_manager.get_summary()
        logger.info(
            f"Position summary: {summary['total_positions']} positions, "
            f"${summary['total_invested']:.2f} invested, "
            f"${summary['available']:.2f} available"
        )
        
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
        
        # Update positions in Excel
        positions = self.position_manager.get_all_positions()
        self.excel_tracker.update_positions(positions)
        
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

        async with semaphore:
            trades = await self.trade_monitor.get_new_trades(wallet)

        if not trades:
            return

        logger.info(f"Found {len(trades)} new trade(s) from {wallet[:8]}...")
        for trade in trades:
            await self._process_trade(trade, wallet)

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

            except Exception as e:
                logger.exception(f"Error in maintenance loop: {e}")

            await asyncio.sleep(max(1, Config.SCAN_INTERVAL_SECONDS))
    
    async def _process_trade(self, trade: dict, trader_wallet: str):
        """
        Process a monitored trader's trade (SIMULATED).
        
        Args:
            trade: Trade dict
            trader_wallet: Trader wallet address
        """
        try:
            market_slug = trade.get("market_slug")
            outcome = trade.get("outcome")
            side = trade.get("side")  # "BUY" or "SELL"
            
            if not market_slug or not outcome or not side:
                logger.warning(f"Incomplete trade data: {trade}")
                return
            
            # Apply sports filter if enabled
            if Config.SPORTS_ONLY and not is_sports_market(market_slug):
                logger.debug(f"Skipping non-sports market: {market_slug}")
                return
            
            logger.info(
                f"Processing trade: {market_slug} | {outcome.upper()} | {side.upper()} | "
                f"Trader: {trader_wallet[:8]}..."
            )
            
            # Handle based on side
            if side.upper() == "BUY":
                observed_size = to_float(trade.get("size"), default=0.0)
                if observed_size > 0:
                    self.trade_monitor.update_size_history(trader_wallet, observed_size)
                await self._handle_buy_signal(
                    market_slug,
                    outcome,
                    trader_wallet,
                    observed_size=observed_size if observed_size > 0 else None,
                )
            elif side.upper() == "SELL":
                await self._handle_sell_signal(market_slug, outcome)
            
        except Exception as e:
            logger.exception(f"Error processing trade: {e}")
    
    async def _handle_buy_signal(
        self,
        market_slug: str,
        outcome: str,
        trader_wallet: str,
        observed_size: float | None = None,
    ):
        """
        Handle a BUY signal (SIMULATED).
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            trader_wallet: Trader wallet
        """
        normalized_outcome = await self.api_client.normalize_outcome_to_yes_no(market_slug, outcome)
        if not normalized_outcome:
            logger.warning(f"Cannot normalize outcome for buy: {market_slug} | {outcome}")
            return

        self.position_manager.reconcile_outcome_alias(
            market_slug=market_slug,
            canonical_outcome=normalized_outcome,
            alias_outcome=outcome,
        )

        # Additional buys are allowed for existing positions; sizing/exposure checks
        # below enforce risk limits for incremental adds.
        
        # Check if NO-side trading is disabled
        if normalized_outcome.lower() == "no" and not Config.ALLOW_BUY_SHORT:
            logger.info(f"NO-side trading disabled, skipping {market_slug} | NO")
            return
        
        # Get current market price
        current_price = await self.api_client.get_best_price(market_slug, "buy", normalized_outcome)
        
        if current_price is None:
            logger.warning(f"Cannot get price for {market_slug} | {normalized_outcome}")
            return
        
        # Apply price filters
        if current_price < Config.MIN_BUY_PRICE:
            logger.info(
                f"Price ${current_price:.4f} below minimum ${Config.MIN_BUY_PRICE}, skipping"
            )
            return
        
        if current_price > Config.MAX_BUY_PRICE:
            logger.info(
                f"Price ${current_price:.4f} above maximum ${Config.MAX_BUY_PRICE}, skipping"
            )
            return
        
        balance = self.position_manager.balance
        if balance is None or balance <= 0:
            logger.warning("Balance unavailable for risk sizing, skipping")
            return

        base_notional = balance * Config.BASE_RISK_PERCENT
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
        investment = min(base_notional * multiplier, trade_notional_cap)
        if investment <= 0:
            logger.warning("Computed non-positive investment; skipping")
            return

        shares = investment / current_price
        
        # Check market exposure cap
        can_open, reason = self.position_manager.can_open_position(market_slug, investment)
        if not can_open:
            logger.warning(f"Cannot open position: {reason}")
            return
        
        # SIMULATED: Open position
        logger.info(
            f"[SIMULATED BUY] {market_slug} | {normalized_outcome.upper()} | "
            f"{shares:.2f} shares @ ${current_price:.4f} = ${investment:.2f}"
        )
        
        self.position_manager.open_position(
            market_slug=market_slug,
            outcome=normalized_outcome,
            shares=shares,
            price=current_price,
            monitored_trader=trader_wallet,
        )
        
        # Log to Excel
        self.excel_tracker.log_trade(
            market_slug=market_slug,
            outcome=normalized_outcome,
            side="BUY",
            shares=shares,
            price=current_price,
            trader=trader_wallet[:8],
            status="simulated",
        )
    
    async def _handle_sell_signal(
        self,
        market_slug: str,
        outcome: str,
    ):
        """
        Handle a SELL signal (SIMULATED).
        
        Args:
            market_slug: Market slug
            outcome: Outcome
        """
        normalized_outcome = await self.api_client.normalize_outcome_to_yes_no(market_slug, outcome)
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
            return
        
        position = self.position_manager.get_position(market_slug, normalized_outcome)
        if not position:
            return
        
        # Get current market price
        current_price = await self.api_client.get_best_price(market_slug, "sell", normalized_outcome)
        
        if current_price is None:
            logger.warning(f"Cannot get price for {market_slug} | {normalized_outcome}")
            return
        
        shares = position["shares"]
        
        # SIMULATED: Close position
        logger.info(
            f"[SIMULATED SELL] {market_slug} | {normalized_outcome.upper()} | "
            f"{shares:.2f} shares @ ${current_price:.4f}"
        )
        
        closed_position = self.position_manager.close_position(
            market_slug=market_slug,
            outcome=normalized_outcome,
            exit_price=current_price,
            reason="trader_signal",
        )
        
        if closed_position:
            pnl = closed_position.get("pnl", 0)
            pnl_pct = closed_position.get("pnl_pct", 0)
            logger.info(f"P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)")
            
            # Log to Excel
            self.excel_tracker.log_trade(
                market_slug=market_slug,
                outcome=normalized_outcome,
                side="SELL",
                shares=shares,
                price=current_price,
                status="simulated",
            )
    
    async def _calculate_simulated_pnl(self, position: dict) -> dict:
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
        
        # Get current market price
        current_price = await self.api_client.get_best_price(market_slug, "sell", outcome)
        
        if current_price is None:
            return {}
        
        current_value = shares * current_price
        pnl = current_value - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0.0
        
        return {
            "current_value": current_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
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

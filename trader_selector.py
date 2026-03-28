"""Trader selection logic."""
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from api_client import PolymarketAPIClient
from config import Config
from utils import to_float

logger = logging.getLogger(__name__)


class TraderSelector:
    """Selects top traders based on performance criteria."""
    
    def __init__(self, api_client: PolymarketAPIClient):
        """Initialize trader selector."""
        self.api_client = api_client

    async def _get_required_tag_wallets(self) -> set[str]:
        """Get wallet set matching REQUIRED_TRADER_TAGS via analytics API."""
        required_tags = [
            t.strip() for t in (Config.REQUIRED_TRADER_TAGS or "").split(",") if t.strip()
        ]
        if not required_tags:
            return set()

        # Fetch a broad tagged snapshot; bot only needs membership for top candidates.
        tagged_rows = await self.api_client.get_traders_performance(
            limit=max(1000, Config.CANDIDATE_LIMIT * 2),
            apply_required_tags=True,
        )
        wallets = {
            str(row.get("wallet") or "").strip().lower()
            for row in tagged_rows
            if row.get("wallet")
        }
        logger.info(f"Loaded {len(wallets)} wallets matching required tags")
        return wallets

    @staticmethod
    def _safe_trade_timestamp(raw_ts: Any) -> datetime:
        """Parse trade timestamp (seconds or milliseconds) into UTC datetime."""
        try:
            ts = float(raw_ts)
            if ts > 1_000_000_000_000:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return datetime.fromtimestamp(0, tz=timezone.utc)

    async def _select_top_traders_from_global_trades(self) -> list[dict[str, Any]]:
        """Fallback selector using recent global trades from Data API."""
        sample_size = max(Config.CANDIDATE_LIMIT * 6, 1000)
        trades = await self.api_client.get_recent_global_trades(limit=sample_size)
        if not trades:
            return []

        now = datetime.now(timezone.utc)
        week_ago = now - timedelta(days=7)
        stats: dict[str, dict[str, float]] = defaultdict(
            lambda: {"week_count": 0.0, "notional": 0.0}
        )

        for trade in trades:
            if not isinstance(trade, dict):
                continue
            wallet = (
                trade.get("proxyWallet")
                or trade.get("user")
                or trade.get("makerAddress")
                or trade.get("wallet")
            )
            if not wallet:
                continue

            trade_time = self._safe_trade_timestamp(trade.get("timestamp"))
            if trade_time < week_ago:
                continue

            size = to_float(trade.get("size"), default=0.0)
            price = to_float(trade.get("price"), default=0.0)
            stats[wallet]["week_count"] += 1.0
            stats[wallet]["notional"] += max(0.0, size * price)

        # Rank wallets by notional proxy first and keep only top CANDIDATE_LIMIT,
        # then apply behavior filters to that bounded set.
        ranked_wallets = sorted(
            stats.items(),
            key=lambda item: item[1]["notional"],
            reverse=True,
        )
        candidate_wallets = ranked_wallets[: Config.CANDIDATE_LIMIT]

        qualified: list[dict[str, Any]] = []
        for wallet, wallet_stats in candidate_wallets:
            avg_trades_per_day = wallet_stats["week_count"] / 7.0
            if avg_trades_per_day < Config.MIN_TRADES_PER_DAY:
                continue
            if avg_trades_per_day > Config.MAX_TRADES_PER_DAY:
                continue

            overall_profit_proxy = wallet_stats["notional"]
            if overall_profit_proxy <= 0:
                continue

            qualified.append(
                {
                    "wallet": wallet,
                    "display_name": wallet[:8],
                    # Public trades endpoint does not expose win rate directly.
                    "win_rate": max(Config.MIN_WIN_RATE, 75.0),
                    "avg_trades_per_day": avg_trades_per_day,
                    "overall_profit": overall_profit_proxy,
                }
            )

        qualified.sort(key=lambda x: x["overall_profit"], reverse=True)
        return qualified[: Config.TOP_N_USERS]
    
    async def select_top_traders(self) -> list[dict[str, Any]]:
        """
        Select top traders based on win rate, trade frequency, and profitability.
        
        Returns:
            List of trader dicts with wallet, display_name, win_rate, avg_trades_per_day, overall_profit
        """
        try:
            # Get trader performance data from Analytics API
            logger.info("Fetching trader performance data...")
            traders_data = await self.api_client.get_traders_performance(
                limit=Config.CANDIDATE_LIMIT,
                apply_required_tags=False,
            )
            
            if not traders_data:
                logger.warning("No trader data received from Analytics API, using Data API fallback")
                fallback = await self._select_top_traders_from_global_trades()
                logger.info(f"Fallback selected {len(fallback)} traders")
                return fallback
            
            ranked_candidates = sorted(
                traders_data,
                key=lambda trader: float(trader.get("overall_gain") or 0.0),
                reverse=True,
            )
            candidate_pool = ranked_candidates[: Config.CANDIDATE_LIMIT]
            logger.info(
                f"Received {len(traders_data)} analytics traders; "
                f"using global top {len(candidate_pool)} as candidate pool"
            )

            required_wallets = await self._get_required_tag_wallets()
            
            # Filter traders
            qualified_traders: list[dict[str, Any]] = []
            
            for trader in candidate_pool:
                wallet = trader.get("wallet") or trader.get("address")
                if not wallet:
                    continue
                wallet_norm = str(wallet).strip().lower()

                if required_wallets and wallet_norm not in required_wallets:
                    continue
                
                # Extract performance metrics
                win_rate = float(trader.get("win_rate") or 0)
                if 0 <= win_rate <= 1:
                    win_rate *= 100.0
                overall_profit = float(trader.get("overall_gain") or 0)
                
                # Get recent trades to calculate trade frequency
                recent_trades = await self.api_client.get_user_trades(
                    wallet=wallet,
                    limit=200
                )
                
                if not recent_trades:
                    continue
                
                # Calculate trades per day over past week
                now = datetime.now(timezone.utc)
                week_ago = now - timedelta(days=7)
                recent_week_trades = [
                    t for t in recent_trades
                    if datetime.fromtimestamp(float(t.get("timestamp", 0)), tz=timezone.utc) >= week_ago
                ]
                
                avg_trades_per_day = len(recent_week_trades) / 7.0
                
                # Apply filters
                if win_rate < Config.MIN_WIN_RATE:
                    continue
                if avg_trades_per_day < Config.MIN_TRADES_PER_DAY:
                    continue
                if avg_trades_per_day > Config.MAX_TRADES_PER_DAY:
                    continue
                if overall_profit <= 0:
                    continue
                
                display_name = trader.get("display_name") or wallet[:8]
                
                qualified_traders.append({
                    "wallet": wallet,
                    "display_name": display_name,
                    "win_rate": win_rate,
                    "avg_trades_per_day": avg_trades_per_day,
                    "overall_profit": overall_profit,
                })
            
            # Sort by overall profit and take top N
            qualified_traders.sort(key=lambda x: x["overall_profit"], reverse=True)
            selected = qualified_traders[:Config.TOP_N_USERS]
            
            logger.info(f"Selected {len(selected)} traders meeting criteria")
            
            return selected
            
        except Exception as e:
            logger.exception(f"Error selecting traders: {e}")
            return []

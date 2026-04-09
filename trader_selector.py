"""Trader selection logic."""
import asyncio
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

    async def _get_required_tag_candidates(self, max_rank: int) -> list[dict[str, Any]]:
        """Get tagged analytics candidates constrained to ACTUAL global rank threshold."""
        required_tags = [
            t.strip() for t in (Config.REQUIRED_TRADER_TAGS or "").split(",") if t.strip()
        ]
        if not required_tags:
            return []

        rank_cap = max(1, int(max_rank))
        fetch_limit = max(2000, rank_cap * 4)

        # CRITICAL: When tag filters are applied, the API returns rank WITHIN that tag group,
        # not global rank. To get actual global top-N with tags, we need to:
        # 1. Fetch global top-N traders (no tag filter)
        # 2. Cross-reference with tagged traders to check if they have required tags
        
        retry_delays = [0.0, 0.5, 1.0]
        
        global_traders = None
        tagged_traders_map = None
        
        for attempt, delay_seconds in enumerate(retry_delays, start=1):
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)

            # Fetch global top-N traders (unfiltered)
            if global_traders is None:
                global_traders = await self.api_client.get_traders_performance(
                    limit=fetch_limit,
                    apply_required_tags=False,
                )
                if not global_traders:
                    logger.warning(f"Global trader fetch returned empty on attempt {attempt}")
                    continue

            # Fetch tagged traders to check tag membership
            if tagged_traders_map is None:
                tagged_rows = await self.api_client.get_traders_performance(
                    limit=fetch_limit,
                    apply_required_tags=True,
                )
                if not tagged_rows:
                    logger.warning(f"Tagged trader fetch returned empty on attempt {attempt}")
                    continue
                    
                # Build wallet -> trader map for tagged traders
                tagged_traders_map = {}
                for row in tagged_rows:
                    if isinstance(row, dict):
                        wallet = str(row.get("wallet") or row.get("address") or "").strip().lower()
                        if wallet:
                            tagged_traders_map[wallet] = row

            # Cross-reference: keep only global top-N traders who have required tags
            candidates: list[dict[str, Any]] = []
            for trader in global_traders:
                if not isinstance(trader, dict):
                    continue
                    
                wallet = str(trader.get("wallet") or trader.get("address") or "").strip().lower()
                if not wallet:
                    continue

                # Get GLOBAL rank from unfiltered API response
                global_rank = int(to_float(trader.get("rank"), default=0.0))
                if global_rank <= 0 or global_rank > rank_cap:
                    continue
                
                # Check if this trader has the required tags
                if wallet not in tagged_traders_map:
                    continue
                
                # Use trader data from global fetch (has correct global rank)
                # but verify they have required tags via tagged_traders_map presence
                display_name = trader.get("display_name") or wallet[:8]
                logger.debug(
                    f"Including trader {display_name} | global_rank={global_rank} | has required tags"
                )
                candidates.append(trader)

            if candidates:
                logger.info(
                    f"Loaded {len(candidates)} traders with required tags within global top {rank_cap} "
                    f"(attempt {attempt}/{len(retry_delays)})"
                )
                return candidates

            logger.warning(
                f"Cross-reference returned 0 qualified traders on attempt {attempt}/{len(retry_delays)}"
            )

        return []

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
            required_tags = [
                t.strip() for t in (Config.REQUIRED_TRADER_TAGS or "").split(",") if t.strip()
            ]

            candidate_pool: list[dict[str, Any]] = []

            if required_tags:
                # Intended behavior: global top-N first, then tag filter.
                # Analytics exposes global rank per trader; use rank<=N within tagged feed
                # to reconstruct that set robustly even when page ordering is inconsistent.
                candidate_pool = await self._get_required_tag_candidates(
                    max_rank=Config.CANDIDATE_LIMIT
                )
                if not candidate_pool:
                    logger.error(
                        "Required tag filter active but no tagged global-top candidates were found; "
                        "aborting selection to avoid unfiltered expansion"
                    )
                    return []
            else:
                # Get trader performance data from Analytics API
                logger.info("Fetching trader performance data...")
                traders_data = await self.api_client.get_traders_performance(
                    limit=max(1000, Config.CANDIDATE_LIMIT * 4),
                    apply_required_tags=False,
                )

                if traders_data:
                    ranked_by_rank = [
                        trader
                        for trader in traders_data
                        if 0 < int(to_float(trader.get("rank"), default=0.0)) <= Config.CANDIDATE_LIMIT
                    ]
                    if ranked_by_rank:
                        ranked_by_rank.sort(
                            key=lambda trader: int(to_float(trader.get("rank"), default=10_000_000))
                        )
                        candidate_pool = ranked_by_rank
                    else:
                        ranked_candidates = sorted(
                            traders_data,
                            key=lambda trader: float(trader.get("overall_gain") or 0.0),
                            reverse=True,
                        )
                        candidate_pool = ranked_candidates[: Config.CANDIDATE_LIMIT]

                if not candidate_pool:
                    traders_data = []

            if not candidate_pool:
                if Config.ENABLE_TRADER_SELECTION_FALLBACK:
                    logger.warning("No trader data received from Analytics API, using Data API fallback")
                    fallback = await self._select_top_traders_from_global_trades()

                    # If required tags are configured, never allow untagged fallback selection.
                    if required_tags:
                        logger.error(
                            "Required tag filter active and analytics candidates unavailable; "
                            "refusing fallback selection (fail-closed)"
                        )
                        return []

                    logger.info(f"Fallback selected {len(fallback)} traders")
                    return fallback

                logger.error(
                    "No trader data received from Analytics API; fallback disabled for safety"
                )
                return []

            # Filter traders
            qualified_traders: list[dict[str, Any]] = []
            
            for trader in candidate_pool:
                wallet = trader.get("wallet") or trader.get("address")
                if not wallet:
                    continue
                
                # Check banned list (by wallet address or display name)
                banned_list = [t.strip().lower() for t in (Config.BANNED_TRADERS or "").split(",") if t.strip()]
                if banned_list:
                    wallet_lower = str(wallet).strip().lower()
                    display_name_lower = str(trader.get("display_name") or "").strip().lower()
                    if wallet_lower in banned_list or display_name_lower in banned_list:
                        logger.debug(f"Skipping banned trader: {trader.get('display_name') or wallet[:8]}")
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

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Set

import aiohttp
from dotenv import load_dotenv

from twitter_client import TwitterClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        RotatingFileHandler("polymarket_tracker_v2.log", maxBytes=10 * 1024 * 1024, backupCount=5),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


class PolymarketTopUsersLiveBot:
    """Daily top-user selector + minute-level live polling for selected users."""

    def __init__(self):
        self.data_api_base = "https://data-api.polymarket.com"
        self.gamma_api_base = "https://gamma-api.polymarket.com"
        self.analytics_api_base = "https://polymarketanalytics.com"

        self.output_file = os.getenv("V2_OUTPUT_FILE", "test_output.txt")
        self.daily_rebuild_seconds = int(os.getenv("V2_DAILY_POLL_SECONDS", "86400"))
        self.trade_poll_seconds = int(os.getenv("V2_TRADE_POLL_SECONDS", "60"))
        self.run_once = self._parse_bool(os.getenv("V2_RUN_ONCE", "false"))
        self.dry_run = self._parse_bool(os.getenv("DRY_RUN", "true"))

        self.top_n = int(os.getenv("V2_TOP_USERS", "5"))
        self.candidate_limit = int(os.getenv("V2_CANDIDATE_LIMIT", "300"))
        self.leaderboard_page_size = int(os.getenv("V2_LEADERBOARD_PAGE_SIZE", "100"))
        self.min_win_rate = float(os.getenv("V2_MIN_WIN_RATE", "80"))
        self.min_trades_per_day = float(os.getenv("V2_MIN_TRADES_PER_DAY", "1"))
        self.max_trades_per_day = float(os.getenv("V2_MAX_TRADES_PER_DAY", "50"))
        self.enforce_computed_win_rate = self._parse_bool(os.getenv("V2_ENFORCE_COMPUTED_WIN_RATE", "false"))
        self.http_max_attempts = int(os.getenv("V2_HTTP_MAX_ATTEMPTS", "5"))
        self.trade_fetch_concurrency = int(os.getenv("V2_TRADE_FETCH_CONCURRENCY", "1"))
        self.trade_fetch_delay_seconds = float(os.getenv("V2_TRADE_FETCH_DELAY_SECONDS", "0.15"))
        self.trade_page_size = int(os.getenv("V2_TRADE_PAGE_SIZE", "200"))
        self.trade_max_pages_per_poll = int(os.getenv("V2_TRADE_MAX_PAGES_PER_POLL", "10"))

        self.session: Optional[aiohttp.ClientSession] = None
        self.twitter_client: Optional[TwitterClient] = None
        self.next_rebuild_at: Optional[datetime] = None

        self.selected_users: List[Dict[str, Any]] = []
        self.seen_trade_keys: Dict[str, Set[str]] = {}

        self.market_cache: Dict[str, Dict[str, Any]] = {}
        self.trade_fetch_semaphore = asyncio.Semaphore(max(1, self.trade_fetch_concurrency))

        if not self.dry_run:
            self.twitter_client = TwitterClient(
                api_key=os.getenv("TWITTER_API_KEY"),
                api_secret=os.getenv("TWITTER_API_SECRET"),
                access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
                access_secret=os.getenv("TWITTER_ACCESS_SECRET"),
                bearer_token=os.getenv("TWITTER_BEARER_TOKEN"),
            )

    async def initialize(self):
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def shutdown(self):
        if self.session:
            await self.session.close()

    async def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.session:
            await self.initialize()

        max_attempts = max(1, self.http_max_attempts)
        for attempt in range(1, max_attempts + 1):
            try:
                async with self.session.get(url, params=params) as response:
                    if response.status == 200:
                        return await response.json()

                    should_retry = response.status in {429, 500, 502, 503, 504}
                    retry_after_header = response.headers.get("Retry-After")
                    retry_after_seconds: Optional[float] = None
                    if retry_after_header:
                        retry_after_seconds = self._to_float(retry_after_header, default=None)

                    if should_retry and attempt < max_attempts:
                        base_backoff = max(0.0, 0.6 * (2 ** (attempt - 1)))
                        jitter = random.uniform(0.0, 0.4)
                        sleep_seconds = retry_after_seconds if retry_after_seconds is not None else base_backoff + jitter
                        await asyncio.sleep(max(0.0, sleep_seconds))
                        continue

                    text = await response.text()
                    logger.warning(f"GET {url} failed ({response.status}): {text[:180]}")
                    return None
            except Exception as error:
                if attempt == max_attempts:
                    logger.error(f"Request error for {url}: {error}")
                    return None
                base_backoff = max(0.0, 0.6 * (2 ** (attempt - 1)))
                jitter = random.uniform(0.0, 0.4)
                await asyncio.sleep(base_backoff + jitter)

        return None

    async def get_leaderboard(
        self,
        limit: int,
        offset: int,
        time_period: str = "all",
        order_by: str = "PNL",
    ) -> List[Dict[str, Any]]:
        params = {
            "timePeriod": time_period,
            "orderBy": order_by,
            "limit": limit,
            "offset": offset,
            "category": "overall",
        }
        data = await self._get_json(f"{self.data_api_base}/v1/leaderboard", params=params)
        return data if isinstance(data, list) else []

    async def get_user_trades(self, wallet: str, limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
        params = {"user": wallet, "limit": limit, "offset": max(0, offset)}
        async with self.trade_fetch_semaphore:
            if self.trade_fetch_delay_seconds > 0:
                await asyncio.sleep(self.trade_fetch_delay_seconds)
            activity_data = await self._get_json(f"{self.data_api_base}/activity", params=params)
            if isinstance(activity_data, list):
                return [
                    item
                    for item in activity_data
                    if not item.get("type") or str(item.get("type")).upper() == "TRADE"
                ]

            legacy_data = await self._get_json(f"{self.data_api_base}/trades", params=params)
            return legacy_data if isinstance(legacy_data, list) else []

    async def get_user_activity(self, wallet: str, limit: int = 500, offset: int = 0) -> List[Dict[str, Any]]:
        params = {"user": wallet, "limit": limit, "offset": max(0, offset)}
        async with self.trade_fetch_semaphore:
            if self.trade_fetch_delay_seconds > 0:
                await asyncio.sleep(self.trade_fetch_delay_seconds)
            data = await self._get_json(f"{self.data_api_base}/activity", params=params)
            return data if isinstance(data, list) else []

    async def _collect_new_trades_for_wallet(self, wallet: str, seen: Set[str]) -> List[Dict[str, Any]]:
        page_size = max(1, self.trade_page_size)
        max_pages = max(1, self.trade_max_pages_per_poll)

        offset = 0
        new_trades: List[Dict[str, Any]] = []
        new_trade_keys: Set[str] = set()

        for _ in range(max_pages):
            activity_page = await self.get_user_activity(wallet=wallet, limit=page_size, offset=offset)
            if activity_page:
                trades_page = [
                    item
                    for item in activity_page
                    if not item.get("type") or str(item.get("type")).upper() == "TRADE"
                ]
                page_item_count = len(activity_page)
            else:
                trades_page = await self.get_user_trades(wallet=wallet, limit=page_size, offset=offset)
                page_item_count = len(trades_page)

            if page_item_count == 0:
                break

            encountered_seen_trade = False

            for trade in trades_page:
                key = self._trade_key(trade)
                if key in seen:
                    encountered_seen_trade = True
                    continue

                if key in new_trade_keys:
                    continue

                new_trade_keys.add(key)
                new_trades.append(trade)

            if page_item_count < page_size:
                break

            if encountered_seen_trade:
                break

            offset += len(trades_page)
        else:
            logger.warning(
                f"Reached V2_TRADE_MAX_PAGES_PER_POLL={max_pages} for {wallet}; "
                "very high burst activity may still leave older trades for next cycle"
            )

        return new_trades

    async def _post_json(
        self,
        url: str,
        payload: Dict[str, Any],
        headers: Optional[Dict[str, str]] = None,
    ) -> Any:
        if not self.session:
            await self.initialize()

        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                async with self.session.post(url, json=payload, headers=headers) as response:
                    if response.status != 200:
                        text = await response.text()
                        logger.warning(f"POST {url} failed ({response.status}): {text[:180]}")
                        return None
                    return await response.json()
            except Exception as error:
                if attempt == max_attempts:
                    logger.error(f"Request error for {url}: {error}")
                    return None
                await asyncio.sleep(0.5 * attempt)

        return None

    async def get_analytics_global_range(self) -> Optional[Dict[str, Any]]:
        analytics_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://polymarketanalytics.com",
            "Referer": "https://polymarketanalytics.com/traders",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        }
        payload = {"getGlobalRange": True, "tag": "Overall"}
        data = await self._post_json(
            f"{self.analytics_api_base}/api/traders-tag-performance",
            payload,
            headers=analytics_headers,
        )
        return data if isinstance(data, dict) else None

    async def get_analytics_trader_rows(self) -> List[Dict[str, Any]]:
        ranges = await self.get_analytics_global_range()
        if not ranges:
            return []

        payload = {
            "tag": "Overall",
            "sortColumn": "overall_gain",
            "sortDirection": "DESC",
            "minPnL": ranges.get("minPnL"),
            "maxPnL": ranges.get("maxPnL"),
            "minActivePositions": ranges.get("minActivePositions"),
            "maxActivePositions": ranges.get("maxActivePositions"),
            "minWinAmount": ranges.get("minWinAmount"),
            "maxWinAmount": ranges.get("maxWinAmount"),
            "minLossAmount": ranges.get("minLossAmount"),
            "maxLossAmount": ranges.get("maxLossAmount"),
            "minWinRate": ranges.get("minWinRate"),
            "maxWinRate": ranges.get("maxWinRate"),
            "minCurrentValue": ranges.get("minCurrentValue"),
            "maxCurrentValue": ranges.get("maxCurrentValue"),
            "minTotalPositions": ranges.get("minTotalPositions"),
            "maxTotalPositions": ranges.get("maxTotalPositions"),
        }

        analytics_headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://polymarketanalytics.com",
            "Referer": "https://polymarketanalytics.com/traders",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        }

        data = await self._post_json(
            f"{self.analytics_api_base}/api/traders-tag-performance",
            payload,
            headers=analytics_headers,
        )
        if not isinstance(data, dict):
            return []

        rows = data.get("data")
        if not isinstance(rows, list):
            return []

        return rows[: self.candidate_limit]

    async def get_user_overall_profit(self, wallet: str) -> float:
        params = {
            "timePeriod": "all",
            "orderBy": "PNL",
            "limit": 1,
            "offset": 0,
            "category": "overall",
            "user": wallet,
        }
        data = await self._get_json(f"{self.data_api_base}/v1/leaderboard", params=params)
        if not isinstance(data, list) or not data:
            return 0.0
        return self._to_float(data[0].get("pnl"), default=0.0) or 0.0

    async def get_market_info(self, slug: str) -> Dict[str, Any]:
        cached = self.market_cache.get(slug)
        if cached:
            return cached

        data = await self._get_json(f"{self.gamma_api_base}/markets", params={"slug": slug})
        info = {
            "resolved": False,
            "winning_outcome": None,
            "category": "Unknown",
        }

        if isinstance(data, list) and data:
            market = data[0]
            outcomes = self._parse_list_field(market.get("outcomes"))
            prices = [self._to_float(value, default=0.0) or 0.0 for value in self._parse_list_field(market.get("outcomePrices"))]

            if outcomes and prices and len(outcomes) == len(prices):
                best_index = max(range(len(prices)), key=lambda index: prices[index])
                best_price = prices[best_index]
                if market.get("closed") and best_price >= 0.97:
                    info["resolved"] = True
                    info["winning_outcome"] = str(outcomes[best_index]).strip().lower()

            events = market.get("events") or []
            if events:
                tags = events[0].get("tags") or []
                if tags:
                    preferred_slugs = {
                        "sports",
                        "politics",
                        "crypto",
                        "business",
                        "world",
                        "technology",
                        "science",
                        "culture",
                        "movies",
                        "ai",
                    }
                    preferred_tag = next((tag for tag in tags if tag.get("slug") in preferred_slugs), tags[0])
                    info["category"] = preferred_tag.get("label") or "Unknown"

        self.market_cache[slug] = info
        return info

    async def get_trade_category(self, trade: Dict[str, Any]) -> str:
        event_slug = trade.get("eventSlug")
        market_keys = [
            trade.get("slug"),
            event_slug,
            trade.get("conditionId") or trade.get("condition_id"),
            trade.get("marketId") or trade.get("market_id"),
        ]
        market_keys = [str(value) for value in market_keys if value]

        for market_key in market_keys:
            cached = self.market_cache.get(market_key)
            if cached and cached.get("category"):
                return cached["category"]

        market = await self._fetch_market_for_trade(trade)
        category = self._extract_category_from_market(market)

        if category == "Other" and event_slug:
            event = await self._fetch_event_by_slug(event_slug)
            category = self._extract_category_from_event(event)

        if category == "Other":
            category = self._infer_category_from_text(trade.get("title") or "")

        for market_key in market_keys:
            cached = self.market_cache.get(market_key) or {"resolved": False, "winning_outcome": None}
            cached["category"] = category
            self.market_cache[market_key] = cached

        return category

    async def _fetch_market_for_trade(self, trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        lookups: List[Dict[str, Any]] = []

        slug = trade.get("slug")
        if slug:
            lookups.append({"slug": slug})

        condition_id = trade.get("conditionId") or trade.get("condition_id")
        if condition_id:
            lookups.append({"condition_id": condition_id})
            lookups.append({"conditionId": condition_id})

        market_id = trade.get("marketId") or trade.get("market_id")
        if market_id:
            lookups.append({"id": market_id})

        for params in lookups:
            data = await self._get_json(f"{self.gamma_api_base}/markets", params=params)
            if isinstance(data, list) and data:
                return data[0]

        return None

    async def _fetch_event_by_slug(self, event_slug: str) -> Optional[Dict[str, Any]]:
        data = await self._get_json(f"{self.gamma_api_base}/events", params={"slug": event_slug})
        if isinstance(data, list) and data:
            return data[0]
        return None

    @staticmethod
    def _extract_category_from_market(market: Optional[Dict[str, Any]]) -> str:
        if not market:
            return "Other"

        tags: List[Dict[str, Any]] = []
        if isinstance(market.get("tags"), list):
            tags.extend(tag for tag in market.get("tags") if isinstance(tag, dict))

        events = market.get("events") or []
        if isinstance(events, list) and events:
            first_event = events[0] if isinstance(events[0], dict) else {}
            event_tags = first_event.get("tags") or []
            if isinstance(event_tags, list):
                tags.extend(tag for tag in event_tags if isinstance(tag, dict))

        preferred_slugs = {
            "sports": "Sports",
            "politics": "Politics",
            "crypto": "Crypto",
            "business": "Business",
            "world": "World",
            "technology": "Technology",
            "science": "Science",
            "culture": "Culture",
            "movies": "Movies",
            "ai": "AI",
        }

        for tag in tags:
            slug = str(tag.get("slug") or "").strip().lower()
            if slug in preferred_slugs:
                return preferred_slugs[slug]

        for tag in tags:
            label = str(tag.get("label") or "").strip()
            if label:
                return label

        return "Other"

    @staticmethod
    def _extract_category_from_event(event: Optional[Dict[str, Any]]) -> str:
        if not event:
            return "Other"

        tags = event.get("tags") or []
        if not isinstance(tags, list):
            return "Other"

        preferred_slugs = {
            "sports": "Sports",
            "politics": "Politics",
            "crypto": "Crypto",
            "business": "Business",
            "world": "World",
            "technology": "Technology",
            "science": "Science",
            "culture": "Culture",
            "movies": "Movies",
            "ai": "AI",
        }

        for tag in tags:
            if not isinstance(tag, dict):
                continue
            slug = str(tag.get("slug") or "").strip().lower()
            if slug in preferred_slugs:
                return preferred_slugs[slug]

        for tag in tags:
            if not isinstance(tag, dict):
                continue
            label = str(tag.get("label") or "").strip()
            if label:
                return label

        return "Other"

    @staticmethod
    def _infer_category_from_text(text: str) -> str:
        value = (text or "").lower()
        if any(word in value for word in ["vs", "nba", "nfl", "mlb", "soccer", "tennis", "ufc", "nhl", "over/under"]):
            return "Sports"
        if any(word in value for word in ["election", "president", "senate", "house", "gop", "democrat", "trump", "biden"]):
            return "Politics"
        if any(word in value for word in ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "token"]):
            return "Crypto"
        if any(word in value for word in ["movie", "film", "oscar", "spirit awards", "actor", "actress"]):
            return "Movies"
        if any(word in value for word in ["ai", "openai", "anthropic", "chatgpt", "llm"]):
            return "AI"
        return "Other"

    async def compute_user_win_rate(self, trades: List[Dict[str, Any]]) -> Optional[float]:
        resolved_count = 0
        winning_count = 0

        for trade in trades:
            slug = trade.get("slug") or trade.get("eventSlug")
            if not slug:
                continue

            market_info = await self.get_market_info(slug)
            if not market_info.get("resolved"):
                continue

            winning_outcome = market_info.get("winning_outcome")
            trade_outcome = str(trade.get("outcome", "")).strip().lower()
            side = str(trade.get("side", "")).upper()

            if not winning_outcome or not trade_outcome or side not in {"BUY", "SELL"}:
                continue

            resolved_count += 1
            is_win = (trade_outcome == winning_outcome and side == "BUY") or (
                trade_outcome != winning_outcome and side == "SELL"
            )
            if is_win:
                winning_count += 1

        if resolved_count == 0:
            return None

        return (winning_count / resolved_count) * 100

    async def evaluate_candidate(self, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        wallet = row.get("proxyWallet") or row.get("trader")
        if not wallet:
            return None

        trades = await self.get_user_trades(wallet=wallet, limit=500)

        seven_days_ago = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp())
        weekly_trade_count = sum(1 for trade in trades if int(trade.get("timestamp", 0)) >= seven_days_ago)
        avg_trades_per_day = weekly_trade_count / 7

        if avg_trades_per_day < self.min_trades_per_day or avg_trades_per_day > self.max_trades_per_day:
            return None

        win_rate = self._to_float(row.get("winRate"), default=None)
        if win_rate is None:
            win_rate = self._to_float(row.get("win_rate"), default=None)

        if win_rate is not None and win_rate <= 1:
            win_rate *= 100

        if self.enforce_computed_win_rate:
            win_rate = await self.compute_user_win_rate(trades)
            if win_rate is None or win_rate < self.min_win_rate:
                return None
        else:
            # WIN_RATE leaderboard is primary source. If numeric value is present, enforce threshold.
            if win_rate is not None and win_rate < self.min_win_rate:
                return None

        overall_profit = await self.get_user_overall_profit(wallet)
        display_name = row.get("userName") or row.get("trader_name") or wallet

        return {
            "wallet": wallet,
            "display_name": display_name,
            "overall_profit": overall_profit,
            "avg_trades_per_day": avg_trades_per_day,
            "weekly_trade_count": weekly_trade_count,
            "win_rate": win_rate,
            "win_rate_source": "analytics" if win_rate is not None else "fallback",
        }

    async def select_top_users(self) -> List[Dict[str, Any]]:
        leaderboard_rows = await self.get_analytics_trader_rows()

        if not leaderboard_rows:
            logger.warning("Polymarket Analytics returned no rows; falling back to data-api WIN_RATE leaderboard")
            leaderboard_rows = []
            fetched = 0

            while fetched < self.candidate_limit:
                batch_limit = min(self.leaderboard_page_size, self.candidate_limit - fetched)
                batch = await self.get_leaderboard(
                    limit=batch_limit,
                    offset=fetched,
                    time_period="all",
                    order_by="WIN_RATE",
                )
                if not batch:
                    break

                leaderboard_rows.extend(batch)
                fetched += len(batch)

                if len(batch) < batch_limit:
                    break

        if not leaderboard_rows:
            logger.warning("No leaderboard rows available for candidate selection")
            return []

        semaphore = asyncio.Semaphore(3)

        async def evaluate_with_limit(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            async with semaphore:
                return await self.evaluate_candidate(candidate)

        evaluated = await asyncio.gather(*(evaluate_with_limit(row) for row in leaderboard_rows))
        filtered = [item for item in evaluated if item]
        filtered.sort(key=lambda item: item["overall_profit"], reverse=True)

        selected = filtered[: self.top_n]
        logger.info(f"Selected {len(selected)} users after WIN_RATE-first filtering and trade-frequency checks")
        return selected

    @staticmethod
    def _trade_key(trade: Dict[str, Any]) -> str:
        trade_id = trade.get("id")
        if trade_id is not None:
            return str(trade_id)

        tx_hash = trade.get("transactionHash") or ""
        timestamp = trade.get("timestamp") or ""
        asset = trade.get("asset") or ""
        outcome = trade.get("outcome") or ""
        side = trade.get("side") or ""
        size = trade.get("size") or ""
        price = trade.get("price") or ""
        return f"{tx_hash}:{timestamp}:{asset}:{outcome}:{side}:{size}:{price}"

    async def rebuild_daily_top_users(self):
        logger.info("Rebuilding daily top users list")
        selected = await self.select_top_users()

        self.selected_users = selected
        self.seen_trade_keys = {}

        for user in self.selected_users:
            wallet = user["wallet"]
            trades = await self.get_user_trades(wallet=wallet, limit=500)
            self.seen_trade_keys[wallet] = {self._trade_key(trade) for trade in trades}

        now = datetime.now(timezone.utc)
        self.next_rebuild_at = now + timedelta(seconds=self.daily_rebuild_seconds)

        lines = [
            f"Top-user rebuild timestamp (UTC): {now.isoformat()}",
            f"Selection filters: Polymarket Analytics trader candidates, numeric win-rate >= {self.min_win_rate:g}% when available, avg trades/day in [{self.min_trades_per_day:g}, {self.max_trades_per_day:g}], sorted by overall profit",
            f"Selected users: {len(self.selected_users)}",
            "",
        ]

        for rank, user in enumerate(self.selected_users, start=1):
            if user["win_rate"] is not None:
                win_rate_text = f"{user['win_rate']:.2f}% (analytics)"
            else:
                win_rate_text = "N/A (fallback source)"
            lines.append(
                f"[{rank}] {user['display_name']} ({user['wallet']}) | profit={user['overall_profit']:.2f} | "
                f"avgTrades/day={user['avg_trades_per_day']:.2f} | winRate={win_rate_text}"
            )

        lines.extend(["", "Live tweets (new trades only after this rebuild):", ""])
        self._write_output(lines, mode="w")

    def format_trade_for_tweet(self, trade: Dict[str, Any], user: Dict[str, Any], category: str) -> str:
        title = trade.get("title", "Unknown Market")
        outcome = trade.get("outcome", "Unknown")
        side = trade.get("side", "UNKNOWN")
        size = self._to_float(trade.get("size"), default=0.0) or 0.0
        price = self._to_float(trade.get("price"), default=0.0) or 0.0

        category_hashtag = "".join(ch for ch in category if ch.isalnum()) or "Prediction"

        return (
            "📊 Polymarket Trade Activity\n\n"
            f"Category: {category}\n"
            f"Trader: {user['display_name']}\n"
            f"{title}\n"
            f"{outcome}\n"
            f"{side} | Size: {size} @ ${price:.2f}\n\n"
            f"#Polymarket #{category_hashtag}"
        )

    async def poll_selected_users_once(self):
        if not self.selected_users:
            logger.warning("No selected users to poll")
            return

        generated = 0

        for user in self.selected_users:
            wallet = user["wallet"]
            seen = self.seen_trade_keys.setdefault(wallet, set())

            new_trades = await self._collect_new_trades_for_wallet(wallet=wallet, seen=seen)
            trades_sorted = sorted(new_trades, key=lambda t: int(t.get("timestamp", 0)))

            for trade in trades_sorted:
                key = self._trade_key(trade)
                if key in seen:
                    continue

                seen.add(key)

                category = await self.get_trade_category(trade)
                tweet = self.format_trade_for_tweet(trade, user, category)
                generated += 1

                if self.dry_run:
                    block = [
                        f"[{datetime.now(timezone.utc).isoformat()}] {wallet}",
                        tweet,
                        "---",
                    ]
                    self._write_output(block, mode="a")
                else:
                    if self.twitter_client:
                        await self.twitter_client.tweet(tweet)

        if generated:
            logger.info(f"Generated {generated} new tweet(s) from live polling")

    async def start(self):
        await self.initialize()
        try:
            await self.rebuild_daily_top_users()
            await self.poll_selected_users_once()

            if self.run_once:
                logger.info("V2_RUN_ONCE enabled. Exiting after first rebuild + poll.")
                return

            while True:
                now = datetime.now(timezone.utc)
                if self.next_rebuild_at and now >= self.next_rebuild_at:
                    await self.rebuild_daily_top_users()

                await self.poll_selected_users_once()
                await asyncio.sleep(self.trade_poll_seconds)
        finally:
            await self.shutdown()

    def _write_output(self, lines: List[str], mode: str):
        with open(self.output_file, mode, encoding="utf-8") as handle:
            handle.write("\n".join(lines))
            handle.write("\n")

    @staticmethod
    def _parse_list_field(value: Any) -> List[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return []

    @staticmethod
    def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _parse_bool(value: str) -> bool:
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}


async def main():
    bot = PolymarketTopUsersLiveBot()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())

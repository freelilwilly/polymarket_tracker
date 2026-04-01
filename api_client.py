"""
Polymarket API client for US trading and EU data monitoring.

Handles:
- US API: Trading, orders, positions (requires authentication)
- Gamma API: Market metadata
- CLOB API: Order books, market data
- Data API: Historical trades, user activity
- Analytics API: Trader performance
"""
import asyncio
import base64
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from typing import Any, Optional

import aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519

from config import Config
from slug_converter import SlugConverter
from utils import resolve_outcome_index, to_float

logger = logging.getLogger(__name__)


class PolymarketAPIClient:
    """Client for interacting with Polymarket APIs."""

    TEAM_ABBREVIATION_MAP: dict[str, str] = {
        "phx": "pho",
        "gnb": "gb",
        "kan": "kc",
        "nwe": "ne",
        "nor": "no",
        "sfo": "sf",
        "tam": "tb",
        "nyk": "ny",
        "wsh": "was"
    }
    # Reverse map for abbreviation lookup (e.g., "pho" -> "phx")
    TEAM_ABBREVIATION_REVERSE_MAP: dict[str, str] = {v: k for k, v in TEAM_ABBREVIATION_MAP.items()}

    STRIPPABLE_SLUG_PREFIXES: set[str] = {
        "aec",
        "asc",
        "asm",
        "acm",
        "acx",
    }
    
    def __init__(self, allow_order_execution: bool = True):
        """Initialize API client."""
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_order_error: Optional[dict[str, Any]] = None
        self.slug_converter = SlugConverter()
        self._market_info_cache: dict[str, dict[str, Any]] = {}
        self.allow_order_execution = bool(allow_order_execution)
        
        # US Trading Platform endpoints
        self.us_api_base = Config.US_API_BASE_URL
        
        # International/public endpoints (NOT geo-blocked for reading)
        self.gamma_api_base = Config.GAMMA_API_BASE_URL
        self.clob_api_base = Config.CLOB_API_BASE_URL
        self.data_api_base = Config.DATA_API_BASE_URL
        self.analytics_api_base = Config.ANALYTICS_API_BASE_URL
        
        # Parse private key only when real credentials are present.
        self.private_key: Optional[ed25519.Ed25519PrivateKey] = None
        self.api_key = Config.POLYMARKET_KEY_ID
        
        secret_candidate = (Config.POLYMARKET_SECRET_KEY or "").strip()
        looks_like_placeholder = (
            not secret_candidate
            or secret_candidate.startswith("your-")
            or "placeholder" in secret_candidate.lower()
        )

        if secret_candidate and not looks_like_placeholder:
            try:
                # US API secret is expected to be base64 and compatible with Ed25519.
                secret_bytes = base64.b64decode(secret_candidate)
                if len(secret_bytes) < 32:
                    raise ValueError(f"Invalid secret key length: {len(secret_bytes)} (expected >=32)")
                self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(secret_bytes[:32])
                logger.info("API credentials loaded successfully")
            except Exception as e:
                # Do not raise here so test mode can run with placeholder values.
                logger.warning(f"Failed to parse API credentials; authenticated endpoints disabled: {e}")

    @staticmethod
    def _normalize_slug_value(slug: str) -> str:
        value = str(slug or "").strip().lower()
        if not value:
            return ""

        parts = [p for p in value.split("-") if p]
        # Strip known API-side wrappers (for example aec-/asc-) while preserving
        # canonical market slugs like nba-... or btc-...
        while len(parts) > 1 and parts[0] in PolymarketAPIClient.STRIPPABLE_SLUG_PREFIXES:
            parts = parts[1:]

        return "-".join(parts)

    @staticmethod
    def _with_aec_prefix(slug: str) -> str:
        value = str(slug or "").strip().lower()
        if value.startswith("aec-"):
            return value
        return f"aec-{value}"

    def _apply_team_abbreviation_map(self, slug: str) -> str:
        base = self._normalize_slug_value(slug)
        if not base:
            return ""

        segments = base.split("-")
        transformed = [self.TEAM_ABBREVIATION_MAP.get(seg, seg) for seg in segments]
        return "-".join(transformed)

    def _apply_team_abbreviation_reverse_map(self, slug: str) -> str:
        base = self._normalize_slug_value(slug)
        if not base:
            return ""

        segments = base.split("-")
        transformed = [self.TEAM_ABBREVIATION_REVERSE_MAP.get(seg, seg) for seg in segments]
        return "-".join(transformed)

    def _generate_slug_candidates(self, market_slug: str) -> list[str]:
        """Generate deterministic slug candidates for metadata and US order endpoints."""
        base = self._normalize_slug_value(market_slug)
        if not base:
            return []

        ordered: list[str] = []
        seen: set[str] = set()

        def _add(value: str):
            candidate = str(value or "").strip().lower()
            if not candidate or candidate in seen:
                return
            seen.add(candidate)
            ordered.append(candidate)

        # Start with raw base and preferred US prefix variant.
        _add(base)
        _add(self._with_aec_prefix(base))

        # Learned EU -> US slug mapping should be attempted early.
        learned = self.slug_converter.get_learned_mapping(base)
        if learned:
            learned_base = self._normalize_slug_value(learned)
            _add(learned_base)
            _add(self._with_aec_prefix(learned_base))

        # Deterministic abbreviation substitution fallback.
        mapped = self._apply_team_abbreviation_map(base)
        if mapped and mapped != base:
            _add(mapped)
            _add(self._with_aec_prefix(mapped))

        # Reverse abbreviation mapping fallback.
        reverse_mapped = self._apply_team_abbreviation_reverse_map(base)
        if reverse_mapped and reverse_mapped != base:
            _add(reverse_mapped)
            _add(self._with_aec_prefix(reverse_mapped))

        # If learned mapping also has additional abbreviations, include it too.
        if learned:
            mapped_learned = self._apply_team_abbreviation_map(learned)
            if mapped_learned:
                _add(mapped_learned)
                _add(self._with_aec_prefix(mapped_learned))
            reverse_mapped_learned = self._apply_team_abbreviation_reverse_map(learned)
            if reverse_mapped_learned:
                _add(reverse_mapped_learned)
                _add(self._with_aec_prefix(reverse_mapped_learned))

        return ordered[:12]

    @staticmethod
    def _extract_error_message(raw_text: str) -> str:
        text = str(raw_text or "")
        if not text:
            return ""
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                message = payload.get("message") or payload.get("error")
                if message:
                    return str(message)
        except Exception:
            pass
        return text

    @staticmethod
    def _is_market_not_found_error(error: Any) -> bool:
        if isinstance(error, dict):
            raw = str(error.get("message") or error.get("raw_message") or "")
        else:
            raw = str(error or "")
        value = raw.lower()
        patterns = (
            "market not found",
            "market does not exist",
            "no such market",
            "unknown market",
            "asset not found",
            "conditionid not found",
        )
        return any(p in value for p in patterns)
    
    async def initialize(self):
        """Initialize HTTP session."""
        if not self.session:
            # trust_env=False prevents system proxy/auth from interfering
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                trust_env=False,
            )
            logger.info("API client initialized")
    
    async def shutdown(self):
        """Close HTTP session."""
        if self.session:
            await self.session.close()
            self.session = None
            logger.info("API client shut down")
    
    def _generate_signature(self, method: str, path: str) -> tuple[str, str]:
        """
        Generate Ed25519 signature for US API requests.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            path: API path (e.g., "/v1/account/balances")
            
        Returns:
            Tuple of (timestamp, signature)
        """
        if not self.private_key:
            raise ValueError("Private key not configured")
        
        timestamp = str(int(time.time() * 1000))
        
        # US API authentication: timestamp + method + path
        message = f"{timestamp}{method.upper()}{path}"
        signature_bytes = self.private_key.sign(message.encode())
        signature = base64.b64encode(signature_bytes).decode()
        
        return timestamp, signature
    
    async def _request(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
        auth_required: bool = False,
    ) -> Optional[Any]:
        """
        Make HTTP request with retries.
        
        Args:
            method: HTTP method
            url: Full URL
            headers: Request headers
            params: Query parameters
            json_data: JSON body
            auth_required: Whether to add US API authentication
            
        Returns:
            Response data or None on failure
        """
        if not self.session:
            await self.initialize()
        
        headers = headers or {}
        headers.setdefault("User-Agent", "polymarket-copytrade-bot/1.0")
        headers.setdefault("Accept", "application/json")
        
        # Add authentication if required
        if auth_required:
            if not self.api_key or not self.private_key:
                logger.warning("Authenticated request skipped: API credentials not configured")
                return None
            # US API authentication
            path = urlsplit(url).path
            timestamp, signature = self._generate_signature(method, path)

            headers["X-PM-Access-Key"] = self.api_key
            headers["X-PM-Timestamp"] = timestamp
            headers["X-PM-Signature"] = signature
            headers["Content-Type"] = "application/json"
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                async with self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    json=json_data,
                ) as response:
                    if response.status == 202:
                        wait_time = 1 + attempt
                        logger.warning(
                            f"Request accepted but not ready (202) for {url}; retrying in {wait_time}s"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    if 200 <= response.status < 300:
                        return await response.json()
                    elif response.status in (429, 503):
                        # Rate limit or service unavailable - retry with backoff
                        wait_time = 2 ** attempt
                        logger.warning(
                            f"Rate limited or service unavailable (attempt {attempt + 1}/{max_retries}). "
                            f"Waiting {wait_time}s..."
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    else:
                        text = await response.text()
                        request_path = urlsplit(url).path
                        if (
                            method.upper() == "GET"
                            and response.status == 404
                            and "/v1/order/" in request_path
                            and "order not found" in text.lower()
                        ):
                            # Newly-submitted orders can be briefly unavailable on details
                            # endpoint due to backend visibility lag.
                            logger.debug(
                                f"Order details not visible yet: {method} {url} -> {response.status}: {text}"
                            )
                        else:
                            logger.warning(
                                f"Request failed: {method} {url} -> {response.status}: {text}"
                            )
                        return None
                        
            except asyncio.TimeoutError:
                logger.warning(f"Request timeout (attempt {attempt + 1}/{max_retries}): {url}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientOSError,
                aiohttp.ServerDisconnectedError,
                ConnectionResetError,
                OSError,
            ) as e:
                if attempt < max_retries - 1:
                    base_wait = 2 ** attempt
                    wait_time = base_wait + random.uniform(0.0, 0.25)
                    logger.warning(
                        f"Transient connection error (attempt {attempt + 1}/{max_retries}): {e}. "
                        f"Retrying in {wait_time:.2f}s..."
                    )
                    await asyncio.sleep(wait_time)
                    continue

                logger.exception(f"Request error: {e}")
                return None
            except Exception as e:
                logger.exception(f"Request error: {e}")
                return None
        
        return None
    
    async def _get_json(
        self,
        url: str,
        params: Optional[dict] = None,
        auth_required: bool = False,
    ) -> Optional[Any]:
        """GET request returning JSON."""
        return await self._request("GET", url, params=params, auth_required=auth_required)
    
    async def _post_json(
        self,
        url: str,
        data: dict,
        auth_required: bool = False,
    ) -> Optional[Any]:
        """POST request with JSON body."""
        return await self._request("POST", url, json_data=data, auth_required=auth_required)

    async def _post_json_with_meta(
        self,
        url: str,
        data: dict,
        auth_required: bool = False,
    ) -> tuple[Optional[Any], Optional[dict[str, Any]]]:
        """POST request returning payload and failure metadata when available."""
        if not self.session:
            await self.initialize()

        headers: dict[str, str] = {
            "User-Agent": "polymarket-copytrade-bot/1.0",
            "Accept": "application/json",
        }

        if auth_required:
            if not self.api_key or not self.private_key:
                logger.warning("Authenticated request skipped: API credentials not configured")
                return None, {
                    "status": None,
                    "message": "API credentials not configured",
                    "url": url,
                }
            path = urlsplit(url).path
            timestamp, signature = self._generate_signature("POST", path)
            headers["X-PM-Access-Key"] = self.api_key
            headers["X-PM-Timestamp"] = timestamp
            headers["X-PM-Signature"] = signature
            headers["Content-Type"] = "application/json"

        max_retries = 3
        last_error: Optional[dict[str, Any]] = None
        for attempt in range(max_retries):
            try:
                async with self.session.request("POST", url, headers=headers, json=data) as response:
                    if response.status == 202:
                        wait_time = 1 + attempt
                        logger.warning(
                            f"Request accepted but not ready (202) for {url}; retrying in {wait_time}s"
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    if 200 <= response.status < 300:
                        payload = await response.json()
                        return payload, None

                    text = await response.text()
                    parsed_message = self._extract_error_message(text)
                    last_error = {
                        "status": response.status,
                        "message": parsed_message,
                        "raw_message": text,
                        "url": url,
                    }

                    if response.status in (429, 503) and attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(
                            f"Rate limited or service unavailable (attempt {attempt + 1}/{max_retries}). "
                            f"Waiting {wait_time}s..."
                        )
                        await asyncio.sleep(wait_time)
                        continue

                    # Suppress "market not found" warnings - these are handled by slug retry logic in place_order
                    is_market_not_found = self._is_market_not_found_error(last_error)
                    if not is_market_not_found:
                        logger.warning(
                            f"Request failed: POST {url} -> {response.status}: {text}"
                        )
                    else:
                        logger.debug(
                            f"Market not found (will retry with different slug): POST {url} -> {response.status}: {text}"
                        )
                    return None, last_error

            except asyncio.TimeoutError:
                last_error = {
                    "status": None,
                    "message": "timeout",
                    "url": url,
                }
                logger.warning(f"Request timeout (attempt {attempt + 1}/{max_retries}): {url}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                    continue
                return None, last_error
            except Exception as e:
                last_error = {
                    "status": None,
                    "message": str(e),
                    "url": url,
                }
                logger.exception(f"Request error: {e}")
                return None, last_error

        return None, last_error
    
    # ==================== Public API Methods (EU Trader Monitoring) ====================
    
    async def get_user_trades(
        self,
        wallet: str,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """
        Get recent trades for a user via Data API.
        
        Args:
            wallet: User wallet address
            limit: Maximum trades to return
            
        Returns:
            List of trade dicts
        """
        # Try activity endpoint first
        url = f"{self.data_api_base}/activity"
        params = {"user": wallet, "limit": limit}
        data = await self._get_json(url, params=params)
        
        if data and isinstance(data, list):
            return data
        
        # Fallback to trades endpoint
        url = f"{self.data_api_base}/trades"
        data = await self._get_json(url, params=params)
        
        return data if isinstance(data, list) else []

    async def get_user_positions(
        self,
        wallet: str,
        limit: int = 200,
    ) -> Optional[list[dict[str, Any]]]:
        """
        Get current public positions for a user via Data API.

        Args:
            wallet: User wallet address
            limit: Maximum positions to return

        Returns:
            List of position dicts, [] for successful empty response,
            or None when the API request fails/unavailable.
        """
        url = f"{self.data_api_base}/positions"
        params = {
            "user": wallet,
            "limit": max(1, int(limit)),
        }
        data = await self._get_json(url, params=params)

        if data is None:
            return None

        if isinstance(data, list):
            return data

        # Defensive parse for alternate envelope shape.
        if isinstance(data, dict):
            payload_positions = data.get("positions")
            if isinstance(payload_positions, list):
                return payload_positions

        logger.warning(
            f"Unexpected positions payload shape for wallet {wallet}: {type(data).__name__}"
        )
        return None

    async def get_recent_global_trades(self, limit: int = 1000) -> list[dict[str, Any]]:
        """
        Get recent global trades from Data API.

        Args:
            limit: Maximum trades to return

        Returns:
            List of trade dicts
        """
        url = f"{self.data_api_base}/trades"
        params = {"limit": max(1, int(limit))}
        data = await self._get_json(url, params=params)
        return data if isinstance(data, list) else []
    
    async def get_traders_performance(
        self,
        limit: int = 500,
        search_query: Optional[str] = None,
        apply_required_tags: bool = True,
    ) -> list[dict[str, Any]]:
        """
        Get trader performance data from Analytics API.
        
        Uses two-step process:
        1. GET global ranges
        2. GET traders with those ranges
        
        Args:
            limit: Maximum number of traders
            search_query: Optional free-text search for trader names/ids
            apply_required_tags: Whether to include REQUIRED_TRADER_TAGS in query params
            
        Returns:
            List of trader performance dicts
        """
        try:
            traders_url = f"{self.analytics_api_base}/traders-tag-performance"
            traders_params: list[tuple[str, str]] = [
                ("tag", "Overall"),
                ("sortDirection", "DESC"),
                ("limit", str(limit)),
                ("offset", "0"),
            ]

            if search_query:
                traders_params.append(("searchQuery", str(search_query)))

            # Mirror production frontend behavior: selectedTraderTags are repeated query params.
            if apply_required_tags:
                required_tags = [
                    t.strip() for t in (Config.REQUIRED_TRADER_TAGS or "").split(",") if t.strip()
                ]
                for tag in required_tags:
                    traders_params.append(("selectedTraderTags", tag))

            traders_data = await self._get_json(traders_url, params=traders_params)

            if not traders_data or not isinstance(traders_data, dict):
                logger.warning("Could not fetch traders from Analytics API")
                return []

            traders_list = traders_data.get("data")
            if not isinstance(traders_list, list):
                traders_list = traders_data.get("traders", [])
            
            if not isinstance(traders_list, list):
                return []
            
            # Convert to normalized format
            normalized_traders = []
            for trader in traders_list:
                if not isinstance(trader, dict):
                    continue
                
                normalized_traders.append({
                    "wallet": trader.get("wallet") or trader.get("address") or trader.get("trader"),
                    "display_name": trader.get("display_name") or trader.get("trader_name") or trader.get("name"),
                    "win_rate": to_float(trader.get("win_rate"), default=0.0),
                    "overall_gain": to_float(trader.get("overall_gain"), default=0.0),
                    "rank": int(to_float(trader.get("rank"), default=0.0)) or None,
                    "tags": trader.get("tags") or [],
                    "tag": trader.get("tag"),
                })
            
            return normalized_traders
            
        except Exception as e:
            logger.exception(f"Error fetching traders performance: {e}")
            return []
    
    async def get_market_info(self, market_slug: str) -> Optional[dict[str, Any]]:
        """
        Get market information from Gamma API.
        
        Args:
            market_slug: Market slug
            
        Returns:
            Market info dict or None
        """
        canonical_slug = self._normalize_slug_value(market_slug)
        cache_key = canonical_slug or str(market_slug or "").strip().lower()

        now = datetime.now(timezone.utc)
        cache_entry = self._market_info_cache.get(cache_key)
        if cache_entry:
            expires_at = cache_entry.get("expires_at")
            if isinstance(expires_at, datetime) and now < expires_at:
                cached_data = cache_entry.get("data")
                logger.debug(
                    f"Market info cache hit: cache_key={cache_key} | original={market_slug} | "
                    f"status={'positive' if cached_data else 'negative'}"
                )
                return cached_data

        candidate_slugs = self._generate_slug_candidates(market_slug)
        logger.debug(
            f"Market info lookup candidates: original={market_slug} | canonical={cache_key} | "
            f"candidates={candidate_slugs}"
        )
        
        data = None
        for candidate_slug in candidate_slugs:
            # Use query parameter ?slug= instead of path /markets/{slug}
            # Path parameter validates as UUID/ID, query parameter searches by slug
            url = f"{self.gamma_api_base}/markets"
            params = {"slug": candidate_slug}
            payload = await self._get_json(url, params=params)
            
            if payload:
                # Gamma API returns a list when using query parameters
                if isinstance(payload, list) and payload:
                    # Take first result (should only be one matching slug)
                    first = payload[0]
                    if isinstance(first, dict):
                        data = first
                        logger.debug(
                            f"Market info resolved from candidate: original={market_slug} | "
                            f"candidate={candidate_slug} | payload_type=list"
                        )
                        break
                elif isinstance(payload, dict):
                    data = payload
                    logger.debug(
                        f"Market info resolved from candidate: original={market_slug} | "
                        f"candidate={candidate_slug} | payload_type=dict"
                    )
                    break
                else:
                    logger.debug(
                        f"Market info candidate had unsupported payload shape: original={market_slug} | "
                        f"candidate={candidate_slug} | payload_type={type(payload).__name__}"
                    )
            else:
                logger.debug(
                    f"Market info candidate returned empty payload: original={market_slug} | "
                    f"candidate={candidate_slug}"
                )

        if not data:
            previous = cache_entry or {}
            fail_count = int(previous.get("fail_count") or 0) + 1
            last_warning_at = previous.get("last_warning_at")
            warning_cooldown = max(10, int(Config.MARKET_INFO_WARNING_COOLDOWN_SECONDS))
            warning_threshold = max(1, int(Config.MARKET_INFO_WARNING_THRESHOLD))
            should_warn = fail_count >= warning_threshold
            if isinstance(last_warning_at, datetime):
                since_last = (now - last_warning_at).total_seconds()
                if since_last < warning_cooldown:
                    should_warn = False

            if should_warn:
                logger.warning(
                    f"Market not found in Gamma API: original={market_slug}, "
                    f"canonical={cache_key}, tried={candidate_slugs}, failures={fail_count}"
                )
                last_warning_at = now
            else:
                logger.debug(
                    f"Market lookup unresolved (suppressed): original={market_slug}, "
                    f"canonical={cache_key}, tried={candidate_slugs}, failures={fail_count}"
                )

            negative_ttl = max(5, int(Config.MARKET_INFO_NEGATIVE_CACHE_SECONDS))
            self._market_info_cache[cache_key] = {
                "data": None,
                "expires_at": now + timedelta(seconds=negative_ttl),
                "fail_count": fail_count,
                "last_warning_at": last_warning_at,
            }
            return None
        
        # Parse outcomes and token IDs from JSON strings to create tokens array
        try:
            # Gamma API returns both outcomes and clobTokenIds as JSON strings
            outcomes_str = data.get("outcomes", "[]")
            clob_token_ids_str = data.get("clobTokenIds", "[]")
            
            # Parse JSON arrays
            outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str
            token_ids = json.loads(clob_token_ids_str) if isinstance(clob_token_ids_str, str) else clob_token_ids_str
            
            # Create tokens array
            if isinstance(token_ids, list) and isinstance(outcomes, list) and len(token_ids) == len(outcomes):
                data["tokens"] = [
                    {"token_id": tid, "outcome": outcome}
                    for tid, outcome in zip(token_ids, outcomes)
                ]
            else:
                logger.warning(f"Could not parse tokens for {market_slug}: token_ids={token_ids}, outcomes={outcomes}")
                data["tokens"] = []
        except Exception as e:
            logger.warning(f"Error parsing tokens for {market_slug}: {e}")
            data["tokens"] = []
        
        positive_ttl = max(5, int(Config.MARKET_INFO_CACHE_SECONDS))
        self._market_info_cache[cache_key] = {
            "data": data,
            "expires_at": now + timedelta(seconds=positive_ttl),
            "fail_count": 0,
            "last_warning_at": None,
        }

        return data
    
    async def get_order_book(self, market_slug: str, outcome: str = "yes") -> Optional[dict[str, Any]]:
        """
        Get order book for a specific market from CLOB API (public, not geo-blocked).
        
        Uses CLOB API which requires token_id, so we:
        1. Get market info from GAMMA API to get token IDs
        2. Resolve which token_id matches the outcome (default: "yes")
        3. Query CLOB orderbook with that token_id
        
        Args:
            market_slug: Market slug (e.g., "nhl-nj-dal-2026-03-24")
            outcome: Outcome to get book for ("yes" or "no"), default "yes"
            
        Returns:
            Order book dict with bids and asks or None if unavailable
        """
        # Step 1: Get market metadata from GAMMA API
        market_info = await self.get_market_info(market_slug)
        if not market_info:
            logger.debug(f"Cannot get market info for {market_slug}")
            return None
        
        # Step 2: Extract token IDs and outcomes
        tokens = market_info.get("tokens", [])
        if not tokens:
            logger.warning(f"No tokens found for {market_slug}")
            return None
        
        # Step 3: Resolve token index for the requested outcome with strict matching
        resolved_index = resolve_outcome_index(outcome, tokens)
        if resolved_index is None:
            logger.warning(
                f"Outcome '{outcome}' not resolvable for {market_slug}. "
                f"Available outcomes: {[t.get('outcome') for t in tokens]}"
            )
            return None

        token = tokens[resolved_index]
        token_id = token.get("token_id") or token.get("tokenId")
        
        if not token_id:
            logger.warning(f"Cannot extract token_id for {market_slug}")
            return None
        
        # Step 4: Query CLOB API orderbook (NOT geo-blocked for reading)
        url = f"{self.clob_api_base}/book"
        params = {"token_id": token_id}
        data = await self._get_json(url, params=params)
        return data if isinstance(data, dict) else None
    
    async def get_best_price(self, market_slug: str, side: str, outcome: str = "yes") -> Optional[float]:
        """
        Get best price from order book.
        
        Args:
            market_slug: Market slug
            side: "buy" or "sell"
            outcome: Outcome to get price for
            
        Returns:
            Best price as float or None
        """
        # get_market_info already handles aec- prefix logic, so just call it directly
        market_info = await self.get_market_info(market_slug)
        if not market_info:
            logger.debug(f"Cannot get market info for {market_slug}")
            return None
        
        tokens = market_info.get("tokens", [])
        if not tokens:
            logger.warning(f"No tokens found for {market_slug}")
            return None
        
        resolved_index = resolve_outcome_index(outcome, tokens)
        if resolved_index is None:
            logger.warning(f"Outcome '{outcome}' not resolvable for {market_slug}")
            return None
        
        token = tokens[resolved_index]
        token_id = token.get("token_id") or token.get("tokenId")
        
        if not token_id:
            logger.warning(f"Cannot extract token_id for {market_slug}")
            return None
        
        # Get order book from CLOB API
        url = f"{self.clob_api_base}/book"
        params = {"token_id": token_id}
        data = await self._get_json(url, params=params)
        
        if not isinstance(data, dict):
            logger.warning(f"No order book data for {market_slug}")
            return None
        
        # Extract best price from order book
        try:
            side_lower = side.lower()
            if side_lower == "buy":
                asks = data.get("asks", [])
                if asks and isinstance(asks, list):
                    prices = [float(ask.get("price", 0)) for ask in asks if ask.get("price")]
                    if prices:
                        return min(prices)
            else:
                bids = data.get("bids", [])
                if bids and isinstance(bids, list):
                    prices = [float(bid.get("price", 0)) for bid in bids if bid.get("price")]
                    if prices:
                        return max(prices)
        except Exception as e:
            logger.warning(f"Error extracting price from orderbook: {e}")
            return None
        
        logger.warning(f"No valid prices in order book for {market_slug}")
        return None
    
    # ==================== US Trading API Methods (Authentication Required) ====================

    async def get_account_overview(self) -> Optional[dict[str, float]]:
        """
        Get account overview values from US API balances endpoint.

        Returns:
            Dict with buying_power, current_balance, asset_notional, total_account_value
            or None on error.
        """
        url = f"{self.us_api_base}/v1/account/balances"
        data = await self._get_json(url, auth_required=True)

        if not data or not isinstance(data, dict):
            return None

        balances = data.get("balances")
        if not isinstance(balances, list) or not balances:
            return None

        primary = balances[0] if isinstance(balances[0], dict) else {}
        buying_power = to_float(primary.get("buyingPower"), default=0.0)
        current_balance = to_float(primary.get("currentBalance"), default=0.0)
        asset_notional = to_float(primary.get("assetNotional"), default=0.0)

        # Equity-like sizing base: available cash plus marked asset notional.
        total_account_value = current_balance + asset_notional
        if total_account_value <= 0 and buying_power > 0:
            total_account_value = buying_power

        return {
            "buying_power": buying_power,
            "current_balance": current_balance,
            "asset_notional": asset_notional,
            "total_account_value": total_account_value,
        }
    
    async def get_balance(self) -> Optional[float]:
        """
        Get account balance from US API.

        For trading risk sizing this returns total account value (equity-like),
        not just immediate buying power.
        
        Returns:
            Balance as float or None on error
        """
        overview = await self.get_account_overview()
        if overview:
            total = overview.get("total_account_value")
            if total is not None and total > 0:
                return total
            buying_power = overview.get("buying_power")
            if buying_power is not None and buying_power > 0:
                return buying_power
        
        return None
    
    async def get_positions(self) -> Optional[list[dict[str, Any]]]:
        """
        Get open positions from US API.
        
        Returns:
            List of position dicts or None on error
        """
        url = f"{self.us_api_base}/v1/portfolio/positions"
        data = await self._get_json(url, auth_required=True)

        if not data:
            logger.warning("get_positions() received None from API")
            return None

        if data and isinstance(data, dict):
            positions = data.get("positions", [])

            def _normalize_position(market_slug: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
                if not market_slug:
                    return None

                metadata = payload.get("marketMetadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                outcome = metadata.get("outcome") or payload.get("outcome") or ""

                # US API can report side exposure with signed quantities for one outcome
                # (e.g., netPosition=-100 for the opposite side). For holdings/position
                # tracking, we need the magnitude of exposure, not the sign.
                qty_available = to_float(payload.get("qtyAvailable"), default=0.0)
                signed_size = to_float(
                    payload.get("size")
                    if payload.get("size") is not None
                    else payload.get("netPosition"),
                    default=0.0,
                )
                qty_bought = to_float(payload.get("qtyBought"), default=0.0)
                qty_sold = to_float(payload.get("qtySold"), default=0.0)

                if abs(qty_available) > 0:
                    long_size = abs(qty_available)
                elif abs(signed_size) > 0:
                    long_size = abs(signed_size)
                else:
                    # Fallback to net traded quantity when explicit position fields are absent.
                    long_size = abs(qty_bought - qty_sold)

                # Keep as warning because this can indicate API schema drift or parsing bugs.
                if long_size <= 0:
                    logger.warning(
                        f"FILTERING OUT POSITION (long_size={long_size:.2f}): market={market_slug}, "
                        f"qtyAvailable={payload.get('qtyAvailable')}, size={payload.get('size')}, "
                        f"netPosition={payload.get('netPosition')}, qtyBought={payload.get('qtyBought')}, "
                        f"outcome={outcome}"
                    )
                    return None

                return {
                    "marketSlug": market_slug,
                    "outcome": outcome,
                    "size": long_size,
                    "raw": payload,
                }

            if isinstance(positions, list):
                normalized_positions: list[dict[str, Any]] = []
                for payload in positions:
                    if not isinstance(payload, dict):
                        continue
                    market_slug = str(payload.get("marketSlug") or payload.get("market_slug") or "").strip()
                    normalized = _normalize_position(market_slug, payload)
                    if normalized is not None:
                        normalized_positions.append(normalized)
                logger.debug(f"Retrieved {len(normalized_positions)} open positions from API")
                return normalized_positions

            if isinstance(positions, dict):
                normalized_positions: list[dict[str, Any]] = []
                for market_slug, payload in positions.items():
                    if not isinstance(payload, dict):
                        continue
                    normalized = _normalize_position(str(market_slug).strip(), payload)
                    if normalized is not None:
                        normalized_positions.append(normalized)
                logger.debug(f"Retrieved {len(normalized_positions)} open positions from API")
                return normalized_positions
        
        return None
    
    async def place_order(
        self,
        market_slug: str,
        outcome: str,
        side: str,
        shares: float,
        price: float,
        tif: str = "TIME_IN_FORCE_GOOD_TILL_CANCEL",
        order_type: str = "ORDER_TYPE_LIMIT",
        convert_no_price: bool = True,
    ) -> Optional[dict[str, Any]]:
        """
        Place an order via US API.
        
        Args:
            market_slug: Market slug
            outcome: Outcome ("yes" or "no")
            side: "BUY" or "SELL"
            shares: Number of shares
            price: Limit price
            tif: Time in force enum
            order_type: Order type enum
            
        Returns:
            Order result dict or None on failure
        """
        url = f"{self.us_api_base}/v1/orders"
        self.last_order_error = None

        if not self.allow_order_execution:
            self.last_order_error = {
                "status": None,
                "message": "ORDER_EXECUTION_DISABLED",
                "market_slug": market_slug,
                "side": side,
            }
            logger.warning(
                f"Order execution disabled by client safety guard: {side} {market_slug}"
            )
            return None

        outcome_lower = str(outcome or "").strip().lower()
        if outcome_lower not in ("yes", "no"):
            normalized = await self.normalize_outcome_to_yes_no(market_slug, outcome)
            if not normalized:
                logger.warning(
                    f"Cannot place order: outcome '{outcome}' could not be normalized for {market_slug}"
                )
                return None
            outcome_lower = normalized

        side_upper = side.upper()

        candidate_slugs = self._generate_slug_candidates(market_slug)
        if not candidate_slugs:
            logger.warning(f"Cannot place order with empty market slug: {market_slug}")
            return None

        input_slug_base = self._normalize_slug_value(market_slug)

        last_market_not_found_error = None
        for candidate_slug in candidate_slugs:
            # ORDER_INTENT_BUY_LONG: Buy YES tokens (index 0) - creates LONG YES position
            # ORDER_INTENT_BUY_SHORT: Buy NO tokens (index 1) - creates LONG NO position (NOT a leveraged short!)
            if side_upper == "BUY":
                if outcome_lower == "yes":
                    order_intent = "ORDER_INTENT_BUY_LONG"
                else:
                    order_intent = "ORDER_INTENT_BUY_SHORT"
            else:
                if outcome_lower == "yes":
                    order_intent = "ORDER_INTENT_SELL_LONG"
                else:
                    order_intent = "ORDER_INTENT_SELL_SHORT"

            if outcome_lower == "no" and convert_no_price:
                adj_price = max(0.01, min(0.99, 1.0 - price))
            else:
                adj_price = max(0.01, min(0.99, price))

            shares_val = max(0.0, float(shares))
            if side_upper == "SELL":
                quantity = int(shares_val)
            else:
                quantity = int(round(shares_val))

            if quantity <= 0:
                logger.warning(
                    f"Refusing to place {side_upper} with non-positive quantity: shares={shares_val:.6f}"
                )
                return None

            order_data = {
                "marketSlug": candidate_slug,
                "type": order_type,
                "price": {"value": f"{adj_price:.2f}", "currency": "USD"},
                "quantity": quantity,
                "tif": tif,
                "intent": order_intent,
                "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
                "participateDontInitiate": False,
            }

            result, order_error = await self._post_json_with_meta(url, order_data, auth_required=True)
            if order_error:
                self.last_order_error = order_error
            if result and isinstance(result, dict):
                # Check for error or rejected status in the response
                status = result.get("status") or result.get("orderStatus")
                error_msg = result.get("error") or result.get("errorMessage") or result.get("message")
                
                # If there's an error message in the response, treat as failure
                if error_msg and ("error" in str(error_msg).lower() or "reject" in str(error_msg).lower()):
                    logger.warning(
                        f"Order rejected: {market_slug} | {outcome} | {side} | "
                        f"{shares} @ ${price:.2f} - Error: {error_msg}"
                    )
                    return None
                
                # Log full response for debugging
                order_id = result.get("orderId") or result.get("order_id") or result.get("id")
                if order_id:
                    # Log the full result to see what the API actually returned
                    logger.debug(f"Order placed - Full API response: {result}")
                    used_slug_base = self._normalize_slug_value(candidate_slug)
                    if used_slug_base and used_slug_base != input_slug_base:
                        self.slug_converter.learn_mapping(input_slug_base, used_slug_base)
                        logger.info(
                            f"Learned slug mapping from successful order: {input_slug_base} -> {used_slug_base}"
                        )
                    return {
                        "order_id": order_id,
                        "result": result,
                        "market_slug_used": candidate_slug,
                    }
            # If market not found, try next candidate
            if order_error and self._is_market_not_found_error(order_error):
                last_market_not_found_error = order_error
                continue
            # For other errors, break
            break
        
        # If all candidates failed with "market not found", log warning
        if last_market_not_found_error:
            last_market_not_found_error["candidate_slugs"] = candidate_slugs
            self.last_order_error = last_market_not_found_error
            logger.warning(
                f"Market not available on US API (tried {candidate_slugs}): {market_slug}"
            )
        
        return None
    
    async def cancel_order(self, order_id: str, market_slug: Optional[str] = None) -> bool:
        """
        Cancel an order via US API.
        
        Args:
            order_id: Order ID to cancel
            market_slug: Market slug for cancel payload (optional)
            
        Returns:
            True if successful, False otherwise
        """
        url = f"{self.us_api_base}/v1/order/{order_id}/cancel"

        slug = str(market_slug or "").strip()
        if not slug:
            details = await self.get_order_details(order_id)
            if isinstance(details, dict):
                slug = str(details.get("marketSlug") or details.get("market_slug") or "").strip()

        if not slug:
            # Try without market slug in payload
            result, _ = await self._post_json_with_meta(url, {}, auth_required=True)
            return result is not None

        # Always try both aec-<slug> and <slug> for US API
        candidate_slugs = [slug]
        if slug.startswith("aec-"):
            # If slug already has aec- prefix, also try without it
            candidate_slugs.append(slug[4:])  # Remove "aec-" prefix
        else:
            # If slug doesn't have prefix, also try with it
            candidate_slugs.append(f"aec-{slug}")

        last_error: Optional[dict[str, Any]] = None
        for candidate_slug in candidate_slugs:
            payload: dict[str, Any] = {"marketSlug": candidate_slug}
            result, cancel_error = await self._post_json_with_meta(url, payload, auth_required=True)
            if cancel_error:
                last_error = cancel_error
            
            # US API may return an empty JSON object on successful cancel.
            if result is not None:
                return True
        
        # All candidates failed
        if last_error:
            logger.warning(
                f"Cancel failed for order {order_id} (tried {candidate_slugs}): "
                f"status={last_error.get('status')} message={last_error.get('message')}"
            )
        return False
    
    async def get_orders(self) -> Optional[list[dict[str, Any]]]:
        """
        Get all open orders from US API.
        
        Returns:
            List of order dicts or None on error
        """
        url = f"{self.us_api_base}/v1/orders/open"
        data = await self._get_json(url, auth_required=True)
        
        if data is None:
            return None
        
        if isinstance(data, dict):
            orders = data.get("orders", [])
            if isinstance(orders, list):
                return orders
        
        return None
    
    async def get_order_details(self, order_id: str) -> Optional[dict[str, Any]]:
        """
        Get details for a specific order.
        
        Args:
            order_id: Order ID
            
        Returns:
            Order details dict or None
        """
        url = f"{self.us_api_base}/v1/order/{order_id}"
        data = await self._get_json(url, auth_required=True)
        
        return data if isinstance(data, dict) else None
    
    async def normalize_outcome_to_yes_no(
        self,
        market_slug: str,
        outcome: str,
        strict: bool = False,
        allow_fuzzy: Optional[bool] = None,
        caller_context: Optional[str] = None,
    ) -> Optional[str]:
        # Docstring removed to fix unterminated triple-quoted string error
        from utils import normalize_outcome_to_yes_no
        return await normalize_outcome_to_yes_no(
            self,
            market_slug,
            outcome,
            logger,
            strict=strict,
            allow_fuzzy=allow_fuzzy,
            caller_context=caller_context,
        )


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
import time
from urllib.parse import urlsplit
from typing import Any, Optional

import aiohttp
from cryptography.hazmat.primitives.asymmetric import ed25519

from config import Config
from utils import resolve_outcome_index, to_float

logger = logging.getLogger(__name__)


class PolymarketAPIClient:
    """Client for interacting with Polymarket APIs."""
    
    def __init__(self):
        """Initialize API client."""
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_order_error: Optional[dict[str, Any]] = None
        
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
                    last_error = {
                        "status": response.status,
                        "message": text,
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

                    logger.warning(
                        f"Request failed: POST {url} -> {response.status}: {text}"
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
        def _build_candidate_slugs(slug: str) -> list[str]:
            candidates: list[str] = []

            def _add(value: str) -> None:
                normalized = str(value or "").strip()
                if normalized and normalized not in candidates:
                    candidates.append(normalized)

            _add(slug)
            base_slug = slug[4:] if slug.startswith("aec-") else slug
            _add(base_slug)

            # Heuristic variant generation for some US aec abbreviations that
            # differ from Gamma slug team codes (e.g., nba-sa-... -> nba-sas-...).
            parts = base_slug.split("-")
            date_idx = None
            for idx, token in enumerate(parts):
                if len(token) == 4 and token.isdigit():
                    date_idx = idx
                    break

            if date_idx is not None and date_idx > 1:
                pre_date = parts[:date_idx]
                post_date = parts[date_idx:]
                for i in range(1, len(pre_date)):
                    token = pre_date[i]
                    if len(token) == 2 and token.isalpha():
                        variant = pre_date.copy()
                        variant[i] = f"{token}s"
                        _add("-".join(variant + post_date))

            return candidates

        candidate_slugs = _build_candidate_slugs(market_slug)

        data: Optional[dict[str, Any]] = None
        url = f"{self.gamma_api_base}/markets"
        for slug in candidate_slugs:
            response = await self._get_json(url, params={"slug": slug, "limit": 1})
            if isinstance(response, list) and response:
                first = response[0]
                if isinstance(first, dict):
                    data = first
                    break
            if isinstance(response, dict):
                payload = response.get("data")
                if isinstance(payload, list) and payload:
                    first = payload[0]
                    if isinstance(first, dict):
                        data = first
                        break

        if not data:
            return None
        
        # Parse clobTokenIds and outcomes from JSON strings to create tokens array
        try:
            clob_token_ids_str = data.get("clobTokenIds", "[]")
            outcomes_str = data.get("outcomes", "[]")
            
            # Parse JSON arrays
            token_ids = json.loads(clob_token_ids_str) if isinstance(clob_token_ids_str, str) else clob_token_ids_str
            outcomes = json.loads(outcomes_str) if isinstance(outcomes_str, str) else outcomes_str
            
            # Create tokens array
            if isinstance(token_ids, list) and isinstance(outcomes, list) and len(token_ids) == len(outcomes):
                data["tokens"] = [
                    {"token_id": tid, "outcome": outcome}
                    for tid, outcome in zip(token_ids, outcomes)
                ]
            else:
                logger.warning(f"Could not parse tokens for {market_slug}")
                data["tokens"] = []
        except Exception as e:
            logger.warning(f"Error parsing tokens for {market_slug}: {e}")
            data["tokens"] = []
        
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
            logger.warning(f"Cannot get market info for {market_slug}")
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
            outcome: Outcome (default: "yes")
            
        Returns:
            Best price or None
        """
        book = await self.get_order_book(market_slug, outcome)
        if not book:
            return None
        
        try:
            def _extract_prices(levels: list[dict[str, Any]]) -> list[float]:
                prices: list[float] = []
                for level in levels:
                    if not isinstance(level, dict):
                        continue
                    p = to_float(level.get("price"), default=-1.0)
                    if 0.0 <= p <= 1.0:
                        prices.append(p)
                return prices

            if side.lower() == "buy":
                # For buying, we want the best ask (lowest available ask).
                asks = book.get("asks", [])
                prices = _extract_prices(asks if isinstance(asks, list) else [])
                if prices:
                    return min(prices)
            else:
                # For selling, we want the best bid (highest available bid).
                bids = book.get("bids", [])
                prices = _extract_prices(bids if isinstance(bids, list) else [])
                if prices:
                    return max(prices)
        except Exception as e:
            logger.warning(f"Error extracting price from orderbook: {e}")
        
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

        if data and isinstance(data, dict):
            positions = data.get("positions", [])

            def _normalize_position(market_slug: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
                if not market_slug:
                    return None

                metadata = payload.get("marketMetadata")
                metadata = metadata if isinstance(metadata, dict) else {}
                outcome = metadata.get("outcome") or payload.get("outcome") or ""

                # Use available long quantity when provided. If missing, infer from signed
                # position fields but never treat negative (short) balances as sellable longs.
                qty_available = to_float(payload.get("qtyAvailable"), default=0.0)
                if qty_available > 0:
                    long_size = qty_available
                else:
                    signed_size = to_float(
                        payload.get("size")
                        if payload.get("size") is not None
                        else payload.get("netPosition"),
                        default=0.0,
                    )
                    long_size = signed_size if signed_size > 0 else 0.0
                    if long_size <= 0:
                        qty_bought = to_float(payload.get("qtyBought"), default=0.0)
                        long_size = qty_bought if qty_bought > 0 else 0.0

                if long_size <= 0:
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

        outcome_lower = str(outcome or "").strip().lower()
        if outcome_lower not in ("yes", "no"):
            normalized = await self.normalize_outcome_to_yes_no(market_slug, outcome)
            if not normalized:
                logger.warning(
                    f"Cannot place order: outcome '{outcome}' could not be normalized for {market_slug}"
                )
                return None
            outcome_lower = normalized
        
        # Determine order intent based on outcome and side
        side_upper = side.upper()
        
        # ORDER_INTENT_BUY_LONG: Buy YES tokens (index 0) - creates LONG YES position
        # ORDER_INTENT_BUY_SHORT: Buy NO tokens (index 1) - creates LONG NO position (NOT a leveraged short!)
        # Note: Despite the confusing name "BUY_SHORT", this is NOT a short position.
        # It's a regular LONG position on the NO-side outcome.
        if side_upper == "BUY":
            if outcome_lower == "yes":
                order_intent = "ORDER_INTENT_BUY_LONG"  # Buy YES
            else:
                order_intent = "ORDER_INTENT_BUY_SHORT"  # Buy NO (LONG position on NO)
        else:
            # SELL reduces existing position regardless of which side (YES or NO)
            if outcome_lower == "yes":
                order_intent = "ORDER_INTENT_SELL_LONG"  # Sell YES
            else:
                order_intent = "ORDER_INTENT_SELL_SHORT"  # Sell NO
        
        # US API requires YES-basis pricing for both BUY_SHORT and SELL_SHORT intents.
        if outcome_lower == "no":
            price = max(0.01, min(0.99, 1.0 - price))
        else:
            price = max(0.01, min(0.99, price))

        shares = max(0.0, float(shares))
        if side_upper == "SELL":
            # Never round up sells; rounding up can exceed holdings and create
            # unintended opposite exposure on some venues.
            quantity = int(shares)
        else:
            quantity = int(round(shares))

        if quantity <= 0:
            logger.warning(
                f"Refusing to place {side_upper} with non-positive quantity: shares={shares:.6f}"
            )
            return None
        
        order_data = {
            "marketSlug": market_slug,
            "type": order_type,
            "price": {"value": f"{price:.2f}", "currency": "USD"},
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
            order_id = result.get("orderId") or result.get("order_id") or result.get("id")
            if order_id:
                return {"order_id": order_id, "result": result}
        
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
        payload: dict[str, Any] = {}

        slug = str(market_slug or "").strip()
        if not slug:
            details = await self.get_order_details(order_id)
            if isinstance(details, dict):
                slug = str(details.get("marketSlug") or details.get("market_slug") or "").strip()

        if slug:
            payload["marketSlug"] = slug

        result = await self._post_json(url, payload, auth_required=True)

        # US API may return an empty JSON object on successful cancel.
        # Treat any non-None response as success.
        return result is not None
    
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
    ) -> Optional[str]:
        """
        Normalize arbitrary outcome text to 'yes' or 'no' for binary markets.
        
        Args:
            market_slug: Market slug
            outcome: Outcome text to normalize
            
        Returns:
            'yes' or 'no' if resolved, None otherwise
        """
        from utils import normalize_outcome_to_yes_no
        return await normalize_outcome_to_yes_no(
            self,
            market_slug,
            outcome,
            logger,
            strict=strict,
            allow_fuzzy=allow_fuzzy,
        )

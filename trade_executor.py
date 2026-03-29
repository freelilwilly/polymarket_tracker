"""Trade execution logic with price validation."""
import logging
from typing import Any, Optional

from api_client import PolymarketAPIClient
from config import Config
from utils import to_float

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Executes trades with price validation and size normalization."""
    
    def __init__(self, api_client: PolymarketAPIClient, test_mode: bool = True):
        """
        Initialize trade executor.
        
        Args:
            api_client: API client for trade execution
            test_mode: If True, simulates trades without actual execution
        """
        self.api_client = api_client
        self.test_mode = test_mode
        self.max_price_tolerance = Config.MAX_PRICE_TOLERANCE

    @staticmethod
    def _to_us_short_price(price: float) -> float:
        """Convert NO-side price to US API short-price basis (YES-side complement)."""
        converted = 1.0 - float(price)
        return max(0.01, min(0.99, converted))

    @staticmethod
    def _normalize_slug_for_compare(slug: str) -> str:
        value = str(slug or "").strip().lower()
        if value.startswith("aec-"):
            return value
        return f"aec-{value}"

    async def _market_position_size(self, market_slug: str) -> float:
        """Get current total position size for the market across outcomes."""
        positions = await self.api_client.get_positions() or []
        needle = self._normalize_slug_for_compare(market_slug)
        total = 0.0
        for position in positions:
            slug = self._normalize_slug_for_compare(position.get("marketSlug", ""))
            if slug == needle:
                total += abs(to_float(position.get("size"), default=0.0))
        return total

    async def _get_live_position_size(self, market_slug: str, outcome: str) -> Optional[float]:
        """
        Get current position size for a specific market/outcome from live API.
        
        Returns None if position not found or API error (fail-open for safety).
        Returns 0.0 if position exists but size is zero.
        """
        try:
            positions = await self.api_client.get_positions() or []
            normalized_slug = self._normalize_slug_for_compare(market_slug)

            # Normalize requested outcome to canonical yes/no when possible.
            requested_outcome = await self.api_client.normalize_outcome_to_yes_no(market_slug, outcome)
            if not requested_outcome:
                requested_outcome = str(outcome or "").strip().lower()
            
            for position in positions:
                pos_slug = self._normalize_slug_for_compare(position.get("marketSlug", ""))
                if pos_slug != normalized_slug:
                    continue

                pos_outcome_raw = str(position.get("outcome", "")).strip()
                pos_outcome = await self.api_client.normalize_outcome_to_yes_no(market_slug, pos_outcome_raw)
                if not pos_outcome:
                    pos_outcome = pos_outcome_raw.lower()

                if pos_outcome == requested_outcome:
                    # Found matching position - return absolute size
                    # (API returns netPosition which can be negative for SHORTs)
                    size = abs(to_float(position.get("size"), default=0.0))
                    return size
            
            # Position not found - return 0.0 (safe to skip sell)
            return 0.0
            
        except Exception as e:
            logger.exception(f"Error checking live position size for {market_slug} | {outcome}: {e}")
            # Fail open - return None to skip sell if API check fails
            return None

    async def execute_buy(
        self,
        market_slug: str,
        observed_price: float,
        target_shares: float,
        outcome: str,
    ) -> Optional[dict[str, Any]]:
        """
        Execute a BUY order with price validation.
        
        Args:
            market_slug: US market slug
            observed_price: Price from the copied trade
            target_shares: Number of shares to buy
            outcome: Outcome to buy (e.g., "yes", "no", "Lakers")
            
        Returns:
            Dict with execution details or None if failed
        """
        try:
            # Normalize outcome to yes/no for US API
            normalized_outcome = await self.api_client.normalize_outcome_to_yes_no(
                market_slug, outcome
            )
            
            if not normalized_outcome:
                logger.warning(f"Could not normalize outcome '{outcome}' for {market_slug}")
                return {"skipped": True, "reason": "OUTCOME_NORMALIZATION_FAILED"}
            
            # Get current market price
            current_price = await self.api_client.get_best_price(
                market_slug, side="buy", outcome=normalized_outcome
            )
            
            if current_price is None:
                logger.warning(f"Could not get current price for {market_slug}")
                return {"skipped": True, "reason": "NO_PRICE"}
            
            # Check price tolerance
            price_diff = abs(current_price - observed_price)
            if price_diff > self.max_price_tolerance:
                logger.info(
                    f"Price moved outside tolerance: {market_slug} | "
                    f"observed={observed_price:.4f}, current={current_price:.4f}, "
                    f"diff={price_diff:.4f} > {self.max_price_tolerance:.4f}"
                )
                return {
                    "skipped": True,
                    "reason": "PRICE_TOLERANCE",
                    "current_price": current_price,
                }
            
            # In test mode, just return success without executing
            if self.test_mode:
                logger.info(
                    f"TEST MODE: Would buy {target_shares:.2f} shares of {market_slug} "
                    f"at {current_price:.4f}"
                )
                return {
                    "success": True,
                    "current_price": current_price,
                    "shares": target_shares,
                }
            
            # Execute actual buy order (LIVE MODE)
            # Determine if this is a NO-side trade
            is_no_side = normalized_outcome.lower() == "no"
            
            if is_no_side and not Config.ALLOW_BUY_SHORT:
                logger.info(f"Skipping NO-side trade for {market_slug} (ALLOW_BUY_SHORT=false)")
                return {"skipped": True, "reason": "NO_SIDE_DISABLED"}
            
            # Place order via US API
            order_result = await self.api_client.place_order(
                market_slug=market_slug,
                outcome=normalized_outcome,
                side="BUY",
                shares=target_shares,
                price=current_price,
            )
            
            if not order_result:
                order_error = self.api_client.last_order_error or {}
                status = order_error.get("status")
                message = str(order_error.get("message") or "").lower()

                market_unavailable_reasons = []
                if "market not found" in message:
                    market_unavailable_reasons.append("MARKET_NOT_FOUND")
                if "not tradable" in message:
                    market_unavailable_reasons.append("NOT_TRADABLE")
                if "asset not found" in message:
                    market_unavailable_reasons.append("ASSET_NOT_FOUND")

                # "symbol is required" has proven to be noisy/ambiguous in the US API;
                # do not treat it as definitive market-unavailable signal.
                if status in (400, 404) and market_unavailable_reasons:
                    logger.info(
                        f"US market unavailable for trading, skipping: {market_slug} | "
                        f"status={status} | reasons={','.join(market_unavailable_reasons)}"
                    )
                    return {
                        "skipped": True,
                        "reason": "US_MARKET_UNAVAILABLE",
                        "status": status,
                        "market_unavailable_reasons": market_unavailable_reasons,
                    }

                logger.warning(f"Order placement failed for {market_slug}")
                return None
            
            logger.info(
                f"BUY executed: {market_slug} | {target_shares:.2f} shares "
                f"@ {current_price:.4f}"
            )
            
            return {
                "success": True,
                "current_price": current_price,
                "shares": target_shares,
                "order_id": order_result.get("order_id"),
            }
            
        except Exception as e:
            logger.exception(f"Error executing buy for {market_slug}: {e}")
            return None

    async def execute_sell(
        self,
        market_slug: str,
        shares: float,
        outcome: str,
        price: Optional[float] = None,
        allow_full_liquidation_on_oversell: bool = False,
        treat_as_market: bool = False,
    ) -> Optional[dict[str, Any]]:
        """
        Execute a SELL order.
        
        Args:
            market_slug: US market slug
            shares: Number of shares to sell
            outcome: Outcome to sell
            price: Optional limit price (if None, uses market price)
            allow_full_liquidation_on_oversell: If True, cap oversell requests
                to current live holdings instead of rejecting
            treat_as_market: If True, submit as IOC at current best sell price
                so copied sells do not rest as GTC limit orders
            
        Returns:
            Dict with execution details or None if failed
        """
        try:
            # Check live position size first to prevent over-selling
            live_position_size = await self._get_live_position_size(market_slug, outcome)
            
            if live_position_size is None:
                logger.warning(
                    f"Could not verify live position size for {market_slug} | {outcome}. "
                    f"Skipping sell to be safe."
                )
                return {"skipped": True, "reason": "NO_LIVE_POSITION_CHECK"}
            
            if shares > live_position_size:
                if allow_full_liquidation_on_oversell and live_position_size > 0:
                    logger.info(
                        f"Oversell request capped to live holdings: requested={shares:.2f}, "
                        f"available={live_position_size:.2f}"
                    )
                    shares = live_position_size
                else:
                    logger.warning(
                        f"Attempted to sell {shares:.2f} shares but only have {live_position_size:.2f}. "
                        f"Rejecting to prevent creating SHORT position."
                    )
                    return {"skipped": True, "reason": "OVER_SELL_PREVENTED"}
            
            # Normalize outcome
            normalized_outcome = await self.api_client.normalize_outcome_to_yes_no(
                market_slug, outcome
            )
            
            if not normalized_outcome:
                logger.warning(f"Could not normalize outcome '{outcome}' for {market_slug}")
                return {"skipped": True, "reason": "OUTCOME_NORMALIZATION_FAILED"}
            
            # Determine price. For market-style copied sells, use aggressive IOC limits
            # so the order is marketable on the US matching venue even if public CLOB
            # quotes diverge from executable US liquidity.
            if price is None:
                if treat_as_market:
                    price = 0.01 if normalized_outcome.lower() == "yes" else 0.99
                else:
                    price = await self.api_client.get_best_price(
                        market_slug, side="sell", outcome=normalized_outcome
                    )
                    if price is None:
                        logger.warning(f"Could not get sell price for {market_slug}")
                        return {"skipped": True, "reason": "NO_PRICE"}
            
            # In test mode, just return success
            if self.test_mode:
                logger.info(
                    f"TEST MODE: Would sell {shares:.2f} shares of {market_slug} "
                    f"at {price:.4f}"
                )
                return {
                    "success": True,
                    "price": price,
                    "shares": shares,
                }
            
            # Execute actual sell order (LIVE MODE)
            tif = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL" if treat_as_market else "TIME_IN_FORCE_GOOD_TILL_CANCEL"
            order_result = await self.api_client.place_order(
                market_slug=market_slug,
                outcome=normalized_outcome,
                side="SELL",
                shares=shares,
                price=price,
                tif=tif,
                order_type="ORDER_TYPE_LIMIT",
            )
            
            if not order_result:
                logger.warning(f"Sell order failed for {market_slug}")
                return None
            
            logger.info(
                f"SELL order accepted: {market_slug} | {shares:.2f} shares @ {price:.4f} | "
                f"tif={tif}"
            )
            
            return {
                "success": True,
                "price": price,
                "shares": shares,
                "order_id": order_result.get("order_id"),
            }
            
        except Exception as e:
            logger.exception(f"Error executing sell for {market_slug}: {e}")
            return None

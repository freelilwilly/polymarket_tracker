"""Trade execution logic with price validation."""
import asyncio
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

            # Determine if this is a NO-side trade and enforce policy in both
            # live and simulated execution paths.
            is_no_side = normalized_outcome.lower() == "no"
            if is_no_side and not Config.ALLOW_BUY_SHORT:
                logger.info(f"Skipping NO-side trade for {market_slug} (ALLOW_BUY_SHORT=false)")
                return {"skipped": True, "reason": "NO_SIDE_DISABLED"}

            # Mirror US API buy quantity semantics in both live and test modes.
            # Live order placement rounds BUY shares to nearest integer and rejects
            # non-positive quantities; enforce the same gate before simulation.
            buy_quantity = int(round(max(0.0, float(target_shares))))
            if buy_quantity <= 0:
                logger.info(
                    f"Skipping BUY: quantity rounds to zero under live order rules: "
                    f"{market_slug} | target_shares={target_shares:.6f}"
                )
                return {
                    "skipped": True,
                    "reason": "ORDER_QUANTITY_TOO_SMALL",
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
            
            # Execute actual buy order (LIVE MODE) as market-style IOC only.
            # Limit price respects tolerance to prevent fills far from observed price.
            # For NO-side, the API inverts this (1.0 - ioc_price), so observed_price + tolerance
            # translates to a NO-side limit that prevents egregious fills.
            ioc_price = min(0.99, observed_price + self.max_price_tolerance)

            async def _fetch_buy_execution(order_id_value: str) -> dict[str, Any]:
                """Poll order details with short backoff and return buy execution summary."""
                last_details: Optional[dict[str, Any]] = None
                poll_delays = [0.2, 0.25, 0.35, 0.45, 0.6, 0.8, 1.0]
                for attempt, delay_seconds in enumerate(poll_delays):
                    details = await self.api_client.get_order_details(order_id_value)
                    if isinstance(details, dict):
                        last_details = details
                        state = str(
                            details.get("state")
                            or details.get("status")
                            or details.get("orderState")
                            or ""
                        ).upper()
                        if state in {
                            "ORDER_STATE_FILLED",
                            "FILLED",
                            "ORDER_STATE_PARTIALLY_FILLED",
                            "PARTIALLY_FILLED",
                            "ORDER_STATE_CANCELLED",
                            "CANCELLED",
                            "CANCELED",
                            "ORDER_STATE_EXPIRED",
                            "EXPIRED",
                            "ORDER_STATE_REJECTED",
                            "REJECTED",
                        }:
                            break
                    if attempt < len(poll_delays) - 1:
                        await asyncio.sleep(delay_seconds)

                details = last_details or {}
                state = str(
                    details.get("state") or details.get("status") or details.get("orderState") or ""
                ).upper()

                cum_data = (
                    details.get("cumQuantity")
                    or details.get("cumQty")
                    or details.get("filledQuantity")
                    or details.get("executedQuantity")
                    or 0
                )
                if isinstance(cum_data, dict):
                    cum_qty = to_float(cum_data.get("value"), default=0.0)
                else:
                    cum_qty = to_float(cum_data, default=0.0)

                avg_data = details.get("avgPx") or details.get("avgPrice") or details.get("averagePrice") or 0
                if isinstance(avg_data, dict):
                    avg_px = to_float(avg_data.get("value"), default=0.0)
                else:
                    avg_px = to_float(avg_data, default=0.0)

                return {
                    "state": state,
                    "cum_qty": max(0.0, cum_qty),
                    "avg_px": max(0.0, avg_px),
                }

            async def _submit_buy_ioc(convert_no_price: bool) -> Optional[dict[str, Any]]:
                order_result = await self.api_client.place_order(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    side="BUY",
                    shares=target_shares,
                    price=ioc_price,
                    tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
                    order_type="ORDER_TYPE_LIMIT",
                    convert_no_price=convert_no_price,
                )
                if not order_result:
                    return None

                order_id = order_result.get("order_id")
                execution = {"state": "", "cum_qty": 0.0, "avg_px": 0.0}
                if order_id:
                    execution = await _fetch_buy_execution(str(order_id))

                return {
                    "order_id": order_id,
                    "execution": execution,
                }

            first_attempt = await _submit_buy_ioc(convert_no_price=True)
            used_no_basis_fallback = False
            if not first_attempt and is_no_side:
                # Some venues/environments interpret short-side price basis differently.
                first_attempt = await _submit_buy_ioc(convert_no_price=False)
                used_no_basis_fallback = True

            if not first_attempt:
                order_error = self.api_client.last_order_error or {}
                status = order_error.get("status")
                message = str(order_error.get("message") or "").lower()
                candidate_slugs = order_error.get("candidate_slugs") or []

                market_unavailable_reasons = []
                if self.api_client._is_market_not_found_error(order_error):
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
                        "candidate_slugs": candidate_slugs,
                    }

                logger.warning(f"Order placement failed for {market_slug}")
                return None

            order_ids: list[str] = []
            first_order_id = str(first_attempt.get("order_id") or "")
            if first_order_id:
                order_ids.append(first_order_id)

            first_exec = first_attempt.get("execution") or {}
            filled_qty = to_float(first_exec.get("cum_qty"), default=0.0)
            avg_fill_px = to_float(first_exec.get("avg_px"), default=0.0)
            order_state = str(first_exec.get("state") or "")

            if is_no_side and filled_qty <= 0 and not used_no_basis_fallback:
                retry_attempt = await _submit_buy_ioc(convert_no_price=False)
                if retry_attempt:
                    retry_order_id = str(retry_attempt.get("order_id") or "")
                    if retry_order_id:
                        order_ids.append(retry_order_id)

                    retry_exec = retry_attempt.get("execution") or {}
                    retry_filled = to_float(retry_exec.get("cum_qty"), default=0.0)
                    retry_avg = to_float(retry_exec.get("avg_px"), default=0.0)
                    retry_state = str(retry_exec.get("state") or "")
                    if retry_filled > 0:
                        filled_qty += retry_filled
                        weighted_notional = (avg_fill_px * (filled_qty - retry_filled)) + (retry_avg * retry_filled)
                        avg_fill_px = (weighted_notional / filled_qty) if filled_qty > 0 else 0.0
                    order_state = retry_state or order_state

            order_id = order_ids[-1] if order_ids else str(first_attempt.get("order_id") or "")

            if filled_qty <= 0:
                logger.info(
                    f"BUY submitted and pending fill visibility: {market_slug} | order_id={order_id} | "
                    f"state={order_state or 'UNKNOWN'}"
                )
                return {
                    "submitted": True,
                    "reason": "BUY_PENDING",
                    "order_id": order_id,
                    "state": order_state,
                    "shares": 0.0,
                    "current_price": current_price,
                }
            
            logger.info(
                f"BUY IOC executed: {market_slug} | {filled_qty:.2f}/{target_shares:.2f} shares "
                f"@ {(avg_fill_px if avg_fill_px > 0 else current_price):.4f} | order_id={order_id}"
            )
            
            return {
                "success": True,
                "current_price": avg_fill_px if avg_fill_px > 0 else current_price,
                "shares": filled_qty,
                "order_id": order_id,
                "state": order_state,
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
        treat_as_market: bool = True,
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
            
            if not treat_as_market:
                logger.warning(
                    f"Non-market copied SELL blocked: {market_slug} | {normalized_outcome}. "
                    f"Copied sells must be IOC market-style orders."
                )
                return {"skipped": True, "reason": "NON_MARKET_SELL_BLOCKED"}

            # Copied SELLs are market-only: force aggressive IOC limits and ignore
            # caller-provided limit prices so orders cannot rest on the book.
            price = 0.01 if normalized_outcome.lower() == "yes" else 0.99

            # Mirror US API sell quantity semantics in both live and test modes.
            # Live order placement floors SELL shares to integer quantity and rejects
            # non-positive quantities; enforce the same gate before simulation.
            sell_quantity = int(max(0.0, float(shares)))
            if sell_quantity <= 0:
                logger.info(
                    f"Skipping SELL: quantity floors to zero under live order rules: "
                    f"{market_slug} | shares={shares:.6f}"
                )
                return {
                    "skipped": True,
                    "reason": "ORDER_QUANTITY_TOO_SMALL",
                    "shares": 0.0,
                    "price": price,
                }
            
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

            async def _fetch_order_execution(order_id: str) -> dict[str, Any]:
                """Poll a fresh IOC order briefly and return execution summary."""
                last_details: Optional[dict[str, Any]] = None
                for _ in range(4):
                    details = await self.api_client.get_order_details(order_id)
                    if isinstance(details, dict):
                        last_details = details
                        state = str(
                            details.get("state")
                            or details.get("status")
                            or details.get("orderState")
                            or ""
                        ).upper()
                        if state in {
                            "ORDER_STATE_FILLED",
                            "FILLED",
                            "ORDER_STATE_EXPIRED",
                            "EXPIRED",
                            "ORDER_STATE_CANCELLED",
                            "CANCELLED",
                            "CANCELED",
                            "ORDER_STATE_REJECTED",
                            "REJECTED",
                            "ORDER_STATE_PARTIALLY_FILLED",
                            "PARTIALLY_FILLED",
                        }:
                            break
                    await asyncio.sleep(0.35)

                details = last_details or {}
                state = str(
                    details.get("state") or details.get("status") or details.get("orderState") or ""
                ).upper()

                cum_data = (
                    details.get("cumQuantity")
                    or details.get("cumQty")
                    or details.get("filledQuantity")
                    or details.get("executedQuantity")
                    or 0
                )
                if isinstance(cum_data, dict):
                    cum_qty = to_float(cum_data.get("value"), default=0.0)
                else:
                    cum_qty = to_float(cum_data, default=0.0)

                avg_data = details.get("avgPx") or details.get("avgPrice") or details.get("averagePrice") or 0
                if isinstance(avg_data, dict):
                    avg_px = to_float(avg_data.get("value"), default=0.0)
                else:
                    avg_px = to_float(avg_data, default=0.0)

                return {
                    "state": state,
                    "cum_qty": max(0.0, cum_qty),
                    "avg_px": max(0.0, avg_px),
                }

            async def _submit_ioc(convert_no_price: bool, submit_shares: float, submit_price: float) -> Optional[dict[str, Any]]:
                """Submit IOC sell and capture immediate execution state."""
                order_result = await self.api_client.place_order(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    side="SELL",
                    shares=submit_shares,
                    price=submit_price,
                    tif="TIME_IN_FORCE_IMMEDIATE_OR_CANCEL",
                    order_type="ORDER_TYPE_LIMIT",
                    convert_no_price=convert_no_price,
                )
                if not order_result:
                    return None

                order_id = order_result.get("order_id")
                execution = {"state": "", "cum_qty": 0.0, "avg_px": 0.0}
                if order_id:
                    execution = await _fetch_order_execution(str(order_id))

                return {
                    "order_id": order_id,
                    "execution": execution,
                }
            
            # Execute actual sell order (LIVE MODE) as market-style IOC only.
            tif = "TIME_IN_FORCE_IMMEDIATE_OR_CANCEL"
            total_filled = 0.0
            weighted_notional = 0.0
            order_ids: list[str] = []

            first = await _submit_ioc(convert_no_price=True, submit_shares=shares, submit_price=price)
            if not first:
                logger.warning(f"Sell IOC failed for {market_slug}")
                return None

            first_order_id = str(first.get("order_id") or "")
            if first_order_id:
                order_ids.append(first_order_id)

            first_exec = first.get("execution") or {}
            first_filled = to_float(first_exec.get("cum_qty"), default=0.0)
            first_avg = to_float(first_exec.get("avg_px"), default=0.0)
            first_state = str(first_exec.get("state") or "")

            if first_filled > 0:
                total_filled += first_filled
                weighted_notional += first_filled * (first_avg if first_avg > 0 else price)

            # If NO-side IOC returned unfilled, retry once with non-converted price basis.
            # Some venues/environments interpret short-side price basis differently.
            remaining = max(0.0, shares - total_filled)
            if normalized_outcome.lower() == "no" and total_filled <= 0 and remaining >= 1.0:
                live_after_first = await self._get_live_position_size(market_slug, normalized_outcome)
                close_epsilon = max(0.0, to_float(Config.SELL_CLOSE_EPSILON_SHARES, default=0.01))
                if live_after_first is not None and live_after_first <= close_epsilon:
                    logger.warning(
                        f"IOC sell visibility uncertain but live position is near zero; skipping retry: "
                        f"{market_slug} | live={live_after_first:.4f}"
                    )
                    return {
                        "skipped": True,
                        "reason": "IOC_UNFILLED_OR_ALREADY_CLOSED",
                        "shares": 0.0,
                        "price": price,
                        "order_id": order_ids[0] if order_ids else None,
                        "order_ids": order_ids,
                        "live_remaining": live_after_first,
                    }
                logger.warning(
                    f"IOC sell unfilled on first attempt: {market_slug} | state={first_state or 'UNKNOWN'} | "
                    f"retrying with alternate NO-price basis"
                )
                second = await _submit_ioc(convert_no_price=False, submit_shares=remaining, submit_price=price)
                if second:
                    second_order_id = str(second.get("order_id") or "")
                    if second_order_id:
                        order_ids.append(second_order_id)
                    second_exec = second.get("execution") or {}
                    second_filled = to_float(second_exec.get("cum_qty"), default=0.0)
                    second_avg = to_float(second_exec.get("avg_px"), default=0.0)
                    if second_filled > 0:
                        total_filled += second_filled
                        weighted_notional += second_filled * (second_avg if second_avg > 0 else price)

            if total_filled <= 0:
                live_after_attempts = await self._get_live_position_size(market_slug, normalized_outcome)
                close_epsilon = max(0.0, to_float(Config.SELL_CLOSE_EPSILON_SHARES, default=0.01))
                if live_after_attempts is not None and live_after_attempts <= close_epsilon:
                    logger.warning(
                        f"SELL IOC reported unfilled but live position is near zero: {market_slug} | "
                        f"live={live_after_attempts:.4f} | order_ids={order_ids}"
                    )
                    return {
                        "skipped": True,
                        "reason": "IOC_UNFILLED_OR_ALREADY_CLOSED",
                        "shares": 0.0,
                        "price": price,
                        "order_id": order_ids[0] if order_ids else None,
                        "order_ids": order_ids,
                        "live_remaining": live_after_attempts,
                    }
                logger.warning(
                    f"SELL IOC unfilled: {market_slug} | outcome={normalized_outcome} | "
                    f"requested={shares:.2f} | order_ids={order_ids}"
                )
                return {
                    "skipped": True,
                    "reason": "IOC_UNFILLED",
                    "shares": 0.0,
                    "price": price,
                    "order_id": order_ids[0] if order_ids else None,
                    "order_ids": order_ids,
                }

            avg_fill_price = weighted_notional / total_filled if total_filled > 0 else price
            logger.info(
                f"SELL IOC filled: {market_slug} | filled={total_filled:.2f}/{shares:.2f} @ {avg_fill_price:.4f}"
            )
            return {
                "success": True,
                "price": avg_fill_price,
                "shares": total_filled,
                "order_id": order_ids[0] if order_ids else None,
                "order_ids": order_ids,
            }
            
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

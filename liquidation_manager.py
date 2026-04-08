"""
Automatic liquidation manager.

Places $0.98 limit orders to auto-exit profitable positions.
Only active when ENABLE_AUTO_LIQUIDATION=true.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from api_client import PolymarketAPIClient
from config import Config
from position_manager import PositionManager

logger = logging.getLogger(__name__)


class LiquidationManager:
    """Manages automatic liquidation orders for profitable positions."""
    
    def __init__(
        self,
        api_client: PolymarketAPIClient,
        position_manager: PositionManager,
    ):
        """
        Initialize liquidation manager.
        
        Args:
            api_client: API client instance
            position_manager: Position manager instance
        """
        self.api_client = api_client
        self.position_manager = position_manager
        
        # Track active liquidation orders: position_key -> order_id
        self.liquidation_orders: dict[str, str] = {}
        
        logger.info(
            f"Liquidation manager initialized (enabled={Config.ENABLE_AUTO_LIQUIDATION})"
        )
    
    async def manage_liquidation_orders(self):
        """
        Main liquidation management loop.
        
        - Creates liquidation orders for positions that don't have them
        - Monitors existing orders for fills
        - Cancels stale orders if position closed externally
        """
        if not Config.ENABLE_AUTO_LIQUIDATION:
            return
        
        positions = self.position_manager.get_all_positions()
        
        if not positions:
            return
        
        # Get all open orders from API
        api_orders = await self.api_client.get_orders()
        if api_orders is None:
            logger.warning("Cannot manage liquidation orders: failed to fetch API orders")
            return
        
        # Build fallback mapping of existing API liquidation-like orders by position key.
        # This prevents duplicate liquidation orders after process restarts when
        # in-memory tracking is empty but orders are still open on the exchange.
        existing_liquidation_by_position: dict[str, str] = {}
        for order in api_orders:
            if not isinstance(order, dict):
                continue

            order_id = order.get("id") or order.get("orderId") or order.get("order_id")
            market_slug = str(order.get("marketSlug") or order.get("market_slug") or "").strip()
            intent = str(order.get("intent") or "").upper()

            if not order_id or not market_slug:
                continue

            if intent not in ("ORDER_INTENT_SELL_LONG", "ORDER_INTENT_SELL_SHORT"):
                continue

            price_data = order.get("price")
            order_price = None
            if isinstance(price_data, dict):
                try:
                    order_price = float(price_data.get("value"))
                except (TypeError, ValueError):
                    order_price = None
            else:
                try:
                    order_price = float(price_data)
                except (TypeError, ValueError):
                    order_price = None

            # Match only orders at configured liquidation price.
            # Use wider tolerance to handle floating point precision and API format variations
            if order_price is None or abs(order_price - float(Config.LIQUIDATION_PRICE)) > 0.01:
                continue

            outcome = "yes" if intent == "ORDER_INTENT_SELL_LONG" else "no"
            
            # Try both slug variants to ensure we match regardless of API format
            candidate_slugs = [market_slug]
            if market_slug.startswith("aec-"):
                candidate_slugs.append(market_slug[4:])
            else:
                candidate_slugs.append(f"aec-{market_slug}")

            for candidate_slug in candidate_slugs:
                position_key = self.position_manager.get_position_key(candidate_slug, outcome)
                if position_key not in existing_liquidation_by_position:
                    existing_liquidation_by_position[position_key] = str(order_id)
                    logger.debug(
                        f"Found existing liquidation order: {candidate_slug} | {outcome} | "
                        f"Order ID: {order_id} | Price: ${order_price:.2f}"
                    )
        
        # Build lookup of active API orders
        api_order_lookup: dict[str, dict[str, Any]] = {}
        for order in api_orders:
            order_id = order.get("id") or order.get("orderId") or order.get("order_id")
            if order_id:
                api_order_lookup[order_id] = order
        
        # Check each position
        for position in positions:
            market_slug = position["market_slug"]
            outcome = position["outcome"]

            # Canonicalize legacy outcome aliases (e.g., team names) so liquidation
            # tracking uses stable yes/no position keys.
            normalized_outcome = outcome
            outcome_lower = str(outcome or "").strip().lower()
            if outcome_lower not in ("yes", "no"):
                resolved = await self.api_client.normalize_outcome_to_yes_no(
                    market_slug,
                    outcome,
                    caller_context="liquidation_reconcile",
                )
                if resolved:
                    self.position_manager.reconcile_outcome_alias(
                        market_slug=market_slug,
                        canonical_outcome=resolved,
                        alias_outcome=outcome,
                    )
                    normalized_outcome = resolved

            shares = position["shares"]
            
            position_key = self.position_manager.get_position_key(market_slug, normalized_outcome)
            
            # Check if we already have a liquidation order for this position
            existing_order_id = self.liquidation_orders.get(position_key)

            if not existing_order_id:
                adopted_order_id = existing_liquidation_by_position.get(position_key)
                if adopted_order_id:
                    self.liquidation_orders[position_key] = adopted_order_id
                    existing_order_id = adopted_order_id
                    logger.info(
                        f"Adopted existing liquidation order from API: {market_slug} | {normalized_outcome} | "
                        f"Order ID: {adopted_order_id}"
                    )
            
            if existing_order_id:
                # Check if order still exists in API
                if existing_order_id in api_order_lookup:
                    # Order still active, check status
                    order = api_order_lookup[existing_order_id]
                    status = str(order.get("status") or order.get("state") or "").upper()

                    expected_quantity = max(1, int(round(float(shares))))
                    existing_quantity = None
                    try:
                        existing_quantity = int(round(float(order.get("quantity") or order.get("leavesQuantity") or 0)))
                    except (TypeError, ValueError):
                        existing_quantity = None
                    
                    if status in ("FILLED", "MATCHED", "COMPLETE"):
                        # Order filled, position should be closed
                        logger.info(
                            f"Liquidation order filled: {market_slug} | {outcome} | "
                            f"Order ID: {existing_order_id}"
                        )
                        
                        # Close position in our tracker
                        self.position_manager.close_position(
                            market_slug,
                            normalized_outcome,
                            exit_price=Config.LIQUIDATION_PRICE,
                            reason="auto_liquidation",
                        )
                        
                        # Remove from liquidation tracking
                        self.liquidation_orders.pop(position_key, None)
                    
                    elif status in ("CANCELLED", "EXPIRED"):
                        # Order canceled/expired, remove from tracking and recreate
                        logger.info(
                            f"Liquidation order {status.lower()}: {market_slug} | {outcome}"
                        )
                        self.liquidation_orders.pop(position_key, None)
                        
                        # Will be recreated below

                    elif existing_quantity is None:
                        logger.warning(
                            f"Cannot determine liquidation order quantity: {market_slug} | {normalized_outcome} | "
                            f"Order ID: {existing_order_id}. Recreating order."
                        )
                        self.liquidation_orders.pop(position_key, None)

                    elif existing_quantity != expected_quantity:
                        logger.info(
                            f"Liquidation quantity mismatch: {market_slug} | {normalized_outcome} | "
                            f"existing={existing_quantity}, expected={expected_quantity}. Replacing order."
                        )

                        canceled = await self.api_client.cancel_order(existing_order_id, market_slug=market_slug)
                        if canceled:
                            self.liquidation_orders.pop(position_key, None)
                        else:
                            logger.warning(
                                f"Failed to cancel mismatched liquidation order: {market_slug} | {normalized_outcome} | "
                                f"Order ID: {existing_order_id}. Skipping recreate this cycle."
                            )
                            continue
                    
                    else:
                        # Order still active (OPEN, PENDING, etc.), keep monitoring
                        continue
                
                else:
                    # Order no longer in API, remove from tracking
                    logger.warning(
                        f"Liquidation order not found in API: {market_slug} | {outcome} | "
                        f"Order ID: {existing_order_id}"
                    )
                    self.liquidation_orders.pop(position_key, None)
            
            # Create liquidation order if we don't have one
            if position_key not in self.liquidation_orders:
                # Double-check: see if there's any existing liquidation order we might have missed
                # (belt-and-suspenders to prevent duplicate orders)
                found_existing = False
                for order in api_orders:
                    if not isinstance(order, dict):
                        continue
                    
                    order_market = str(order.get("marketSlug") or order.get("market_slug") or "").strip()
                    order_intent = str(order.get("intent") or "").upper()
                    
                    # Check if this order is for our position
                    expected_intent = "ORDER_INTENT_SELL_LONG" if normalized_outcome == "yes" else "ORDER_INTENT_SELL_SHORT"
                    if order_intent != expected_intent:
                        continue
                    
                    # Check both slug variants
                    slug_matches = (
                        order_market == market_slug or
                        order_market == f"aec-{market_slug}" or
                        (order_market.startswith("aec-") and order_market[4:] == market_slug)
                    )
                    
                    if slug_matches:
                        # Found an existing order for this position
                        order_price_data = order.get("price")
                        order_price = None
                        if isinstance(order_price_data, dict):
                            try:
                                order_price = float(order_price_data.get("value"))
                            except (TypeError, ValueError):
                                pass
                        
                        if order_price and abs(order_price - float(Config.LIQUIDATION_PRICE)) < 0.02:
                            found_existing = True
                            order_id = order.get("id") or order.get("orderId") or order.get("order_id")
                            logger.info(
                                f"Found existing liquidation order (adoption missed it): {market_slug} | {normalized_outcome} | "
                                f"Order ID: {order_id} | Price: ${order_price:.2f}"
                            )
                            self.liquidation_orders[position_key] = str(order_id)
                            break
                
                if not found_existing:
                    await self._create_liquidation_order(market_slug, normalized_outcome, shares)
    
    async def _create_liquidation_order(
        self,
        market_slug: str,
        outcome: str,
        shares: float,
    ):
        """
        Create a liquidation order for a position.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            shares: Number of shares to sell
        """
        try:
            # Log the position details before attempting to create the order
            position_key = self.position_manager.get_position_key(market_slug, outcome)
            position_data = self.position_manager.positions.get(position_key, {})
            
            # CRITICAL PRE-FLIGHT CHECK: Verify account actually has these shares
            # Get API positions to confirm the shares exist
            api_positions = await self.api_client.get_positions()
            api_position_for_market = None
            if api_positions:
                for api_pos in api_positions:
                    api_market = str(api_pos.get("marketSlug") or api_pos.get("market_slug") or "").strip()
                    api_outcome_raw = str(api_pos.get("outcome") or "").strip()
                    
                    # Normalize API market slug (remove aec- prefix)
                    if api_market.startswith("aec-"):
                        api_market = api_market[4:]
                    
                    # CRITICAL: Normalize the API outcome to yes/no for comparison
                    # The API returns team names like "Fighting Illini" but we store as "yes/no"
                    api_outcome_normalized = api_outcome_raw.lower()
                    if api_outcome_normalized not in ("yes", "no"):
                        # Try to normalize team name to yes/no
                        normalized = await self.api_client.normalize_outcome_to_yes_no(
                            api_market,
                            api_outcome_raw,
                            caller_context="liquidation_preflight",
                        )
                        if normalized:
                            api_outcome_normalized = normalized
                    
                    # Match by market AND outcome (comparing normalized outcomes)
                    if api_market == market_slug and api_outcome_normalized == outcome.lower():
                        api_position_for_market = api_pos
                        break
            
            api_shares = 0.0
            if api_position_for_market:
                try:
                    api_shares = abs(float(api_position_for_market.get("size") or 0))
                except (TypeError, ValueError):
                    pass
            
            logger.info(
                f"Creating liquidation order: {market_slug} | {outcome} | "
                f"Attempting {shares:.2f} shares @ ${Config.LIQUIDATION_PRICE} | "
                f"Local position: {position_data.get('shares', 'N/A')} shares @ "
                f"${position_data.get('avg_price', 'N/A')} | "
                f"API has: {api_shares:.2f} shares"
            )
            
            # If API has no shares or fewer shares than we're trying to sell, warn
            if api_shares < 0.01:
                logger.error(
                    f"ABORTING liquidation order: API reports {api_shares:.2f} shares but trying to sell {shares:.2f}. "
                    f"Position tracking may be out of sync!"
                )
                return
            elif api_shares < shares - 0.01:
                logger.warning(
                    f"API has fewer shares ({api_shares:.2f}) than position tracking ({shares:.2f}). "
                    f"Adjusting order to match API balance."
                )
                shares = api_shares
            
            result = await self.api_client.place_order(
                market_slug=market_slug,
                outcome=outcome,
                side="SELL",
                shares=shares,
                price=Config.LIQUIDATION_PRICE,
                convert_no_price=False,  # Don't invert NO prices for SELL orders
            )
            
            if result:
                order_id = result.get("order_id")
                if order_id:
                    position_key = self.position_manager.get_position_key(market_slug, outcome)
                    self.liquidation_orders[position_key] = order_id
                    
                    logger.info(
                        f"Liquidation order created: {market_slug} | {outcome} | "
                        f"Order ID: {order_id}"
                    )
                else:
                    logger.warning(f"Liquidation order created but no order ID returned")
            else:
                logger.warning(
                    f"Failed to create liquidation order: {market_slug} | {outcome} | "
                    f"API returned None (check logs for errors)"
                )
        
        except Exception as e:
            logger.exception(f"Error creating liquidation order: {e}")
    
    async def cancel_liquidation_order(
        self,
        market_slug: str,
        outcome: str,
    ) -> bool:
        """
        Cancel liquidation order for a position.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            
        Returns:
            True if canceled successfully
        """
        position_key = self.position_manager.get_position_key(market_slug, outcome)
        order_id = self.liquidation_orders.get(position_key)

        if not order_id:
            # Fallback for process restarts: discover a matching liquidation order
            # directly from API so copied sells can reliably release reserved shares.
            orders = await self.api_client.get_orders()
            if isinstance(orders, list):
                expected_intent = "ORDER_INTENT_SELL_LONG" if str(outcome).lower() == "yes" else "ORDER_INTENT_SELL_SHORT"
                candidate_slugs = {str(market_slug).strip()}
                if str(market_slug).startswith("aec-"):
                    candidate_slugs.add(str(market_slug)[4:])

                for order in orders:
                    if not isinstance(order, dict):
                        continue

                    slug = str(order.get("marketSlug") or order.get("market_slug") or "").strip()
                    if slug not in candidate_slugs:
                        continue

                    intent = str(order.get("intent") or "").upper()
                    if intent != expected_intent:
                        continue

                    price_data = order.get("price")
                    try:
                        if isinstance(price_data, dict):
                            order_price = float(price_data.get("value"))
                        else:
                            order_price = float(price_data)
                    except (TypeError, ValueError):
                        continue

                    if abs(order_price - float(Config.LIQUIDATION_PRICE)) > 0.001:
                        continue

                    found_id = order.get("id") or order.get("orderId") or order.get("order_id")
                    if found_id:
                        order_id = str(found_id)
                        self.liquidation_orders[position_key] = order_id
                        break

        if not order_id:
            return False
        
        success = await self.api_client.cancel_order(order_id, market_slug=market_slug)
        
        if success:
            self.liquidation_orders.pop(position_key, None)
            logger.info(
                f"Liquidation order canceled: {market_slug} | {outcome} | "
                f"Order ID: {order_id}"
            )
        else:
            logger.warning(
                f"Failed to cancel liquidation order: {market_slug} | {outcome} | "
                f"Order ID: {order_id}"
            )
        
        return success
    
    async def cancel_all_liquidation_orders(self):
        """Cancel all tracked liquidation orders."""
        if not self.liquidation_orders:
            return
        
        logger.info(f"Canceling {len(self.liquidation_orders)} liquidation orders...")
        
        for position_key, order_id in list(self.liquidation_orders.items()):
            try:
                market_slug = position_key.split("|", 1)[0] if "|" in position_key else ""
                await self.api_client.cancel_order(order_id, market_slug=market_slug or None)
                self.liquidation_orders.pop(position_key, None)
            except Exception as e:
                logger.exception(f"Error canceling liquidation order {order_id}: {e}")

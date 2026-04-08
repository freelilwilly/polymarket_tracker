"""
Stop-loss order manager.

Creates limit sell orders at 15% below entry price to limit downside risk.
Active when ENABLE_STOP_LOSS=true.
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from api_client import PolymarketAPIClient
from config import Config
from position_manager import PositionManager

logger = logging.getLogger(__name__)


class StopLossManager:
    """Manages automatic stop-loss orders for positions."""
    
    def __init__(
        self,
        api_client: PolymarketAPIClient,
        position_manager: PositionManager,
    ):
        """
        Initialize stop-loss manager.
        
        Args:
            api_client: API client instance
            position_manager: Position manager instance
        """
        self.api_client = api_client
        self.position_manager = position_manager
        
        # Track active stop-loss orders: position_key -> order details
        self.stop_loss_orders: dict[str, dict[str, Any]] = {}
        
        # Track position shares to detect averaging up
        self.position_shares_cache: dict[str, float] = {}
        
        self.state_file = "stop_loss_state.json"
        self._load_state()
        
        logger.info(
            f"Stop-loss manager initialized (enabled={Config.ENABLE_STOP_LOSS}, "
            f"percent={Config.STOP_LOSS_PERCENT * 100:.0f}%)"
        )
    
    def _load_state(self) -> None:
        """Load stop-loss state from disk."""
        if not os.path.exists(self.state_file):
            logger.debug(f"No existing stop-loss state file: {self.state_file}")
            self.stop_loss_orders = {}
            return
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.stop_loss_orders = data.get("stop_loss_orders", {})
                logger.info(
                    f"Loaded {len(self.stop_loss_orders)} stop-loss orders from {self.state_file}"
                )
        except Exception as e:
            logger.exception(f"Error loading stop-loss state: {e}")
            self.stop_loss_orders = {}
    
    def _save_state(self) -> None:
        """Save stop-loss state to disk."""
        try:
            data = {
                "stop_loss_orders": self.stop_loss_orders,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            
            # Atomic write via temp file
            temp_file = f"{self.state_file}.tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            
            # Replace original file
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
            os.rename(temp_file, self.state_file)
        except Exception as e:
            logger.exception(f"Error saving stop-loss state: {e}")
    
    def _calculate_stop_price(self, entry_price: float) -> float:
        """
        Calculate stop-loss price.
        
        Args:
            entry_price: Position entry price
            
        Returns:
            Stop-loss price (entry_price * (1 - stop_loss_percent))
        """
        stop_price = entry_price * (1.0 - Config.STOP_LOSS_PERCENT)
        # Clamp to valid range
        return max(0.01, min(0.99, stop_price))
    
    async def manage_stop_loss_orders(self):
        """
        Main stop-loss management loop.
        
        - Creates stop-loss orders for positions that don't have them
        - Monitors existing orders for fills
        - Updates stop-loss on averaging up (position shares increased)
        - Cancels stale orders if position closed externally
        """
        if not Config.ENABLE_STOP_LOSS:
            return
        
        positions = self.position_manager.get_all_positions()
        
        if not positions:
            # Clean up orphaned orders
            if self.stop_loss_orders:
                logger.info("No positions but have stop-loss orders. Canceling all.")
                await self.cancel_all_stop_loss_orders()
            return
        
        # Get all open orders from API
        api_orders = await self.api_client.get_orders()
        if api_orders is None:
            logger.warning("Cannot manage stop-loss orders: failed to fetch API orders")
            return
        
        # Build fallback mapping of existing API stop-loss-like orders by position key.
        # This prevents duplicate stop-loss orders after process restarts when
        # in-memory tracking is empty but orders are still open on the exchange.
        existing_stop_loss_by_position: dict[str, tuple[str, float]] = {}
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

            if order_price is None:
                continue
            
            # Skip if this looks like a liquidation order (near $0.98)
            if abs(order_price - float(Config.LIQUIDATION_PRICE)) < 0.02:
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
                if position_key not in existing_stop_loss_by_position:
                    existing_stop_loss_by_position[position_key] = (str(order_id), order_price)
                    logger.debug(
                        f"Found existing stop-loss order: {candidate_slug} | {outcome} | "
                        f"Order ID: {order_id} | Price: ${order_price:.2f}"
                    )
        
        # Build lookup of active API orders
        api_order_lookup: dict[str, dict[str, Any]] = {}
        for order in api_orders:
            order_id = order.get("id") or order.get("orderId") or order.get("order_id")
            if order_id:
                api_order_lookup[order_id] = order
        
        # Track positions we've seen this cycle
        seen_position_keys = set()
        
        # Check each position
        for position in positions:
            market_slug = position["market_slug"]
            outcome = position["outcome"]

            # Canonicalize legacy outcome aliases (e.g., team names) so stop-loss
            # tracking uses stable yes/no position keys.
            normalized_outcome = outcome
            outcome_lower = str(outcome or "").strip().lower()
            if outcome_lower not in ("yes", "no"):
                resolved = await self.api_client.normalize_outcome_to_yes_no(
                    market_slug,
                    outcome,
                    caller_context="stop_loss_reconcile",
                )
                if resolved:
                    self.position_manager.reconcile_outcome_alias(
                        market_slug=market_slug,
                        canonical_outcome=resolved,
                        alias_outcome=outcome,
                    )
                    normalized_outcome = resolved

            shares = position["shares"]
            entry_price = position.get("entry_price", 0.0)
            
            position_key = self.position_manager.get_position_key(market_slug, normalized_outcome)
            seen_position_keys.add(position_key)
            
            # Calculate expected stop price
            expected_stop_price = self._calculate_stop_price(entry_price)
            
            # Check for averaging up (shares increased)
            cached_shares = self.position_shares_cache.get(position_key, 0.0)
            if cached_shares > 0 and shares > cached_shares + 0.01:
                logger.info(
                    f"Detected averaging up: {market_slug} | {normalized_outcome} | "
                    f"shares increased from {cached_shares:.2f} to {shares:.2f}"
                )
                # Will cancel and recreate with new stop price
                existing_order_id = self.stop_loss_orders.get(position_key, {}).get("order_id")
                if existing_order_id:
                    await self._cancel_stop_loss_order(market_slug, normalized_outcome)
            
            # Update shares cache
            self.position_shares_cache[position_key] = shares
            
            # Check if we already have a stop-loss order for this position
            existing_order = self.stop_loss_orders.get(position_key)
            existing_order_id = existing_order.get("order_id") if existing_order else None

            if not existing_order_id:
                # Try to adopt existing order from API
                adopted_info = existing_stop_loss_by_position.get(position_key)
                if adopted_info:
                    adopted_order_id, adopted_price = adopted_info
                    
                    # Check if price is within tolerance of expected stop price
                    price_diff = abs(adopted_price - expected_stop_price)
                    tolerance = expected_stop_price * Config.STOP_LOSS_PRICE_TOLERANCE
                    
                    if price_diff <= tolerance:
                        self.stop_loss_orders[position_key] = {
                            "order_id": adopted_order_id,
                            "stop_price": adopted_price,
                            "entry_price": entry_price,
                            "shares": shares,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        }
                        self._save_state()
                        existing_order_id = adopted_order_id
                        logger.info(
                            f"Adopted existing stop-loss order from API: {market_slug} | {normalized_outcome} | "
                            f"Order ID: {adopted_order_id} | Price: ${adopted_price:.2f}"
                        )
                    else:
                        logger.warning(
                            f"Found order but price too far from expected: {market_slug} | {normalized_outcome} | "
                            f"API price: ${adopted_price:.2f}, expected: ${expected_stop_price:.2f}, "
                            f"diff: ${price_diff:.4f} > tolerance: ${tolerance:.4f}"
                        )
            
            if existing_order_id:
                # Check if order still exists in API
                if existing_order_id in api_order_lookup:
                    # Order still active, check status
                    order = api_order_lookup[existing_order_id]
                    status = str(order.get("status") or order.get("state") or "").upper()

                    if status in ("FILLED", "MATCHED", "COMPLETE"):
                        # Order filled, position should be closed
                        logger.warning(
                            f"Stop-loss order FILLED: {market_slug} | {normalized_outcome} | "
                            f"Order ID: {existing_order_id}"
                        )
                        
                        # Extract fill price
                        fill_price = existing_order.get("stop_price", expected_stop_price)
                        try:
                            avg_px = order.get("avgPrice") or order.get("avg_price")
                            if avg_px:
                                fill_price = float(avg_px)
                        except (TypeError, ValueError):
                            pass
                        
                        # Close position in our tracker
                        closed_position = self.position_manager.close_position(
                            market_slug,
                            normalized_outcome,
                            exit_price=fill_price,
                            reason="stop_loss",
                        )
                        
                        # Check if this was a loss and record wash sale
                        if closed_position and Config.ENABLE_WASH_SALE_PREVENTION:
                            invested = closed_position.get("invested", 0.0)
                            exit_value = closed_position.get("shares", 0.0) * fill_price
                            pnl = exit_value - invested
                            
                            if pnl < 0:
                                # Import here to avoid circular dependency
                                from wash_sale_tracker import WashSaleTracker
                                wash_tracker = getattr(self.position_manager, 'wash_sale_tracker', None)
                                if wash_tracker:
                                    wash_tracker.record_loss_sale(
                                        market_slug=market_slug,
                                        outcome=normalized_outcome,
                                        realized_pnl=pnl,
                                        exit_price=fill_price,
                                    )
                                    logger.info(
                                        f"Stop-loss fill triggered wash sale block: "
                                        f"{market_slug} | {normalized_outcome} | PnL=${pnl:.2f}"
                                    )
                        
                        # Remove from stop-loss tracking
                        self.stop_loss_orders.pop(position_key, None)
                        self.position_shares_cache.pop(position_key, None)
                        self._save_state()
                    
                    elif status in ("CANCELLED", "EXPIRED"):
                        # Order canceled/expired, remove from tracking and recreate
                        logger.info(
                            f"Stop-loss order {status.lower()}: {market_slug} | {normalized_outcome}"
                        )
                        self.stop_loss_orders.pop(position_key, None)
                        self._save_state()
                        # Will be recreated below
                    
                    else:
                        # Check if stop price needs updating (entry price changed)
                        tracked_entry_price = existing_order.get("entry_price", 0.0)
                        if abs(entry_price - tracked_entry_price) > 0.001:
                            logger.info(
                                f"Entry price changed: {market_slug} | {normalized_outcome} | "
                                f"old=${tracked_entry_price:.4f}, new=${entry_price:.4f}. "
                                f"Updating stop-loss."
                            )
                            await self._cancel_stop_loss_order(market_slug, normalized_outcome)
                            # Will be recreated below
                        else:
                            # Order still active (OPEN, PENDING, etc.), keep monitoring
                            continue
                
                else:
                    # Order no longer in API, remove from tracking
                    logger.warning(
                        f"Stop-loss order not found in API: {market_slug} | {normalized_outcome} | "
                        f"Order ID: {existing_order_id}"
                    )
                    self.stop_loss_orders.pop(position_key, None)
                    self._save_state()
            
            # Create stop-loss order if we don't have one
            if position_key not in self.stop_loss_orders:
                await self._create_stop_loss_order(
                    market_slug,
                    normalized_outcome,
                    shares,
                    entry_price,
                )
        
        # Clean up orphaned orders (tracked but no matching position)
        orphaned_keys = set(self.stop_loss_orders.keys()) - seen_position_keys
        for position_key in orphaned_keys:
            logger.info(f"Removing orphaned stop-loss order: {position_key}")
            order_id = self.stop_loss_orders[position_key].get("order_id")
            if order_id:
                market_slug = position_key.split("|", 1)[0] if "|" in position_key else ""
                try:
                    await self.api_client.cancel_order(order_id, market_slug=market_slug or None)
                except Exception as e:
                    logger.warning(f"Error canceling orphaned order {order_id}: {e}")
            
            self.stop_loss_orders.pop(position_key, None)
            self.position_shares_cache.pop(position_key, None)
        
        if orphaned_keys:
            self._save_state()
    
    async def _create_stop_loss_order(
        self,
        market_slug: str,
        outcome: str,
        shares: float,
        entry_price: float,
    ):
        """
        Create a stop-loss order for a position.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            shares: Number of shares to sell
            entry_price: Position entry price
        """
        try:
            stop_price = self._calculate_stop_price(entry_price)
            
            position_key = self.position_manager.get_position_key(market_slug, outcome)
            
            # CRITICAL PRE-FLIGHT CHECK: Verify account actually has these shares
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
                    api_outcome_normalized = api_outcome_raw.lower()
                    if api_outcome_normalized not in ("yes", "no"):
                        normalized = await self.api_client.normalize_outcome_to_yes_no(
                            api_market,
                            api_outcome_raw,
                            caller_context="stop_loss_preflight",
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
                f"Creating stop-loss order: {market_slug} | {outcome} | "
                f"{shares:.2f} shares @ ${stop_price:.4f} "
                f"(entry=${entry_price:.4f}, {Config.STOP_LOSS_PERCENT * 100:.0f}% below) | "
                f"API has: {api_shares:.2f} shares"
            )
            
            # If API has no shares or fewer shares than we're trying to sell, warn
            if api_shares < 0.01:
                logger.error(
                    f"ABORTING stop-loss order: API reports {api_shares:.2f} shares but trying to sell {shares:.2f}. "
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
                price=stop_price,
                convert_no_price=False,  # Don't invert NO prices for SELL orders
            )
            
            if result:
                order_id = result.get("order_id")
                if order_id:
                    self.stop_loss_orders[position_key] = {
                        "order_id": order_id,
                        "stop_price": stop_price,
                        "entry_price": entry_price,
                        "shares": shares,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                    self._save_state()
                    
                    logger.info(
                        f"Stop-loss order created: {market_slug} | {outcome} | "
                        f"Order ID: {order_id} | Stop price: ${stop_price:.4f}"
                    )
                else:
                    logger.warning(f"Stop-loss order created but no order ID returned")
            else:
                logger.warning(
                    f"Failed to create stop-loss order: {market_slug} | {outcome} | "
                    f"API returned None (check logs for errors)"
                )
        
        except Exception as e:
            logger.exception(f"Error creating stop-loss order: {e}")
    
    async def _cancel_stop_loss_order(
        self,
        market_slug: str,
        outcome: str,
    ) -> bool:
        """
        Internal method to cancel stop-loss order.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            
        Returns:
            True if canceled successfully
        """
        position_key = self.position_manager.get_position_key(market_slug, outcome)
        order_info = self.stop_loss_orders.get(position_key)
        
        if not order_info:
            return False
        
        order_id = order_info.get("order_id")
        if not order_id:
            return False
        
        success = await self.api_client.cancel_order(order_id, market_slug=market_slug)
        
        if success:
            self.stop_loss_orders.pop(position_key, None)
            self.position_shares_cache.pop(position_key, None)
            self._save_state()
            logger.info(
                f"Stop-loss order canceled: {market_slug} | {outcome} | "
                f"Order ID: {order_id}"
            )
        else:
            logger.warning(
                f"Failed to cancel stop-loss order: {market_slug} | {outcome} | "
                f"Order ID: {order_id}"
            )
        
        return success
    
    async def cancel_stop_loss_order(
        self,
        market_slug: str,
        outcome: str,
    ) -> bool:
        """
        Public method to cancel stop-loss order for a position.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            
        Returns:
            True if canceled successfully
        """
        return await self._cancel_stop_loss_order(market_slug, outcome)
    
    async def cancel_all_stop_loss_orders(self):
        """Cancel all tracked stop-loss orders."""
        if not self.stop_loss_orders:
            return
        
        logger.info(f"Canceling {len(self.stop_loss_orders)} stop-loss orders...")
        
        for position_key, order_info in list(self.stop_loss_orders.items()):
            try:
                order_id = order_info.get("order_id")
                if not order_id:
                    continue
                
                market_slug = position_key.split("|", 1)[0] if "|" in position_key else ""
                await self.api_client.cancel_order(order_id, market_slug=market_slug or None)
            except Exception as e:
                logger.exception(f"Error canceling stop-loss order: {e}")
        
        self.stop_loss_orders.clear()
        self.position_shares_cache.clear()
        self._save_state()

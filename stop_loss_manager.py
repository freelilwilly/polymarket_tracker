"""
Stop-loss manager using active monitoring.

Monitors positions and executes market sells when price drops to stop-loss threshold.
Active when ENABLE_STOP_LOSS=true.

DESIGN: Since Polymarket US API doesn't support STOP orders, we use active monitoring
instead of placing GTC limit orders upfront (which would fill immediately if market
price is above the limit).
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
    """Manages automatic stop-loss protection via active price monitoring."""
    
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
        
        # Track stop-loss thresholds: position_key -> threshold details
        # No orders are placed - we monitor price and execute when triggered
        self.stop_loss_thresholds: dict[str, dict[str, Any]] = {}
        
        # Track position shares to detect averaging up
        self.position_shares_cache: dict[str, float] = {}
        
        self.state_file = "stop_loss_state.json"
        self._load_state()
        
        logger.info(
            f"Stop-loss manager initialized (enabled={Config.ENABLE_STOP_LOSS}, "
            f"percent={Config.STOP_LOSS_PERCENT * 100:.0f}%, mode=active_monitoring)"
        )
    
    def _load_state(self) -> None:
        """Load stop-loss state from disk."""
        if not os.path.exists(self.state_file):
            logger.debug(f"No existing stop-loss state file: {self.state_file}")
            self.stop_loss_thresholds = {}
            return
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                # Migrate old format: "stop_loss_orders" -> "stop_loss_thresholds"
                self.stop_loss_thresholds = (
                    data.get("stop_loss_thresholds") or 
                    data.get("stop_loss_orders") or 
                    {}
                )
                
                # Clean out any "order_id" fields from old format
                for key, threshold in self.stop_loss_thresholds.items():
                    if "order_id" in threshold:
                        del threshold["order_id"]
                
                logger.info(
                    f"Loaded {len(self.stop_loss_thresholds)} stop-loss thresholds from {self.state_file}"
                )
        except Exception as e:
            logger.exception(f"Error loading stop-loss state: {e}")
            self.stop_loss_thresholds = {}
    
    def _save_state(self) -> None:
        """Save stop-loss state to disk."""
        try:
            data = {
                "stop_loss_thresholds": self.stop_loss_thresholds,
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
        
        Active monitoring approach:
        - Tracks stop-loss thresholds for each position
        - Monitors current market price each cycle
        - Executes IOC market sell when price drops to/below threshold
        - Updates threshold on averaging up (position shares increased)
        """
        if not Config.ENABLE_STOP_LOSS:
            return
        
        positions = self.position_manager.get_all_positions()
        
        if not positions:
            # Clean up orphaned thresholds
            if self.stop_loss_thresholds:
                logger.debug("No positions, clearing stop-loss thresholds")
                self.stop_loss_thresholds.clear()
                self.position_shares_cache.clear()
                self._save_state()
            return
        
        # Track positions we've seen this cycle
        seen_position_keys = set()
        
        # Check each position
        for position in positions:
            market_slug = position["market_slug"]
            outcome = position["outcome"]

            # Canonicalize legacy outcome aliases (e.g., team names) to yes/no
            normalized_outcome = outcome
            outcome_lower = str(outcome or "").strip().lower()
            normalization_failed = False
            
            if outcome_lower not in ("yes", "no"):
                resolved = await self.api_client.normalize_outcome_to_yes_no(
                    market_slug,
                    outcome,
                    caller_context="stop_loss_monitor",
                )
                if resolved:
                    self.position_manager.reconcile_outcome_alias(
                        market_slug=market_slug,
                        canonical_outcome=resolved,
                        alias_outcome=outcome,
                    )
                    normalized_outcome = resolved
                else:
                    # Normalization failed - try to infer from US API positions
                    normalization_failed = True
                    logger.debug(
                        f"Outcome normalization unavailable: {market_slug} | {outcome}, "
                        f"attempting fallback via API position lookup"
                    )
                    
                    api_positions = await self.api_client.get_positions()
                    if api_positions:
                        for api_pos in api_positions:
                            api_market = str(api_pos.get("marketSlug") or api_pos.get("market_slug") or "").strip()
                            # Normalize market slug (remove aec- prefix for comparison)
                            if api_market.startswith("aec-"):
                                api_market = api_market[4:]
                            
                            if api_market == market_slug:
                                # Found matching market - use API's outcome field
                                api_outcome = str(api_pos.get("outcome") or "").strip().lower()
                                if api_outcome in ("yes", "no"):
                                    normalized_outcome = api_outcome
                                    
                                    # Extract token_id from raw position data for orderbook lookup
                                    raw_data = api_pos.get("raw") or {}
                                    token_id = (
                                        raw_data.get("tokenId") or 
                                        raw_data.get("token_id") or
                                        raw_data.get("assetId") or
                                        raw_data.get("asset_id")
                                    )
                                    
                                    if token_id:
                                        # Store token_id for price lookup (bypass market metadata)
                                        self.stop_loss_thresholds.setdefault(
                                            self.position_manager.get_position_key(market_slug, api_outcome),
                                            {}
                                        )["token_id"] = str(token_id)
                                    
                                    logger.info(
                                        f"Inferred outcome from API position: {market_slug} | "
                                        f"{outcome} -> {api_outcome}" +
                                        (f" | token_id={token_id}" if token_id else "")
                                    )
                                    
                                    # Reconcile for future use
                                    self.position_manager.reconcile_outcome_alias(
                                        market_slug=market_slug,
                                        canonical_outcome=api_outcome,
                                        alias_outcome=outcome,
                                    )
                                    normalization_failed = False
                                break

            shares = position["shares"]
            entry_price = position.get("entry_price", 0.0)
            
            if entry_price <= 0:
                logger.warning(
                    f"Skipping stop-loss for position with invalid entry price: "
                    f"{market_slug} | {normalized_outcome} | entry=${entry_price:.4f}"
                )
                continue
            
            position_key = self.position_manager.get_position_key(market_slug, normalized_outcome)
            seen_position_keys.add(position_key)
            
            # Calculate stop-loss threshold
            stop_price = self._calculate_stop_price(entry_price)
            
            # Check for averaging up (shares increased) - recalculate threshold
            cached_shares = self.position_shares_cache.get(position_key, 0.0)
            if cached_shares > 0 and shares > cached_shares + 0.01:
                logger.info(
                    f"Averaging up detected: {market_slug} | {normalized_outcome} | "
                    f"shares: {cached_shares:.2f} -> {shares:.2f}, updating stop-loss threshold"
                )
            
            # Update shares cache
            self.position_shares_cache[position_key] = shares
            
            # Get or create threshold tracking
            existing_threshold = self.stop_loss_thresholds.get(position_key)
            
            if not existing_threshold:
                # New position, set up stop-loss threshold
                # Preserve token_id if it was set during fallback
                new_threshold = {
                    "stop_price": stop_price,
                    "entry_price": entry_price,
                    "shares": shares,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                
                # Check if token_id was already stored (from fallback)
                temp_threshold = self.stop_loss_thresholds.get(position_key, {})
                if "token_id" in temp_threshold:
                    new_threshold["token_id"] = temp_threshold["token_id"]
                
                self.stop_loss_thresholds[position_key] = new_threshold
                self._save_state()
                
                logger.info(
                    f"Stop-loss threshold set: {market_slug} | {normalized_outcome} | "
                    f"entry=${entry_price:.4f}, stop=${stop_price:.4f} "
                    f"({Config.STOP_LOSS_PERCENT * 100:.0f}% below entry)"
                )
            else:
                # Check if entry price changed (averaging up)
                tracked_entry = existing_threshold.get("entry_price", 0.0)
                if abs(entry_price - tracked_entry) > 0.001:
                    # Entry price changed, update threshold
                    updated_threshold = {
                        "stop_price": stop_price,
                        "entry_price": entry_price,
                        "shares": shares,
                        "created_at": existing_threshold.get("created_at"),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                    
                    # Preserve token_id if present
                    if "token_id" in existing_threshold:
                        updated_threshold["token_id"] = existing_threshold["token_id"]
                    
                    self.stop_loss_thresholds[position_key] = updated_threshold
                    self._save_state()
                    
                    logger.info(
                        f"Stop-loss threshold updated: {market_slug} | {normalized_outcome} | "
                        f"entry: ${tracked_entry:.4f} -> ${entry_price:.4f}, "
                        f"stop: ${existing_threshold.get('stop_price', 0):.4f} -> ${stop_price:.4f}"
                    )
            
            # === CRITICAL: Check if stop-loss should trigger ===
            # Get current market price
            current_price = await self.api_client.get_best_price(
                market_slug, 
                side="sell",  # We're selling, so get best bid price
                outcome=normalized_outcome
            )
            
            # If metadata unavailable, try token_id fallback
            if current_price is None:
                token_id = existing_threshold.get("token_id") if existing_threshold else None
                if token_id:
                    logger.debug(
                        f"Market metadata unavailable for {market_slug}, "
                        f"using token_id={token_id} for orderbook lookup"
                    )
                    current_price = await self.api_client.get_best_price_by_token_id(
                        token_id=token_id,
                        side="sell"
                    )
            
            if current_price is None:
                if normalization_failed:
                    # Market metadata unavailable - can't monitor stop-loss
                    logger.warning(
                        f"Stop-loss monitoring disabled for {market_slug} | {outcome}: "
                        f"market metadata unavailable, cannot determine price. "
                        f"Position remains unprotected until metadata available."
                    )
                else:
                    logger.debug(
                        f"Cannot check stop-loss: no market price for {market_slug} | {normalized_outcome}"
                    )
                continue
            
            # Check if current price <= stop price (triggered)
            if current_price <= stop_price:
                logger.warning(
                    f"STOP-LOSS TRIGGERED: {market_slug} | {normalized_outcome} | "
                    f"current=${current_price:.4f} <= stop=${stop_price:.4f} | "
                    f"entry=${entry_price:.4f} | shares={shares:.2f}"
                )
                
                # Execute emergency market sell via trade executor
                await self._execute_stop_loss_sell(
                    market_slug=market_slug,
                    outcome=normalized_outcome,
                    shares=shares,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    current_price=current_price,
                )
                
                # Remove threshold after triggering
                self.stop_loss_thresholds.pop(position_key, None)
                self.position_shares_cache.pop(position_key, None)
                self._save_state()
        
        # Clean up orphaned thresholds (tracked but no matching position)
        orphaned_keys = set(self.stop_loss_thresholds.keys()) - seen_position_keys
        if orphaned_keys:
            logger.debug(f"Removing {len(orphaned_keys)} orphaned stop-loss thresholds")
            for position_key in orphaned_keys:
                self.stop_loss_thresholds.pop(position_key, None)
                self.position_shares_cache.pop(position_key, None)
            self._save_state()
    
    async def _execute_stop_loss_sell(
        self,
        market_slug: str,
        outcome: str,
        shares: float,
        entry_price: float,
        stop_price: float,
        current_price: float,
    ):
        """
        Execute stop-loss sell via IOC market order.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            shares: Number of shares to sell
            entry_price: Position entry price
            stop_price: Stop-loss threshold price
            current_price: Current market price
        """
        try:
            logger.warning(
                f"Executing STOP-LOSS SELL: {market_slug} | {outcome} | "
                f"{shares:.2f} shares at market | "
                f"entry=${entry_price:.4f}, stop=${stop_price:.4f}, current=${current_price:.4f}"
            )
            
            # Import here to avoid circular dependency
            from trade_executor import TradeExecutor
            
            # Create trade executor instance (shares api_client)
            trade_executor = TradeExecutor(
                api_client=self.api_client,
                test_mode=False,  # Always execute in live mode
            )
            
            # Execute IOC market sell
            # Price is set aggressively low to ensure fill (YES=0.01, NO=0.99)
            result = await trade_executor.execute_sell(
                market_slug=market_slug,
                outcome=outcome,
                shares=shares,
            )
            
            if result and result.get("success"):
                fill_price = result.get("price", current_price)
                filled_shares = result.get("shares", 0.0)
                
                logger.warning(
                    f"STOP-LOSS EXECUTED: {market_slug} | {outcome} | "
                    f"sold {filled_shares:.2f} shares @ ${fill_price:.4f}"
                )
                
                # Position should be closed by execute_sell, but verify
                position = self.position_manager.get_position(market_slug, outcome)
                if position:
                    # Force close if execute_sell didn't close it
                    self.position_manager.close_position(
                        market_slug,
                        outcome,
                        exit_price=fill_price,
                        reason="stop_loss",
                    )
                
                # Check if this was a loss and record wash sale
                if Config.ENABLE_WASH_SALE_PREVENTION:
                    invested = shares * entry_price
                    exit_value = filled_shares * fill_price
                    pnl = exit_value - invested
                    
                    if pnl < 0:
                        wash_tracker = getattr(self.position_manager, 'wash_sale_tracker', None)
                        if wash_tracker:
                            wash_tracker.record_loss_sale(
                                market_slug=market_slug,
                                outcome=outcome,
                                realized_pnl=pnl,
                                exit_price=fill_price,
                            )
                            logger.info(
                                f"Stop-loss triggered wash sale block: "
                                f"{market_slug} | {outcome} | PnL=${pnl:.2f}"
                            )
            else:
                reason = result.get("reason") if result else "unknown"
                logger.error(
                    f"STOP-LOSS SELL FAILED: {market_slug} | {outcome} | "
                    f"reason={reason}"
                )
        
        except Exception as e:
            logger.exception(
                f"Error executing stop-loss sell: {market_slug} | {outcome} | {e}"
            )
    
    async def cancel_stop_loss_order(
        self,
        market_slug: str,
        outcome: str,
    ) -> bool:
        """
        Remove stop-loss threshold for a position.
        
        Note: Since we use active monitoring (no orders), this just
        removes the threshold from tracking.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            
        Returns:
            True if threshold was removed
        """
        position_key = self.position_manager.get_position_key(market_slug, outcome)
        threshold_info = self.stop_loss_thresholds.get(position_key)
        
        if not threshold_info:
            return False
        
        self.stop_loss_thresholds.pop(position_key, None)
        self.position_shares_cache.pop(position_key, None)
        self._save_state()
        
        logger.info(
            f"Stop-loss threshold removed: {market_slug} | {outcome} | "
            f"stop=${threshold_info.get('stop_price', 0):.4f}"
        )
        
        return True
    
    async def cancel_all_stop_loss_orders(self):
        """Clear all stop-loss thresholds."""
        if not self.stop_loss_thresholds:
            return
        
        logger.info(f"Clearing {len(self.stop_loss_thresholds)} stop-loss thresholds...")
        
        self.stop_loss_thresholds.clear()
        self.position_shares_cache.clear()
        self._save_state()

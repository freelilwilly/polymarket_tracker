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
            
            logger.debug(
                f"Stop-loss check: Processing position market_slug='{market_slug}' "
                f"outcome='{outcome}' shares={position.get('shares', 0):.2f}"
            )

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
                    logger.info(
                        f"Outcome normalization unavailable: {market_slug} | {outcome}, "
                        f"attempting fallback via API position lookup"
                    )
                    
                    api_positions = await self.api_client.get_positions()
                    logger.info(f"Fallback: Retrieved {len(api_positions) if api_positions else 0} positions from US API")
                    
                    if api_positions:
                        api_markets_seen = []
                        for api_pos in api_positions:
                            api_market = str(api_pos.get("marketSlug") or api_pos.get("market_slug") or "").strip()
                            api_markets_seen.append(api_market)
                            
                            # Normalize market slug (remove aec- prefix for comparison)
                            normalized_api_market = api_market[4:] if api_market.startswith("aec-") else api_market
                            
                            logger.debug(
                                f"Fallback: Checking position: api_market='{api_market}' "
                                f"(normalized='{normalized_api_market}') vs target='{market_slug}' | "
                                f"outcome='{api_pos.get('outcome')}'"
                            )
                            
                            if normalized_api_market == market_slug:
                                # Found matching market - dump raw data to understand structure
                                api_outcome = str(api_pos.get("outcome") or "").strip().lower()
                                raw_data = api_pos.get("raw") or {}
                                
                                logger.info(
                                    f"Fallback: Found matching position! api_outcome='{api_outcome}' | "
                                    f"raw_keys={list(raw_data.keys())}"
                                )
                                logger.info(
                                    f"Fallback: Raw payload dump: {raw_data}"
                                )
                                
                                # Try to infer yes/no from position data
                                # Option 1: Check if outcome is already yes/no
                                token_id = None  # Initialize to avoid reference errors
                                
                                if api_outcome in ("yes", "no"):
                                    normalized_outcome = api_outcome
                                else:
                                    # Option 2: Check for token index or asset info
                                    # In CTF markets: index 0 = YES, index 1 = NO
                                    token_index = raw_data.get("outcomeIndex") or raw_data.get("outcome_index")
                                    
                                    # Option 3: Check metadata for more info
                                    metadata = raw_data.get("marketMetadata") or {}
                                    if isinstance(metadata, dict):
                                        meta_outcome = str(metadata.get("outcome") or "").strip().lower()
                                        meta_side = str(metadata.get("side") or "").strip().lower()
                                        meta_index = metadata.get("outcomeIndex") or metadata.get("index")
                                        
                                        logger.info(
                                            f"Fallback: metadata.outcome='{meta_outcome}', "
                                            f"metadata.side='{meta_side}', metadata.index={meta_index}"
                                        )
                                        
                                        # Check if metadata has yes/no or side info
                                        if meta_outcome in ("yes", "no"):
                                            normalized_outcome = meta_outcome
                                        elif meta_side in ("yes", "no"):
                                            normalized_outcome = meta_side
                                        elif meta_index is not None:
                                            normalized_outcome = "yes" if int(meta_index) == 0 else "no"
                                        elif token_index is not None:
                                            normalized_outcome = "yes" if int(token_index) == 0 else "no"
                                        
                                        # Option 4: Infer from team abbreviation in slug
                                        # For sports markets format: sport-team1-team2-date
                                        # Team 1 = YES (token 0), Team 2 = NO (token 1)
                                        team_data = metadata.get("team") or {}
                                        team_abbr = str(team_data.get("abbreviation") or "").strip().lower()
                                        
                                        if team_abbr and normalized_outcome not in ("yes", "no"):
                                            logger.info(f"Fallback: team.abbreviation='{team_abbr}'")
                                            
                                            # Parse market slug to extract team codes
                                            # Expected format: sport-team1-team2-date
                                            slug_parts = market_slug.split("-")
                                            if len(slug_parts) >= 4:
                                                # Extract team codes (positions 1 and 2 after sport)
                                                team1_code = slug_parts[1].lower()
                                                team2_code = slug_parts[2].lower()
                                                
                                                logger.info(
                                                    f"Fallback: Parsed slug teams: team1='{team1_code}' (YES), "
                                                    f"team2='{team2_code}' (NO)"
                                                )
                                                
                                                # Match team abbreviation to slug position
                                                if team_abbr == team1_code:
                                                    normalized_outcome = "yes"
                                                    logger.info(
                                                        f"Fallback: Team '{team_abbr}' matches position 1 → YES"
                                                    )
                                                elif team_abbr == team2_code:
                                                    normalized_outcome = "no"
                                                    logger.info(
                                                        f"Fallback: Team '{team_abbr}' matches position 2 → NO"
                                                    )
                                    
                                if normalized_outcome not in ("yes", "no"):
                                    logger.warning(
                                        f"Fallback: Could not determine yes/no from position metadata. "
                                        f"api_outcome='{api_outcome}', attempting token_id lookup..."
                                    )
                                    
                                    # Try querying with eventSlug from metadata
                                    event_slug = raw_data.get("marketMetadata", {}).get("eventSlug")
                                    if event_slug and str(event_slug).strip():
                                        logger.info(f"Fallback: Attempting eventSlug lookup: {event_slug}")
                                        event_market_info = await self.api_client.get_market_info(str(event_slug).strip())
                                        
                                        if event_market_info:
                                            tokens = event_market_info.get("tokens", [])
                                            logger.info(f"Fallback: Found market via eventSlug with {len(tokens)} tokens")
                                            
                                            # We still need to determine which token corresponds to this outcome
                                            # For now, skip this path - we already have team abbr logic above
                                
                                # Extract token_id for orderbook queries (if not already done above)
                                if not token_id:
                                    token_id = (
                                        raw_data.get("tokenId") or 
                                        raw_data.get("token_id") or
                                        raw_data.get("assetId") or
                                        raw_data.get("asset_id") or
                                        raw_data.get("asset")
                                    )
                                
                                # If still no token_id but we have eventSlug and normalized outcome, try to get tokens
                                if not token_id and normalized_outcome in ("yes", "no"):
                                    event_slug = raw_data.get("marketMetadata", {}).get("eventSlug")
                                    if event_slug:
                                        market_info = await self.api_client.get_market_info(str(event_slug).strip())
                                        if market_info:
                                            tokens = market_info.get("tokens", [])
                                            # YES = token 0, NO = token 1
                                            token_idx = 0 if normalized_outcome == "yes" else 1
                                            if len(tokens) > token_idx:
                                                token = tokens[token_idx]
                                                token_id = token.get("token_id") or token.get("tokenId")
                                                logger.info(
                                                    f"Fallback: Extracted token_id={token_id} from eventSlug market info "
                                                    f"(index {token_idx} for {normalized_outcome})"
                                                )
                                
                                logger.info(
                                    f"Fallback: Extracted token_id='{token_id}' from raw_data keys: {list(raw_data.keys())}"
                                )
                                
                                # Final check: did we successfully determine yes/no?
                                if normalized_outcome not in ("yes", "no"):
                                    logger.warning(
                                        f"Fallback: Exhausted all methods to determine yes/no. "
                                        f"Position cannot be protected."
                                    )
                                    break
                                
                                if token_id:
                                    # Store token_id for price lookup (bypass market metadata)
                                    self.stop_loss_thresholds.setdefault(
                                        self.position_manager.get_position_key(market_slug, normalized_outcome),
                                        {}
                                    )["token_id"] = str(token_id)
                                
                                logger.info(
                                    f"Inferred outcome from API position: {market_slug} | "
                                    f"{outcome} -> {normalized_outcome}" +
                                    (f" | token_id={token_id}" if token_id else "")
                                )
                                
                                # Reconcile for future use
                                self.position_manager.reconcile_outcome_alias(
                                    market_slug=market_slug,
                                    canonical_outcome=normalized_outcome,
                                    alias_outcome=outcome,
                                )
                                normalization_failed = False
                                break
                        
                        if normalization_failed:
                            logger.warning(
                                f"Fallback: No matching position found for '{market_slug}'. "
                                f"API markets seen: {api_markets_seen}"
                            )
            
            # === CRITICAL FIX: Extract token_id for ALL positions ===
            # Even if outcome is already yes/no, we need token_id for price monitoring
            # when Gamma API metadata is unavailable (e.g., Masters markets)
            position_key = self.position_manager.get_position_key(market_slug, normalized_outcome)
            existing_threshold = self.stop_loss_thresholds.get(position_key)
            
            # Only fetch token_id if we don't already have one
            if not (existing_threshold and existing_threshold.get("token_id")):
                try:
                    api_positions = await self.api_client.get_positions()
                    if api_positions:
                        for api_pos in api_positions:
                            api_market = str(api_pos.get("marketSlug") or api_pos.get("market_slug") or "").strip()
                            # Strip all known API prefixes for comparison (aec-, atc-, asc-, asm-, acm-, acx-)
                            normalized_api_market = api_market
                            for prefix in ["aec-", "atc-", "asc-", "asm-", "acm-", "acx-"]:
                                if normalized_api_market.startswith(prefix):
                                    normalized_api_market = normalized_api_market[len(prefix):]
                                    break
                            
                            # Also normalize market_slug for comparison
                            normalized_market_slug = market_slug
                            for prefix in ["aec-", "atc-", "asc-", "asm-", "acm-", "acx-"]:
                                if normalized_market_slug.startswith(prefix):
                                    normalized_market_slug = normalized_market_slug[len(prefix):]
                                    break
                            
                            if normalized_api_market == normalized_market_slug:
                                raw_data = api_pos.get("raw") or {}
                                token_id = (
                                    raw_data.get("tokenId") or 
                                    raw_data.get("token_id") or
                                    raw_data.get("assetId") or
                                    raw_data.get("asset_id") or
                                    raw_data.get("asset")
                                )
                                
                                # If still no token_id, try extracting from eventSlug market info
                                if not token_id and normalized_outcome in ("yes", "no"):
                                    event_slug = raw_data.get("marketMetadata", {}).get("eventSlug")
                                    if event_slug:
                                        market_info = await self.api_client.get_market_info(str(event_slug).strip())
                                        if market_info:
                                            tokens = market_info.get("tokens", [])
                                            token_idx = 0 if normalized_outcome == "yes" else 1
                                            if len(tokens) > token_idx:
                                                token = tokens[token_idx]
                                                token_id = token.get("token_id") or token.get("tokenId")
                                
                                if token_id:
                                    # Store token_id for price lookup fallback
                                    self.stop_loss_thresholds.setdefault(position_key, {})["token_id"] = str(token_id)
                                    logger.info(
                                        f"Extracted token_id for price monitoring: {market_slug} | "
                                        f"{normalized_outcome} | token_id={token_id}"
                                    )
                                break
                except Exception as e:
                    logger.warning(f"Failed to extract token_id for {market_slug}: {e}")

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
            
            # Check if we have a complete threshold (not just token_id)
            has_complete_threshold = existing_threshold and "entry_price" in existing_threshold
            
            if not has_complete_threshold:
                # New position or incomplete threshold, set up complete stop-loss threshold
                new_threshold = {
                    "stop_price": stop_price,
                    "entry_price": entry_price,
                    "shares": shares,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                
                # Preserve token_id if it was already extracted
                if existing_threshold and "token_id" in existing_threshold:
                    new_threshold["token_id"] = existing_threshold["token_id"]
                
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
                # Re-fetch threshold to get latest token_id
                current_threshold = self.stop_loss_thresholds.get(position_key)
                token_id = current_threshold.get("token_id") if current_threshold else None
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
                # CRITICAL: Always log WARNING for positions with stop-loss thresholds
                # These positions are supposed to be protected but can't be monitored
                logger.warning(
                    f"Stop-loss monitoring FAILED for {market_slug} | {normalized_outcome}: "
                    f"cannot determine current price (metadata unavailable, token_id lookup failed). "
                    f"Position with entry=${entry_price:.4f}, stop=${stop_price:.4f} remains UNPROTECTED."
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

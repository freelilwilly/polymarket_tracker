"""
Position tracking and management.

Tracks:
- Open positions per market
- Entry prices, current values
- P&L (realized and unrealized)
- Per-market exposure caps (25% of balance)
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from api_client import PolymarketAPIClient
from config import Config
from utils import to_float

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages position tracking and sizing."""
    
    def __init__(self, api_client: PolymarketAPIClient):
        """
        Initialize position manager.
        
        Args:
            api_client: API client instance
        """
        self.api_client = api_client
        self.positions: dict[str, dict[str, Any]] = {}
        self.state_file = "positions_state.json"
        self.balance: Optional[float] = None
        self.buying_power: Optional[float] = None
        self._load_state()
    
    def _load_state(self):
        """Load positions from state file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    self.positions = data.get("positions", {})
                    self.balance = data.get("balance")
                    self.buying_power = data.get("buying_power")
                    logger.info(f"Loaded {len(self.positions)} positions from state file")
            except Exception as e:
                logger.exception(f"Error loading positions state: {e}")
                self.positions = {}
    
    def _save_state(self):
        """Save positions to state file."""
        try:
            data = {
                "positions": self.positions,
                "balance": self.balance,
                "buying_power": self.buying_power,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            
            # Write to temp file first, then rename (atomic operation)
            temp_file = f"{self.state_file}.tmp"
            with open(temp_file, "w") as f:
                json.dump(data, f, indent=2)
            
            # Replace old file with new one
            if os.path.exists(self.state_file):
                os.remove(self.state_file)
            os.rename(temp_file, self.state_file)
            
        except Exception as e:
            logger.exception(f"Error saving positions state: {e}")
    
    async def update_balance(self) -> Optional[float]:
        """
        Fetch and update account value from API.
        
        Returns:
            Current total account value (cash + marked positions) or None on error
        """
        overview = await self.api_client.get_account_overview()
        if overview is not None:
            total_value = to_float(overview.get("total_account_value"), default=0.0)
            buying_power = to_float(overview.get("buying_power"), default=0.0)

            if total_value <= 0:
                total_value = await self.api_client.get_balance() or 0.0

            self.balance = total_value if total_value > 0 else None
            self.buying_power = buying_power if buying_power > 0 else None
            self._save_state()
            logger.debug(
                f"Account value updated: ${self.balance:.2f}"
                + (f" | Buying power: ${self.buying_power:.2f}" if self.buying_power is not None else "")
            )
        else:
            logger.warning("Failed to fetch balance")
        
        return self.balance
    
    def get_position_key(self, market_slug: str, outcome: str) -> str:
        """
        Generate unique position key.
        
        Normalizes market_slug to canonical form (without aec- prefix) to ensure
        positions match regardless of whether API returns slug with or without prefix.
        
        Args:
            market_slug: Market slug
            outcome: Outcome ("yes" or "no")
            
        Returns:
            Position key
        """
        # Normalize slug to canonical form (without aec- prefix)
        normalized_slug = market_slug[4:] if market_slug.startswith("aec-") else market_slug
        return f"{normalized_slug}|{outcome.lower()}"

    def reconcile_outcome_alias(
        self,
        market_slug: str,
        canonical_outcome: str,
        alias_outcome: Optional[str] = None,
    ) -> bool:
        """
        Reconcile a legacy/raw outcome key to canonical yes/no outcome key.

        If an alias position exists (for example a team name outcome), this method
        migrates it to the canonical key. If both alias and canonical keys exist,
        it merges alias position data into canonical to prevent duplicate exposure.

        Args:
            market_slug: Market slug
            canonical_outcome: Canonical outcome ("yes" or "no")
            alias_outcome: Raw/non-canonical outcome text to reconcile (optional)

        Returns:
            True if any state change occurred, else False
        """
        canonical = (canonical_outcome or "").strip().lower()
        alias = (alias_outcome or "").strip().lower()

        if not canonical or not alias or canonical == alias:
            return False

        canonical_key = self.get_position_key(market_slug, canonical)
        alias_key = self.get_position_key(market_slug, alias)

        if alias_key not in self.positions:
            return False

        alias_pos = self.positions.get(alias_key)
        if alias_pos is None:
            return False

        if canonical_key not in self.positions:
            # Simple migration when canonical key does not exist yet.
            self.positions.pop(alias_key, None)
            alias_pos["outcome"] = canonical
            self.positions[canonical_key] = alias_pos
            self._save_state()
            logger.info(
                f"Migrated legacy position outcome alias: {market_slug} | {alias} -> {canonical}"
            )
            return True

        # Merge duplicate alias + canonical positions into a single canonical entry.
        canonical_pos = self.positions.get(canonical_key)
        if canonical_pos is None:
            return False

        canonical_shares = to_float(canonical_pos.get("shares"), default=0.0)
        alias_shares = to_float(alias_pos.get("shares"), default=0.0)
        canonical_invested = to_float(canonical_pos.get("invested"), default=0.0)
        alias_invested = to_float(alias_pos.get("invested"), default=0.0)

        merged_shares = canonical_shares + alias_shares
        merged_invested = canonical_invested + alias_invested

        canonical_pos["shares"] = merged_shares
        canonical_pos["invested"] = merged_invested
        canonical_pos["outcome"] = canonical

        # Preserve earliest open timestamp when both exist.
        canonical_opened_at = canonical_pos.get("opened_at")
        alias_opened_at = alias_pos.get("opened_at")
        if alias_opened_at and (not canonical_opened_at or alias_opened_at < canonical_opened_at):
            canonical_pos["opened_at"] = alias_opened_at

        # If canonical trader is missing, carry over alias trader for traceability.
        if not canonical_pos.get("monitored_trader") and alias_pos.get("monitored_trader"):
            canonical_pos["monitored_trader"] = alias_pos.get("monitored_trader")

        self.positions.pop(alias_key, None)
        self.positions[canonical_key] = canonical_pos
        self._save_state()

        logger.warning(
            f"Merged duplicate position aliases: {market_slug} | {alias} + {canonical} -> {canonical} "
            f"({merged_shares:.2f} shares, ${merged_invested:.2f} invested)"
        )
        return True
    
    def has_position(self, market_slug: str, outcome: str) -> bool:
        """
        Check if we have an open position.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            
        Returns:
            True if position exists
        """
        key = self.get_position_key(market_slug, outcome)
        return key in self.positions
    
    def get_position(self, market_slug: str, outcome: str) -> Optional[dict[str, Any]]:
        """
        Get position details.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            
        Returns:
            Position dict or None
        """
        key = self.get_position_key(market_slug, outcome)
        return self.positions.get(key)
    
    def get_market_exposure(self, market_slug: str) -> float:
        """
        Get total exposure (invested capital) across all positions in a market.
        
        Args:
            market_slug: Market slug
            
        Returns:
            Total invested amount
        """
        total = 0.0
        for key, pos in self.positions.items():
            if pos["market_slug"] == market_slug:
                total += pos.get("invested", 0.0)
        
        return total
    
    def can_open_position(
        self,
        market_slug: str,
        investment_amount: float,
    ) -> tuple[bool, str]:
        """
        Check if we can open a new position given market exposure caps.
        
        Per-market exposure cap: 25% of balance.
        
        Args:
            market_slug: Market slug
            investment_amount: Proposed investment amount
            
        Returns:
            Tuple of (can_open, reason)
        """
        if self.balance is None or self.balance <= 0:
            return False, "Balance unknown or zero"
        
        # Get current market exposure
        current_exposure = self.get_market_exposure(market_slug)
        
        # Check per-market cap (25% of balance)
        # Exposure cap is based on total account value, not just current buying power.
        market_cap = self.balance * Config.MAX_POSITION_SIZE_PER_MARKET
        if current_exposure + investment_amount > market_cap:
            return False, (
                f"Market exposure cap exceeded: current=${current_exposure:.2f}, "
                f"proposed=${investment_amount:.2f}, cap=${market_cap:.2f}"
            )
        
        return True, "OK"
    
    def open_position(
        self,
        market_slug: str,
        outcome: str,
        shares: float,
        price: float,
        monitored_trader: Optional[str] = None,
    ):
        """
        Record a new position.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            shares: Number of shares
            price: Entry price
            monitored_trader: Trader wallet we copied (optional)
        """
        key = self.get_position_key(market_slug, outcome)

        # Calculate invested amount for this fill (shares * price)
        new_invested = shares * price

        existing = self.positions.get(key)
        if existing:
            old_shares = to_float(existing.get("shares"), default=0.0)
            old_invested = to_float(existing.get("invested"), default=0.0)

            total_shares = old_shares + shares
            total_invested = old_invested + new_invested
            avg_entry = (total_invested / total_shares) if total_shares > 0 else price

            existing["shares"] = total_shares
            existing["invested"] = total_invested
            existing["entry_price"] = avg_entry
            existing["outcome"] = outcome.lower()

            # Keep first opened timestamp; refresh copied-trader source if provided.
            if monitored_trader:
                existing["monitored_trader"] = monitored_trader

            self.positions[key] = existing
            self._save_state()

            logger.info(
                f"Added to position: {market_slug} | {outcome} | "
                f"+{shares:.2f} shares @ ${price:.4f} (${new_invested:.2f}) | "
                f"Total={total_shares:.2f} shares, Avg=${avg_entry:.4f}, Invested=${total_invested:.2f}"
            )
            return

        position = {
            "market_slug": market_slug[4:] if market_slug.startswith("aec-") else market_slug,
            "outcome": outcome.lower(),
            "shares": shares,
            "entry_price": price,
            "invested": new_invested,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "monitored_trader": monitored_trader,
        }

        self.positions[key] = position
        self._save_state()

        logger.info(
            f"Opened position: {market_slug} | {outcome} | "
            f"{shares:.2f} shares @ ${price:.4f} = ${new_invested:.2f}"
        )
    
    def close_position(
        self,
        market_slug: str,
        outcome: str,
        exit_price: float,
        reason: str = "manual",
    ) -> Optional[dict[str, Any]]:
        """
        Close a position and calculate P&L.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            exit_price: Exit price
            reason: Close reason
            
        Returns:
            Position summary dict or None if not found
        """
        key = self.get_position_key(market_slug, outcome)
        
        if key not in self.positions:
            logger.warning(f"Cannot close position: not found ({key})")
            return None
        
        position = self.positions.pop(key)
        
        # Calculate P&L
        shares = position["shares"]
        entry_price = position["entry_price"]
        invested = position.get("invested", shares * entry_price)
        
        exit_value = shares * exit_price
        pnl = exit_value - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0.0
        
        position.update({
            "exit_price": exit_price,
            "exit_value": exit_value,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "close_reason": reason,
        })
        
        self._save_state()
        
        logger.info(
            f"Closed position: {market_slug} | {outcome} | "
            f"P&L=${pnl:+.2f} ({pnl_pct:+.2f}%) | Reason: {reason}"
        )
        
        return position
    
    def update_position_shares(
        self,
        market_slug: str,
        outcome: str,
        new_shares: float,
        new_invested: Optional[float] = None,
    ):
        """
        Update position share count (for partial closes).
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            new_shares: New share count
            new_invested: New invested amount (optional, will be calculated if not provided)
        """
        key = self.get_position_key(market_slug, outcome)
        
        if key not in self.positions:
            logger.warning(f"Cannot update position: not found ({key})")
            return
        
        position = self.positions[key]
        old_shares = position["shares"]
        
        # Update shares
        position["shares"] = new_shares
        
        # Update invested amount
        if new_invested is not None:
            position["invested"] = new_invested
        else:
            # Proportional reduction
            if old_shares > 0:
                position["invested"] = position["invested"] * (new_shares / old_shares)
        
        self._save_state()
        
        logger.info(
            f"Updated position: {market_slug} | {outcome} | "
            f"{old_shares:.2f} -> {new_shares:.2f} shares"
        )
    
    async def sync_positions_with_api(self):
        """
        Synchronize local positions with API positions.
        
        Handles:
        - Positions closed externally
        - Position size mismatches
        """
        api_positions = await self.api_client.get_positions()
        
        if api_positions is None:
            logger.warning("Cannot sync positions: API returned None")
            return
        
        # Build lookup from API positions
        # Map API positions by their NORMALIZED keys only for importing
        # We'll handle legacy key matching separately during the update phase
        api_lookup: dict[str, dict[str, Any]] = {}
        api_positions_by_market: dict[str, dict[str, Any]] = {}  # For matching: "market|outcome" -> position
        
        for api_pos in api_positions:
            market_slug = api_pos.get("marketSlug") or api_pos.get("market_slug")
            raw_outcome = str(api_pos.get("outcome", "")).strip()
            outcome_lower = raw_outcome.lower()
            shares = to_float(api_pos.get("size") or api_pos.get("shares"), default=0.0)
            
            if not market_slug or not raw_outcome:
                continue

            raw_payload = api_pos.get("raw") if isinstance(api_pos.get("raw"), dict) else {}
            avg_px = to_float(
                (raw_payload.get("avgPx") or {}).get("value") if isinstance(raw_payload.get("avgPx"), dict) else raw_payload.get("avgPx"),
                default=0.0,
            )
            cost_value = to_float(
                (raw_payload.get("cost") or {}).get("value") if isinstance(raw_payload.get("cost"), dict) else raw_payload.get("cost"),
                default=0.0,
            )
            invested = cost_value if cost_value > 0 else (shares * avg_px if avg_px > 0 else 0.0)

            # Try to normalize outcome (team name → yes/no)
            normalized_outcome = None
            if outcome_lower not in ("yes", "no"):
                normalized_outcome = await self.api_client.normalize_outcome_to_yes_no(market_slug, raw_outcome)
            
            # Use normalized outcome if available, otherwise use raw lowercase
            primary_outcome = normalized_outcome if normalized_outcome else outcome_lower
            
            # Normalize market slug (remove aec- prefix)
            normalized_market = market_slug[4:] if market_slug.startswith("aec-") else market_slug
            
            position_data = {
                "market_slug": normalized_market,
                "outcome": primary_outcome,
                "shares": shares,
                "avg_px": avg_px,
                "invested": invested,
                "api_data": api_pos,
                "raw_outcome": outcome_lower,  # Keep raw outcome for matching
            }
            
            # Create key with normalized outcome (preferred) - ONLY ONE KEY per position
            primary_key = self.get_position_key(normalized_market, primary_outcome)
            api_lookup[primary_key] = position_data
            
            # Also track by market+outcome for flexible matching (handles both normalized and raw outcomes)
            # This is ONLY for matching, not importing
            market_id_normalized = f"{normalized_market}|{primary_outcome}"
            market_id_raw = f"{normalized_market}|{outcome_lower}"
            api_positions_by_market[market_id_normalized] = position_data
            if market_id_raw != market_id_normalized:
                api_positions_by_market[market_id_raw] = position_data
        
        # Track which API positions have been matched to local positions
        # to prevent duplicate imports
        matched_api_positions = set()
        
        # Find positions closed externally (in local state but not in API)
        # Check by both exact key match and flexible market+outcome matching
        to_remove = []
        for key, local_pos in self.positions.items():
            local_market = local_pos['market_slug']
            local_outcome = local_pos['outcome']
            
            # Try various matching strategies
            found_in_api = False
            
            # Strategy 1: Exact key match in api_lookup
            if key in api_lookup:
                found_in_api = True
                matched_api_positions.add(key)
            
            # Strategy 2: Try market+outcome matching (handles outcome normalization differences)
            if not found_in_api:
                market_id = f"{local_market}|{local_outcome}"
                if market_id in api_positions_by_market:
                    found_in_api = True
                    # Mark the corresponding normalized key as matched
                    api_data = api_positions_by_market[market_id]
                    matched_key = self.get_position_key(api_data["market_slug"], api_data["outcome"])
                    matched_api_positions.add(matched_key)
            
            # Strategy 3: Try with aec- prefix variations
            if not found_in_api:
                alt_market = f"aec-{local_market}" if not local_market.startswith("aec-") else local_market[4:]
                market_id_alt = f"{alt_market}|{local_outcome}"
                if market_id_alt in api_positions_by_market:
                    found_in_api = True
                    api_data = api_positions_by_market[market_id_alt]
                    matched_key = self.get_position_key(api_data["market_slug"], api_data["outcome"])
                    matched_api_positions.add(matched_key)
            
            if not found_in_api:
                logger.warning(
                    f"Position closed externally: {local_market} | {local_outcome} (not found in API)"
                )
                to_remove.append(key)
        
        # Remove closed positions
        for key in to_remove:
            self.positions.pop(key, None)
        
        # Update share counts for existing positions
        # Use flexible matching to handle positions with mismatched keys (legacy formats)
        migrated = 0
        for key, local_pos in list(self.positions.items()):
            api_data = None
            matched_api_key = None
            
            local_market = local_pos['market_slug']
            local_outcome = local_pos['outcome']
            
            # Strategy 1: Exact key match
            if key in api_lookup:
                api_data = api_lookup[key]
                matched_api_key = key
                matched_api_positions.add(key)
            
            # Strategy 2: Market+outcome matching (handles outcome normalization)
            if not api_data:
                market_id = f"{local_market}|{local_outcome}"
                if market_id in api_positions_by_market:
                    api_data = api_positions_by_market[market_id]
                    matched_api_key = self.get_position_key(api_data["market_slug"], api_data["outcome"])
                    matched_api_positions.add(matched_api_key)
            
            # Strategy 3: Try with aec- prefix variations
            if not api_data:
                alt_market = f"aec-{local_market}" if not local_market.startswith("aec-") else local_market[4:]
                market_id_alt = f"{alt_market}|{local_outcome}"
                if market_id_alt in api_positions_by_market:
                    api_data = api_positions_by_market[market_id_alt]
                    matched_api_key = self.get_position_key(api_data["market_slug"], api_data["outcome"])
                    matched_api_positions.add(matched_api_key)
            
            if api_data:
                api_shares = api_data["shares"]
                local_shares = local_pos["shares"]
                
                # MIGRATION: If this position uses a legacy key or outcome, migrate it
                correct_key = self.get_position_key(api_data["market_slug"], api_data["outcome"])
                
                if key != correct_key:
                    logger.info(
                        f"Migrating position from key '{key}' to '{correct_key}'"
                    )
                    # Remove old position
                    old_data = self.positions.pop(key)
                    # Update data with normalized values
                    old_data['outcome'] = api_data["outcome"]
                    old_data['market_slug'] = api_data["market_slug"]
                    # Create new position with correct key
                    self.positions[correct_key] = old_data
                    migrated += 1
                    # Update references for share comparison below
                    key = correct_key
                    local_pos = old_data
                
                # Allow small floating-point discrepancies (0.01 shares)
                if abs(api_shares - local_shares) > 0.01:
                    logger.warning(
                        f"Share mismatch: {local_pos['market_slug']} | {local_pos['outcome']} | "
                        f"Local={local_shares:.2f}, API={api_shares:.2f}. Updating to API value."
                    )
                    
                    if api_shares == 0:
                        # Position fully closed
                        to_remove.append(key)
                    else:
                        # Update share count
                        self.update_position_shares(
                            local_pos["market_slug"],
                            local_pos["outcome"],
                            api_shares,
                            new_invested=api_data.get("invested"),
                        )

        # Import API positions that are missing locally so local state fully reflects
        # exchange holdings (important for liquidation sizing correctness).
        # Only import positions that weren't matched to any local position
        imported = 0
        for key, api_pos in api_lookup.items():
            # Skip if this position was already matched to a local position
            if key in matched_api_positions:
                continue
            
            # Skip if position already exists with this exact key
            if key in self.positions:
                continue

            shares = to_float(api_pos.get("shares"), default=0.0)
            if shares <= 0:
                continue

            entry_price = to_float(api_pos.get("avg_px"), default=0.0)
            if entry_price <= 0:
                entry_price = 0.5

            invested = to_float(api_pos.get("invested"), default=0.0)
            if invested <= 0:
                invested = shares * entry_price

            self.positions[key] = {
                "market_slug": api_pos["market_slug"],
                "outcome": api_pos["outcome"],
                "shares": shares,
                "entry_price": entry_price,
                "invested": invested,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "monitored_trader": None,
            }
            imported += 1
            logger.debug(f"Imported new API position: {key}")
        
        # Remove any positions with zero shares
        for key in to_remove:
            self.positions.pop(key, None)
        
        # FINAL DEDUPLICATION: Remove any duplicate positions (same market+outcome, different keys)
        # This can happen if there were legacy positions that weren't caught by migration
        seen_positions = {}  # market_slug|outcome -> key
        duplicates_to_remove = []
        for key, pos in self.positions.items():
            pos_id = f"{pos['market_slug']}|{pos['outcome']}"
            if pos_id in seen_positions:
                # Duplicate found - keep the one with more shares, or the first one if equal
                existing_key = seen_positions[pos_id]
                existing_shares = self.positions[existing_key]['shares']
                current_shares = pos['shares']
                
                if current_shares > existing_shares:
                    # Current position has more shares, remove the existing one
                    duplicates_to_remove.append(existing_key)
                    seen_positions[pos_id] = key
                    logger.info(f"Duplicate position cleaned, keeping {key} ({current_shares} shares) over {existing_key} ({existing_shares} shares)")
                else:
                    # Existing position has more/equal shares, remove current one
                    duplicates_to_remove.append(key)
                    logger.info(f"Duplicate position cleaned, keeping {existing_key} ({existing_shares} shares) over {key} ({current_shares} shares)")
            else:
                seen_positions[pos_id] = key
        
        for key in duplicates_to_remove:
            self.positions.pop(key, None)
        
        if to_remove or imported or migrated or duplicates_to_remove:
            self._save_state()
            if to_remove:
                logger.info(f"Removed {len(to_remove)} stale positions during sync")
            if imported:
                logger.info(f"Imported {imported} API position(s) missing from local state")
            if migrated:
                logger.info(f"Migrated {migrated} position(s) from team names to yes/no outcomes")
            if duplicates_to_remove:
                logger.info(f"Removed {len(duplicates_to_remove)} duplicate position(s)")
    
    def get_all_positions(self) -> list[dict[str, Any]]:
        """
        Get all open positions.
        
        Returns:
            List of position dicts
        """
        return list(self.positions.values())
    
    async def get_position_value(
        self,
        market_slug: str,
        outcome: str,
    ) -> Optional[float]:
        """
        Get current value of a position based on market price.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            
        Returns:
            Current value or None
        """
        position = self.get_position(market_slug, outcome)
        if not position:
            return None
        
        # Get current market price (best bid for selling)
        current_price = await self.api_client.get_best_price(market_slug, "sell", outcome)
        if current_price is None:
            return None
        
        shares = position["shares"]
        return shares * current_price
    
    async def get_position_pnl(
        self,
        market_slug: str,
        outcome: str,
    ) -> Optional[dict[str, float]]:
        """
        Calculate unrealized P&L for a position.
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            
        Returns:
            Dict with pnl, pnl_pct, current_value, or None
        """
        position = self.get_position(market_slug, outcome)
        if not position:
            return None
        
        current_value = await self.get_position_value(market_slug, outcome)
        if current_value is None:
            return None
        
        invested = position.get("invested", position["shares"] * position["entry_price"])
        pnl = current_value - invested
        pnl_pct = (pnl / invested * 100) if invested > 0 else 0.0
        
        return {
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "current_value": current_value,
            "invested": invested,
        }
    
    def get_summary(self) -> dict[str, Any]:
        """
        Get portfolio summary.
        
        Returns:
            Summary dict with total positions, invested, etc.
        """
        total_positions = len(self.positions)
        total_invested = sum(
            pos.get("invested", pos["shares"] * pos["entry_price"])
            for pos in self.positions.values()
        )
        
        # Get unique markets
        unique_markets = set(pos["market_slug"] for pos in self.positions.values())
        
        return {
            "total_positions": total_positions,
            "unique_markets": len(unique_markets),
            "total_invested": total_invested,
            "balance": self.balance,
            "buying_power": self.buying_power,
            "available": self.buying_power if self.buying_power is not None else ((self.balance - total_invested) if self.balance else None),
        }
    
    async def get_total_positions_value(self) -> float:
        """
        Calculate current market value of all open positions using FRESH API data.
        
        This ensures account status always reflects actual positions, not stale cache.
        
        Returns:
            Total current value of all positions
        """
        total_value = 0.0
        
        # CRITICAL: Fetch fresh API positions every time to avoid stale data
        api_positions = await self.api_client.get_positions()
        
        if not api_positions:
            # If API unavailable, fall back to local cache
            logger.debug("API positions unavailable, using local cache for position value")
            for pos in self.positions.values():
                market_slug = pos["market_slug"]
                outcome = pos["outcome"]
                shares = pos["shares"]
                
                current_price = await self.api_client.get_best_price(market_slug, "sell", outcome)
                if current_price and current_price > 0:
                    total_value += shares * current_price
                else:
                    total_value += shares * pos["entry_price"]
            return total_value
        
        # Use fresh API data for accurate calculation
        for api_pos in api_positions:
            try:
                shares = abs(float(api_pos.get("size") or 0))
                if shares < 0.01:
                    continue
                
                market_slug = api_pos.get("market") or api_pos.get("marketSlug") or api_pos.get("market_slug")
                outcome = str(api_pos.get("outcome") or "").strip().lower()
                
                if not market_slug or not outcome:
                    continue
                
                # Normalize market slug (remove aec- prefix if present)
                if isinstance(market_slug, str) and market_slug.startswith("aec-"):
                    market_slug = market_slug[4:]
                
                # Get current market price for this position
                current_price = await self.api_client.get_best_price(market_slug, "sell", outcome)
                if current_price and current_price > 0:
                    total_value += shares * current_price
                else:
                    # If can't get price, estimate from API-reported average price
                    raw_data = api_pos.get("raw", {})
                    if isinstance(raw_data, dict):
                        avg_px_data = raw_data.get("avgPx", {})
                        if isinstance(avg_px_data, dict):
                            avg_px = to_float(avg_px_data.get("value"), default=0.0)
                        else:
                            avg_px = to_float(avg_px_data, default=0.0)
                        
                        if avg_px > 0:
                            total_value += shares * avg_px
                        else:
                            # Last fallback: assume entry price of 0.50
                            total_value += shares * 0.50
            
            except Exception as e:
                logger.debug(f"Error calculating value for position: {e}")
                continue
        
        return total_value

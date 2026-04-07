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
from wash_sale_tracker import WashSaleTracker

logger = logging.getLogger(__name__)


class PositionManager:
    """Manages position tracking and sizing."""
    
    def __init__(self, api_client: PolymarketAPIClient, state_file: Optional[str] = None):
        """
        Initialize position manager.
        
        Args:
            api_client: API client instance
            state_file: Path for persisted position state file
        """
        self.api_client = api_client
        self.positions: dict[str, dict[str, Any]] = {}
        configured_state_file = str(state_file or "").strip()
        self.state_file = configured_state_file or "positions_state.json"
        self.balance: Optional[float] = None
        self.buying_power: Optional[float] = None
        self._recent_owner_cache: dict[str, dict[str, Any]] = {}
        self._sync_missing_counts: dict[str, int] = {}
        
        # Initialize wash sale tracker
        wash_sale_file = (
            "wash_sale_state.json" if not configured_state_file or configured_state_file == "positions_state.json"
            else f"wash_sale_{configured_state_file}"
        )
        self.wash_sale_tracker = WashSaleTracker(state_file=wash_sale_file) if Config.ENABLE_WASH_SALE_PREVENTION else None
        
        self._load_state()

    def _position_identity(self, market_slug: str, outcome: str) -> str:
        return self.get_position_key(market_slug, outcome)

    def _canonical_market_slug(self, market_slug: str) -> str:
        slug = str(market_slug or "").strip().lower()
        
        # First normalize slug value (strip aec- prefix, etc.)
        normalizer = getattr(self.api_client, "_normalize_slug_value", None)
        if callable(normalizer):
            slug = str(normalizer(slug) or "").strip().lower()
        else:
            slug = slug[4:] if slug.startswith("aec-") else slug
        
        # Apply team abbreviation mapping to handle variations like "sas" → "sa"
        abbrev_mapper = getattr(self.api_client, "_apply_team_abbreviation_map", None)
        if callable(abbrev_mapper):
            slug = abbrev_mapper(slug)
        
        return str(slug or "").strip().lower()

    @staticmethod
    def _normalize_trader_shares(raw: Any) -> dict[str, float]:
        if not isinstance(raw, dict):
            return {}

        normalized: dict[str, float] = {}
        for trader, shares in raw.items():
            owner = str(trader or "").strip().lower()
            if not owner:
                continue
            amount = max(0.0, to_float(shares, default=0.0))
            if amount <= 0:
                continue
            normalized[owner] = normalized.get(owner, 0.0) + amount

        return normalized

    @staticmethod
    def _merge_trader_shares(base: dict[str, float], extra: dict[str, float]) -> dict[str, float]:
        merged = dict(base)
        for owner, shares in extra.items():
            merged[owner] = merged.get(owner, 0.0) + max(0.0, to_float(shares, default=0.0))
        return {owner: amount for owner, amount in merged.items() if amount > 0}

    def _ensure_position_trader_shares(self, position: dict[str, Any]) -> dict[str, float]:
        trader_shares = self._normalize_trader_shares(position.get("trader_shares"))
        total_shares = max(0.0, to_float(position.get("shares"), default=0.0))
        owner = str(position.get("monitored_trader") or "").strip().lower()

        if not trader_shares and owner and total_shares > 0:
            trader_shares = {owner: total_shares}

        position["trader_shares"] = trader_shares
        return trader_shares

    @staticmethod
    def _pick_primary_owner(trader_shares: dict[str, float]) -> Optional[str]:
        positive = [(owner, shares) for owner, shares in trader_shares.items() if shares > 0]
        if len(positive) == 1:
            return positive[0][0]
        return None

    def remember_recent_owner(
        self,
        market_slug: str,
        outcome: str,
        monitored_trader: Optional[str],
        shares: float,
    ) -> None:
        owner = str(monitored_trader or "").strip().lower()
        if not owner:
            return
        key = self._position_identity(market_slug, outcome)
        self._recent_owner_cache[key] = {
            "owner": owner,
            "shares": max(0.0, to_float(shares, default=0.0)),
            "removed_at": datetime.now(timezone.utc),
        }

    def get_recent_owner_candidate(self, market_slug: str, outcome: str, ttl_seconds: int) -> Optional[str]:
        key = self._position_identity(market_slug, outcome)
        record = self._recent_owner_cache.get(key)
        if not record:
            return None

        removed_at = record.get("removed_at")
        if not isinstance(removed_at, datetime):
            self._recent_owner_cache.pop(key, None)
            return None

        ttl = max(1, int(ttl_seconds))
        age_seconds = (datetime.now(timezone.utc) - removed_at).total_seconds()
        if age_seconds > ttl:
            self._recent_owner_cache.pop(key, None)
            return None

        owner = str(record.get("owner") or "").strip().lower()
        return owner or None

    def set_position_monitored_trader(self, market_slug: str, outcome: str, monitored_trader: str) -> bool:
        key = self.get_position_key(market_slug, outcome)
        position = self.positions.get(key)
        owner = str(monitored_trader or "").strip().lower()
        if not position or not owner:
            return False

        shares = max(0.0, to_float(position.get("shares"), default=0.0))
        trader_shares = self._ensure_position_trader_shares(position)
        trader_added = False
        if owner not in trader_shares and shares > 0:
            trader_shares[owner] = shares
            trader_added = True

        existing_owner = str(position.get("monitored_trader") or "").strip().lower()
        if existing_owner == owner:
            if trader_added:
                position["trader_shares"] = trader_shares
                self.positions[key] = position
                self._save_state()
            return False

        position["monitored_trader"] = owner
        position["trader_shares"] = trader_shares
        self.positions[key] = position
        self._save_state()
        logger.info(
            f"Relinked position owner: {position.get('market_slug')} | {position.get('outcome')} | "
            "owner relinked"
        )
        return True

    @staticmethod
    def _deserialize_timestamp(value: Any) -> Optional[datetime]:
        if isinstance(value, datetime):
            return value
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            return None

    def _serialize_recent_owner_cache(self) -> dict[str, dict[str, Any]]:
        serialized: dict[str, dict[str, Any]] = {}
        for key, record in self._recent_owner_cache.items():
            if not isinstance(record, dict):
                continue
            owner = str(record.get("owner") or "").strip().lower()
            if not owner:
                continue
            removed_at = self._deserialize_timestamp(record.get("removed_at"))
            if removed_at is None:
                removed_at = datetime.now(timezone.utc)
            serialized[key] = {
                "owner": owner,
                "shares": max(0.0, to_float(record.get("shares"), default=0.0)),
                "removed_at": removed_at.isoformat(),
            }
        return serialized

    def _load_recent_owner_cache(self, raw_cache: Any) -> None:
        self._recent_owner_cache = {}
        if not isinstance(raw_cache, dict):
            return

        for key, record in raw_cache.items():
            if not isinstance(record, dict):
                continue
            owner = str(record.get("owner") or "").strip().lower()
            removed_at = self._deserialize_timestamp(record.get("removed_at"))
            if not owner or removed_at is None:
                continue
            self._recent_owner_cache[str(key)] = {
                "owner": owner,
                "shares": max(0.0, to_float(record.get("shares"), default=0.0)),
                "removed_at": removed_at,
            }
    
    def _load_state(self):
        """Load positions from state file."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r") as f:
                    data = json.load(f)
                    loaded_positions = data.get("positions", {})
                    canonical_positions: dict[str, dict[str, Any]] = {}
                    migrated = 0

                    if isinstance(loaded_positions, dict):
                        for _, pos in loaded_positions.items():
                            if not isinstance(pos, dict):
                                continue

                            raw_slug = str(pos.get("market_slug") or "").strip().lower()
                            raw_outcome = str(pos.get("outcome") or "").strip().lower()
                            if not raw_slug or not raw_outcome:
                                continue

                            canonical_slug = self._canonical_market_slug(raw_slug)
                            canonical_key = f"{canonical_slug}|{raw_outcome}"

                            normalized_pos = dict(pos)
                            normalized_pos["market_slug"] = canonical_slug
                            normalized_pos["outcome"] = raw_outcome

                            if canonical_key in canonical_positions:
                                # Merge duplicates introduced by canonicalization.
                                existing = canonical_positions[canonical_key]
                                existing_shares = to_float(existing.get("shares"), default=0.0)
                                incoming_shares = to_float(normalized_pos.get("shares"), default=0.0)
                                existing_invested = to_float(existing.get("invested"), default=0.0)
                                incoming_invested = to_float(normalized_pos.get("invested"), default=0.0)

                                merged_shares = existing_shares + incoming_shares
                                merged_invested = existing_invested + incoming_invested
                                existing["shares"] = merged_shares
                                existing["invested"] = merged_invested
                                if merged_shares > 0:
                                    existing["entry_price"] = merged_invested / merged_shares

                                existing_owner = str(existing.get("monitored_trader") or "").strip()
                                incoming_owner = str(normalized_pos.get("monitored_trader") or "").strip()
                                if not existing_owner and incoming_owner:
                                    existing["monitored_trader"] = incoming_owner.lower() or None

                                existing_trader_shares = self._normalize_trader_shares(existing.get("trader_shares"))
                                incoming_trader_shares = self._normalize_trader_shares(normalized_pos.get("trader_shares"))
                                if not incoming_trader_shares and incoming_owner and incoming_shares > 0:
                                    incoming_trader_shares = {incoming_owner.lower(): incoming_shares}
                                existing["trader_shares"] = self._merge_trader_shares(
                                    existing_trader_shares,
                                    incoming_trader_shares,
                                )

                                canonical_positions[canonical_key] = existing
                                migrated += 1
                            else:
                                canonical_positions[canonical_key] = normalized_pos

                            original_key = str(pos.get("market_slug") or "").strip().lower() + "|" + raw_outcome
                            if original_key != canonical_key or raw_slug != canonical_slug:
                                migrated += 1

                    self.positions = canonical_positions
                    for pos in self.positions.values():
                        if not isinstance(pos, dict):
                            continue
                        owner = str(pos.get("monitored_trader") or "").strip().lower()
                        pos["monitored_trader"] = owner or None
                        trader_shares = self._ensure_position_trader_shares(pos)
                        primary_owner = self._pick_primary_owner(trader_shares)
                        pos["monitored_trader"] = primary_owner or (owner or None)

                    self.balance = data.get("balance")
                    self.buying_power = data.get("buying_power")
                    self._load_recent_owner_cache(data.get("recent_owner_cache", {}))

                    raw_sync_missing_counts = data.get("sync_missing_counts", {})
                    self._sync_missing_counts = {}
                    if isinstance(raw_sync_missing_counts, dict):
                        for key, value in raw_sync_missing_counts.items():
                            misses = int(to_float(value, default=0.0))
                            if misses > 0:
                                self._sync_missing_counts[str(key)] = misses

                    logger.info(f"Loaded {len(self.positions)} positions from state file")
                    if migrated > 0:
                        logger.info(f"Migrated {migrated} position entries to canonical slug keys")
                        self._save_state()
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
                "recent_owner_cache": self._serialize_recent_owner_cache(),
                "sync_missing_counts": self._sync_missing_counts,
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
        normalized_slug = self._canonical_market_slug(market_slug)
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
            alias_owner = str(alias_pos.get("monitored_trader") or "").strip().lower()
            canonical_pos["monitored_trader"] = alias_owner or None

        canonical_trader_shares = self._ensure_position_trader_shares(canonical_pos)
        alias_trader_shares = self._ensure_position_trader_shares(alias_pos)
        merged_trader_shares = self._merge_trader_shares(canonical_trader_shares, alias_trader_shares)
        canonical_pos["trader_shares"] = merged_trader_shares
        primary_owner = self._pick_primary_owner(merged_trader_shares)
        current_owner = str(canonical_pos.get("monitored_trader") or "").strip().lower() or None
        canonical_pos["monitored_trader"] = primary_owner or current_owner

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
        # Normalize both slugs for comparison to handle abbreviation variations (e.g., "sa" vs "sas")
        normalized_market = self._canonical_market_slug(market_slug)
        for key, pos in self.positions.items():
            pos_market = self._canonical_market_slug(pos["market_slug"])
            if pos_market == normalized_market:
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
        owner = str(monitored_trader or "").strip().lower()

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
            trader_shares = self._ensure_position_trader_shares(existing)
            if owner:
                existing["monitored_trader"] = owner
                trader_shares[owner] = trader_shares.get(owner, 0.0) + shares

            existing["trader_shares"] = trader_shares
            primary_owner = self._pick_primary_owner(trader_shares)
            if primary_owner:
                existing["monitored_trader"] = primary_owner

            self.positions[key] = existing
            self._save_state()

            logger.info(
                f"Added to position: {market_slug} | {outcome} | "
                f"+{shares:.2f} shares @ ${price:.4f} (${new_invested:.2f}) | "
                f"Total={total_shares:.2f} shares, Avg=${avg_entry:.4f}, Invested=${total_invested:.2f}"
            )
            return

        position = {
            "market_slug": self._canonical_market_slug(market_slug),
            "outcome": outcome.lower(),
            "shares": shares,
            "entry_price": price,
            "invested": new_invested,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "monitored_trader": owner or None,
            "trader_shares": {owner: shares} if owner and shares > 0 else {},
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
        
        position = self.positions[key]
        
        # Cache owner information before removal for potential recovery
        monitored_trader = position.get("monitored_trader")
        shares = position.get("shares", 0.0)
        self.remember_recent_owner(market_slug, outcome, monitored_trader, shares)
        
        # Now remove the position
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
        
        # Record wash sale if this was a loss
        if Config.ENABLE_WASH_SALE_PREVENTION and self.wash_sale_tracker and pnl < 0:
            self.wash_sale_tracker.record_loss_sale(
                market_slug=market_slug,
                outcome=outcome,
                realized_pnl=pnl,
                exit_price=exit_price,
            )
        
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
        trader_wallet: Optional[str] = None,
    ):
        """
        Update position share count (for partial closes).
        
        Args:
            market_slug: Market slug
            outcome: Outcome
            new_shares: New share count
            new_invested: New invested amount (optional, will be calculated if not provided)
            trader_wallet: Trader attribution to reduce first (optional)
        """
        key = self.get_position_key(market_slug, outcome)
        
        if key not in self.positions:
            logger.warning(f"Cannot update position: not found ({key})")
            return
        
        position = self.positions[key]
        old_shares = position["shares"]
        old_shares_value = max(0.0, to_float(old_shares, default=0.0))
        target_shares = max(0.0, to_float(new_shares, default=0.0))
        reduction = max(0.0, old_shares_value - target_shares)
        
        # Update shares
        position["shares"] = target_shares
        
        # Update invested amount
        if new_invested is not None:
            position["invested"] = new_invested
        else:
            # Proportional reduction
            if old_shares_value > 0:
                position["invested"] = position["invested"] * (target_shares / old_shares_value)

        trader_shares = self._ensure_position_trader_shares(position)
        owner = str(trader_wallet or "").strip().lower()
        if reduction > 0 and owner and owner in trader_shares:
            trader_shares[owner] = max(0.0, trader_shares.get(owner, 0.0) - reduction)

        total_attributed = sum(max(0.0, v) for v in trader_shares.values())
        if total_attributed > 0 and target_shares >= 0:
            if total_attributed > target_shares + 1e-9:
                scale = target_shares / total_attributed if total_attributed > 0 else 0.0
                for k in list(trader_shares.keys()):
                    trader_shares[k] = max(0.0, trader_shares[k] * scale)
            elif not owner:
                # API-led share updates without a specific trader should preserve ratios.
                scale = target_shares / total_attributed if total_attributed > 0 else 0.0
                for k in list(trader_shares.keys()):
                    trader_shares[k] = max(0.0, trader_shares[k] * scale)

        trader_shares = {k: v for k, v in trader_shares.items() if v > 1e-9}
        position["trader_shares"] = trader_shares
        primary_owner = self._pick_primary_owner(trader_shares)
        position["monitored_trader"] = primary_owner

        attributed_after = sum(max(0.0, v) for v in trader_shares.values())
        if attributed_after > target_shares + 1e-6:
            logger.warning(
                f"Attribution invariant warning: {market_slug} | {outcome} | "
                f"attributed={attributed_after:.6f} exceeds shares={target_shares:.6f}"
            )
        
        self._save_state()
        
        logger.info(
            f"Updated position: {market_slug} | {outcome} | "
            f"{old_shares_value:.2f} -> {target_shares:.2f} shares"
        )

    def get_trader_attributed_shares(self, market_slug: str, outcome: str, trader_wallet: str) -> float:
        position = self.get_position(market_slug, outcome)
        if not position:
            return 0.0

        owner = str(trader_wallet or "").strip().lower()
        if not owner:
            return 0.0

        trader_shares = self._ensure_position_trader_shares(position)
        return max(0.0, to_float(trader_shares.get(owner), default=0.0))
    
    def is_buy_blocked_by_wash_sale(self, market_slug: str, outcome: str) -> tuple[bool, Optional[str]]:
        """
        Check if buy is blocked by wash sale rule.
        
        Args:
            market_slug: Market identifier
            outcome: Position outcome (yes/no)
            
        Returns:
            Tuple of (is_blocked, reason_if_blocked)
        """
        if not Config.ENABLE_WASH_SALE_PREVENTION or not self.wash_sale_tracker:
            return (False, None)
        
        is_blocked = self.wash_sale_tracker.is_blocked(market_slug, outcome)
        if not is_blocked:
            return (False, None)
        
        reason = self.wash_sale_tracker.get_blocked_reason(market_slug, outcome)
        return (True, reason)
    
    async def sync_positions_with_api(self) -> list[dict[str, Any]]:
        """
        Synchronize local positions with API positions.
        
        Handles:
        - Positions closed externally
        - Position size mismatches
        
        Returns:
            List of positions requiring emergency sell (detected as externally closed)
        """
        emergency_sell_candidates: list[dict[str, Any]] = []
        
        api_positions = await self.api_client.get_positions()
        
        if api_positions is None:
            logger.warning("Cannot sync positions: API returned None")
            return emergency_sell_candidates
        
        # Build lookup from API positions
        # Map API positions by their NORMALIZED keys only for importing
        # We'll handle legacy key matching separately during the update phase
        api_lookup: dict[str, dict[str, Any]] = {}
        api_positions_by_market: dict[str, dict[str, Any]] = {}  # For matching: "market|outcome" -> position
        api_positions_by_market_only: dict[str, list[dict[str, Any]]] = {}
        
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
                normalized_outcome = await self.api_client.normalize_outcome_to_yes_no(
                    market_slug,
                    raw_outcome,
                    caller_context="position_sync",
                )
            
            # Use normalized outcome if available, otherwise use raw lowercase
            primary_outcome = normalized_outcome if normalized_outcome else outcome_lower
            
            # Normalize market slug (remove aec- prefix)
            normalized_market = self._canonical_market_slug(market_slug)
            
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

            api_positions_by_market_only.setdefault(normalized_market, []).append(position_data)
        
        # Track which API positions have been matched to local positions
        # to prevent duplicate imports
        matched_api_positions = set()
        
        # Find positions closed externally (in local state but not in API)
        # Check by both exact key match and flexible market+outcome matching
        to_remove = []
        for key, local_pos in self.positions.items():
            local_market = local_pos['market_slug']
            local_outcome = local_pos['outcome']
            miss_identity = self._position_identity(local_market, local_outcome)
            
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

            # Strategy 4: Conservative market-only fallback.
            # If there is exactly one API position for the market, treat it as a match
            # even when outcome normalization failed, to avoid false local closure.
            if not found_in_api:
                market_only = api_positions_by_market_only.get(local_market) or []
                if len(market_only) == 1:
                    found_in_api = True
                    api_data = market_only[0]
                    matched_key = self.get_position_key(api_data["market_slug"], api_data["outcome"])
                    matched_api_positions.add(matched_key)
            
            if not found_in_api:
                miss_threshold = max(1, Config.POSITION_SYNC_MISS_THRESHOLD)
                local_outcome_lower = str(local_outcome or "").strip().lower()

                # For non-binary local outcomes (team labels) while Gamma metadata is
                # unavailable, use a wider miss threshold to avoid close/re-import churn
                # from transient API omission or unresolved normalization.
                if local_outcome_lower not in ("yes", "no"):
                    metadata_available = await self.api_client.get_market_info(local_market)
                    if not metadata_available:
                        miss_threshold = max(miss_threshold, 12)

                misses = int(self._sync_missing_counts.get(miss_identity, 0)) + 1
                self._sync_missing_counts[miss_identity] = misses
                if misses < miss_threshold:
                    logger.warning(
                        f"Deferring external close after fallback match miss: {local_market} | {local_outcome} | "
                        f"misses={misses}/{miss_threshold}"
                    )
                    continue

                self.remember_recent_owner(
                    local_market,
                    local_outcome,
                    local_pos.get("monitored_trader"),
                    to_float(local_pos.get("shares"), default=0.0),
                )
                logger.warning(
                    f"Position closed externally after repeated fallback match misses: {local_market} | {local_outcome} | "
                    f"misses={misses}"
                )
                
                # Add to emergency sell candidates if feature is enabled
                if Config.EMERGENCY_SELL_ON_EXTERNAL_CLOSE_ENABLED:
                    emergency_sell_candidates.append({
                        "market_slug": local_market,
                        "outcome": local_outcome,
                        "shares": to_float(local_pos.get("shares"), default=0.0),
                        "monitored_trader": local_pos.get("monitored_trader"),
                        "reason": "external_close_detected",
                    })
                
                to_remove.append(key)
            else:
                self._sync_missing_counts.pop(miss_identity, None)
        
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

            # Strategy 4: Conservative market-only fallback.
            if not api_data:
                market_only = api_positions_by_market_only.get(local_market) or []
                if len(market_only) == 1:
                    api_data = market_only[0]
                    matched_api_key = self.get_position_key(api_data["market_slug"], api_data["outcome"])
                    matched_api_positions.add(matched_api_key)
            
            if api_data:
                self._sync_missing_counts.pop(self._position_identity(local_market, local_outcome), None)
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

            recovered_owner = self.get_recent_owner_candidate(
                api_pos["market_slug"],
                api_pos["outcome"],
                Config.POSITION_OWNER_RECOVERY_TTL_SECONDS,
            )

            self.positions[key] = {
                "market_slug": api_pos["market_slug"],
                "outcome": api_pos["outcome"],
                "shares": shares,
                "entry_price": entry_price,
                "invested": invested,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "monitored_trader": recovered_owner,
                "trader_shares": {recovered_owner: shares} if recovered_owner else {},
            }
            imported += 1
            logger.debug(f"Imported new API position: {key}")
            if recovered_owner:
                logger.info(
                    f"Recovered owner for re-imported position: {api_pos['market_slug']} | "
                    f"{api_pos['outcome']} | owner recovered"
                )
            else:
                logger.warning(
                    f"Imported API position without owner link: {api_pos['market_slug']} | {api_pos['outcome']}"
                )
        
        # Remove any positions with zero shares
        for key in to_remove:
            pos = self.positions.pop(key, None)
            if isinstance(pos, dict):
                self._sync_missing_counts.pop(
                    self._position_identity(pos.get("market_slug"), pos.get("outcome")),
                    None,
                )
        
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
            pos = self.positions.pop(key, None)
            if isinstance(pos, dict):
                self._sync_missing_counts.pop(
                    self._position_identity(pos.get("market_slug"), pos.get("outcome")),
                    None,
                )
        
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
        
        # Phase 3: Check for over-cap positions after sync
        if self.balance and self.balance > 0:
            market_cap = self.balance * Config.MAX_POSITION_SIZE_PER_MARKET
            over_cap_positions = []
            
            for key, pos in self.positions.items():
                market_slug = pos.get("market_slug")
                if market_slug:
                    market_exposure = self.get_market_exposure(market_slug)
                    if market_exposure > market_cap:
                        over_cap_positions.append({
                            "market_slug": market_slug,
                            "exposure": market_exposure,
                            "cap": market_cap,
                            "excess": market_exposure - market_cap,
                        })
            
            # Deduplicate by market_slug
            seen_markets = set()
            unique_over_cap = []
            for pos_info in over_cap_positions:
                if pos_info["market_slug"] not in seen_markets:
                    seen_markets.add(pos_info["market_slug"])
                    unique_over_cap.append(pos_info)
            
            if unique_over_cap:
                for pos_info in unique_over_cap:
                    logger.debug(
                        f"Position exceeds market cap (post-sync state): {pos_info['market_slug']} | "
                        f"Exposure=${pos_info['exposure']:.2f} | Cap=${pos_info['cap']:.2f} | "
                        f"Over by ${pos_info['excess']:.2f} ({pos_info['excess']/pos_info['cap']*100:.1f}%)"
                    )
        
        return emergency_sell_candidates
    
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
                if isinstance(market_slug, str):
                    market_slug = self._canonical_market_slug(market_slug)
                
                raw_data = api_pos.get("raw", {})
                raw_data = raw_data if isinstance(raw_data, dict) else {}

                # Prefer API-provided mark-like fields to avoid repeated metadata lookups.
                cur_price = to_float(api_pos.get("curPrice"), default=0.0)
                if cur_price <= 0:
                    cur_price = to_float(raw_data.get("curPrice"), default=0.0)

                if cur_price > 0:
                    total_value += shares * cur_price
                    continue

                # Fallback to average fill price from API payload.
                avg_px_data = raw_data.get("avgPx", {})
                if isinstance(avg_px_data, dict):
                    avg_px = to_float(avg_px_data.get("value"), default=0.0)
                else:
                    avg_px = to_float(avg_px_data, default=0.0)

                if avg_px <= 0:
                    avg_px = to_float(api_pos.get("avgPrice"), default=0.0)

                if avg_px > 0:
                    total_value += shares * avg_px
                else:
                    # Last fallback: neutral midpoint valuation for unresolved marks.
                    total_value += shares * 0.50
            
            except Exception as e:
                logger.debug(f"Error calculating value for position: {e}")
                continue
        
        return total_value

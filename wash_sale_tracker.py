"""
Wash sale tracking and prevention.

Tracks positions sold at a loss to prevent repurchasing within 30 days,
complying with traditional wash sale rules to avoid tax complications and
enforce disciplined trading.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from config import Config

logger = logging.getLogger(__name__)


class WashSaleTracker:
    """Tracks realized losses and blocks rebuys within wash sale period."""
    
    def __init__(self, state_file: Optional[str] = None):
        """
        Initialize wash sale tracker.
        
        Args:
            state_file: Path for persisted wash sale state file
        """
        self.state_file = state_file or "wash_sale_state.json"
        self.wash_sales: dict[str, dict[str, Any]] = {}
        self._load_state()
        self.cleanup_expired()
    
    def _get_position_key(self, market_slug: str, outcome: str) -> str:
        """Generate consistent position key."""
        slug = str(market_slug or "").strip().lower()
        out = str(outcome or "").strip().lower()
        return f"{slug}|{out}"
    
    def _load_state(self) -> None:
        """Load wash sale state from disk."""
        if not os.path.exists(self.state_file):
            logger.debug(f"No existing wash sale state file: {self.state_file}")
            self.wash_sales = {}
            return
        
        try:
            with open(self.state_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.wash_sales = data.get("wash_sales", {})
                logger.info(f"Loaded {len(self.wash_sales)} wash sale entries from {self.state_file}")
        except Exception as e:
            logger.exception(f"Error loading wash sale state: {e}")
            self.wash_sales = {}
    
    def _save_state(self) -> None:
        """Save wash sale state to disk."""
        try:
            data = {
                "wash_sales": self.wash_sales,
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
            logger.exception(f"Error saving wash sale state: {e}")
    
    def record_loss_sale(
        self,
        market_slug: str,
        outcome: str,
        realized_pnl: float,
        exit_price: float,
    ) -> None:
        """
        Record a position sold at a loss.
        
        Args:
            market_slug: Market identifier
            outcome: Position outcome (yes/no)
            realized_pnl: Realized profit/loss (negative = loss)
            exit_price: Exit price of the sale
        """
        if realized_pnl >= 0:
            logger.debug(
                f"Not recording wash sale for {market_slug} | {outcome}: "
                f"not a loss (pnl=${realized_pnl:.2f})"
            )
            return
        
        position_key = self._get_position_key(market_slug, outcome)
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(days=Config.WASH_SALE_DAYS)
        
        self.wash_sales[position_key] = {
            "market_slug": market_slug,
            "outcome": outcome,
            "realized_loss": realized_pnl,
            "exit_price": exit_price,
            "closed_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        
        self._save_state()
        
        logger.warning(
            f"WASH SALE recorded: {market_slug} | {outcome} | "
            f"loss=${realized_pnl:.2f} | exit_price=${exit_price:.2f} | "
            f"blocked until {expires_at.strftime('%Y-%m-%d %H:%M UTC')}"
        )
    
    def is_blocked(self, market_slug: str, outcome: str) -> bool:
        """
        Check if position is blocked by wash sale rule.
        
        Args:
            market_slug: Market identifier
            outcome: Position outcome (yes/no)
            
        Returns:
            True if buy is blocked, False otherwise
        """
        position_key = self._get_position_key(market_slug, outcome)
        
        if position_key not in self.wash_sales:
            return False
        
        entry = self.wash_sales[position_key]
        expires_at_str = entry.get("expires_at")
        
        if not expires_at_str:
            return False
        
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            now = datetime.now(timezone.utc)
            
            # Ensure timezone-aware comparison
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            
            return now < expires_at
        except Exception as e:
            logger.error(f"Error checking wash sale expiration for {position_key}: {e}")
            return False
    
    def get_blocked_reason(self, market_slug: str, outcome: str) -> Optional[str]:
        """
        Get human-readable reason why position is blocked.
        
        Args:
            market_slug: Market identifier
            outcome: Position outcome (yes/no)
            
        Returns:
            Reason string if blocked, None otherwise
        """
        if not self.is_blocked(market_slug, outcome):
            return None
        
        position_key = self._get_position_key(market_slug, outcome)
        entry = self.wash_sales.get(position_key, {})
        
        realized_loss = entry.get("realized_loss", 0.0)
        expires_at_str = entry.get("expires_at", "")
        
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            
            return (
                f"Wash sale block until {expires_at.strftime('%Y-%m-%d %H:%M UTC')} "
                f"(loss=${realized_loss:.2f})"
            )
        except Exception:
            return f"Wash sale block active (loss=${realized_loss:.2f})"
    
    def cleanup_expired(self) -> None:
        """Remove expired wash sale entries."""
        now = datetime.now(timezone.utc)
        expired_keys = []
        
        for position_key, entry in self.wash_sales.items():
            expires_at_str = entry.get("expires_at")
            if not expires_at_str:
                expired_keys.append(position_key)
                continue
            
            try:
                expires_at = datetime.fromisoformat(expires_at_str)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                
                if now >= expires_at:
                    expired_keys.append(position_key)
            except Exception as e:
                logger.error(f"Error parsing expiration for {position_key}: {e}")
                expired_keys.append(position_key)
        
        if expired_keys:
            for key in expired_keys:
                del self.wash_sales[key]
            
            self._save_state()
            logger.info(f"Cleaned up {len(expired_keys)} expired wash sale entries")
    
    def get_all_blocks(self) -> list[dict[str, Any]]:
        """
        Get all active wash sale blocks.
        
        Returns:
            List of active wash sale entries
        """
        self.cleanup_expired()
        return [
            {
                "position_key": key,
                **entry
            }
            for key, entry in self.wash_sales.items()
            if self.is_blocked(entry["market_slug"], entry["outcome"])
        ]

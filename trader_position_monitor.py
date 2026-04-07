"""
Monitor trader positions to detect exits via GTC limit orders.

This module tracks positions held by monitored traders and detects when
they close positions that may not appear in the trade activity feeds
(e.g., GTC limit orders that fill later).
"""
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from api_client import PolymarketAPIClient
from config import Config
from utils import to_float

logger = logging.getLogger(__name__)


class TraderPositionMonitor:
    """Monitors trader positions to detect exits not visible in trade feeds."""
    
    def __init__(self, api_client: PolymarketAPIClient):
        """Initialize trader position monitor."""
        self.api_client = api_client
        
        # Track last known positions: wallet -> {market|outcome: shares}
        self.trader_positions: dict[str, dict[str, float]] = defaultdict(dict)
        
        # Track last poll time per wallet
        self.last_poll_time: dict[str, datetime] = {}
        
        # Wallet display labels
        self.wallet_labels: dict[str, str] = {}
    
    def set_wallet_label(self, wallet: str, label: str) -> None:
        """Set display label for a wallet."""
        wallet_key = str(wallet or "").strip().lower()
        display = str(label or "").strip()
        if wallet_key and display:
            self.wallet_labels[wallet_key] = display
    
    def _wallet_label(self, wallet: str) -> str:
        """Get display label for wallet."""
        wallet_key = str(wallet or "").strip().lower()
        return self.wallet_labels.get(wallet_key) or f"{wallet_key[:8]}..."
    
    @staticmethod
    def _normalize_market_slug(slug: str) -> str:
        """Normalize market slug for comparison."""
        normalized = str(slug or "").strip().lower()
        if normalized.startswith("aec-"):
            normalized = normalized[4:]
        return normalized
    
    async def poll_trader_positions(
        self,
        wallet: str,
    ) -> list[dict[str, Any]]:
        """
        Poll trader positions and detect changes.
        
        Args:
            wallet: Trader wallet address
            
        Returns:
            List of detected exit events (each dict contains market_slug, outcome, previous_shares)
        """
        wallet_key = str(wallet or "").strip().lower()
        if not wallet_key:
            return []
        
        detected_exits: list[dict[str, Any]] = []
        
        try:
            # Get current positions from Data API
            positions = await self.api_client.get_user_positions(
                wallet=wallet,
                limit=Config.TRADE_PAGE_SIZE,
            )
            
            if positions is None:
                # API unavailable - skip this cycle
                logger.debug(f"Positions API unavailable for {self._wallet_label(wallet)}")
                return []
            
            # Build current position snapshot
            current_snapshot: dict[str, float] = {}
            for pos in positions:
                if not isinstance(pos, dict):
                    continue
                
                market_slug = self._normalize_market_slug(
                    str(pos.get("slug") or pos.get("marketSlug") or pos.get("market_slug") or "")
                )
                outcome = str(pos.get("outcome") or "").strip().lower()
                shares = to_float(pos.get("size") or pos.get("shares"), default=0.0)
                
                if not market_slug or not outcome or shares <= 0:
                    continue
                
                position_key = f"{market_slug}|{outcome}"
                current_snapshot[position_key] = shares
            
            # Compare with previous snapshot
            previous_snapshot = self.trader_positions.get(wallet_key, {})
            
            if previous_snapshot:
                # Detect positions that closed or reduced significantly
                for position_key, prev_shares in previous_snapshot.items():
                    current_shares = current_snapshot.get(position_key, 0.0)
                    
                    # Detect significant reduction (>80% of position or full closure)
                    reduction_ratio = (prev_shares - current_shares) / prev_shares if prev_shares > 0 else 0.0
                    
                    # Only trigger on substantial exits (>80% reduction or full closure)
                    if reduction_ratio >= Config.TRADER_POSITION_EXIT_THRESHOLD:
                        market_slug, outcome = position_key.split("|", 1)
                        
                        logger.debug(
                            f"Trader exit detected: {self._wallet_label(wallet)} | "
                            f"{market_slug} | {outcome} | "
                            f"shares: {prev_shares:.2f} -> {current_shares:.2f} "
                            f"({reduction_ratio*100:.1f}% reduction)"
                        )
                        
                        detected_exits.append({
                            "market_slug": market_slug,
                            "outcome": outcome,
                            "previous_shares": prev_shares,
                            "current_shares": current_shares,
                            "reduction_ratio": reduction_ratio,
                            "trader_wallet": wallet,
                            "trader_label": self._wallet_label(wallet),
                        })
            
            # Update snapshot
            self.trader_positions[wallet_key] = current_snapshot
            self.last_poll_time[wallet_key] = datetime.now(timezone.utc)
            
            return detected_exits
            
        except Exception as e:
            logger.exception(f"Error polling trader positions for {self._wallet_label(wallet)}: {e}")
            return []
    
    def initialize_wallet(self, wallet: str) -> None:
        """Initialize wallet tracking (clears existing state)."""
        wallet_key = str(wallet or "").strip().lower()
        if wallet_key:
            self.trader_positions[wallet_key] = {}
            logger.debug(f"Initialized position tracking for {self._wallet_label(wallet)}")

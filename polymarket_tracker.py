import aiohttp
import asyncio
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class PolymarketTracker:
    """Tracks Polymarket account activity via Data API and WebSocket."""

    def __init__(self, account_address: str, poll_interval: int = 5):
        """
        Initialize tracker.
        
        Args:
            account_address: Ethereum wallet address to track (0x-prefixed)
            poll_interval: Seconds between Data API polls
        """
        self.account_address = account_address
        self.poll_interval = poll_interval
        self.data_api_base = "https://data-api.polymarket.com"
        self.clob_ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/"
        
        self.session: Optional[aiohttp.ClientSession] = None
        self.last_trades: Dict[str, Any] = {}
        self.activity_callbacks: List[Callable] = []
        self.running = False

    async def initialize(self):
        """Create HTTP session."""
        self.session = aiohttp.ClientSession()

    async def shutdown(self):
        """Close HTTP session."""
        if self.session:
            await self.session.close()

    async def get_user_positions(self) -> List[Dict[str, Any]]:
        """
        Fetch current user positions from Data API.
        
        Returns:
            List of position objects
        """
        if not self.session:
            await self.initialize()

        try:
            url = f"{self.data_api_base}/positions"
            params = {
                "user": self.account_address,
                "limit": 500,
                "sortBy": "TOKENS",
                "sortDirection": "DESC"
            }
            
            async with self.session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Fetched {len(data)} positions for {self.account_address}")
                    return data
                else:
                    logger.error(f"Error fetching positions: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"Exception fetching positions: {e}")
            return []

    async def get_user_activity(self) -> List[Dict[str, Any]]:
        """
        Fetch user activity from Data API.
        
        Returns:
            List of activity events
        """
        if not self.session:
            await self.initialize()

        try:
            url = f"{self.data_api_base}/activity"
            params = {
                "user": self.account_address,
                "limit": 100
            }
            
            async with self.session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Fetched activity for {self.account_address}")
                    return data
                else:
                    logger.error(f"Error fetching activity: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"Exception fetching activity: {e}")
            return []

    async def get_user_trades(self) -> List[Dict[str, Any]]:
        """
        Fetch user trade history from Data API.
        
        Returns:
            List of trade objects
        """
        if not self.session:
            await self.initialize()

        try:
            url = f"{self.data_api_base}/trades"
            params = {
                "user": self.account_address,
                "limit": 100
            }
            
            async with self.session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info(f"Fetched {len(data)} trades for {self.account_address}")
                    return data
                else:
                    logger.error(f"Error fetching trades: {resp.status}")
                    return []
        except Exception as e:
            logger.error(f"Exception fetching trades: {e}")
            return []

    def register_activity_callback(self, callback: Callable):
        """
        Register callback for activity events.
        
        Args:
            callback: Async function to call with activity data
        """
        self.activity_callbacks.append(callback)

    async def _detect_new_trades(self, current_trades: List[Dict]) -> List[Dict]:
        """
        Compare current trades with last known trades to detect new ones.
        
        Args:
            current_trades: Current trades from API
            
        Returns:
            List of new trades
        """
        current_ids = {self._trade_key(trade): trade for trade in current_trades}
        new_trades = []
        
        for trade_id, trade in current_ids.items():
            if trade_id and trade_id not in self.last_trades:
                new_trades.append(trade)
        
        self.last_trades = current_ids
        return new_trades

    @staticmethod
    def _trade_key(trade: Dict[str, Any]) -> str:
        """Build a stable identifier for a trade event."""
        trade_id = trade.get("id")
        if trade_id is not None:
            return str(trade_id)

        tx_hash = trade.get("transactionHash") or ""
        timestamp = trade.get("timestamp") or ""
        asset = trade.get("asset") or ""
        outcome = trade.get("outcome") or ""
        side = trade.get("side") or ""
        size = trade.get("size") or ""
        price = trade.get("price") or ""
        return f"{tx_hash}:{timestamp}:{asset}:{outcome}:{side}:{size}:{price}"

    async def _notify_callbacks(self, activity_data: Dict[str, Any]):
        """
        Notify all registered callbacks of activity.
        
        Args:
            activity_data: Activity information to pass to callbacks
        """
        for callback in self.activity_callbacks:
            try:
                await callback(activity_data)
            except Exception as e:
                logger.error(f"Error in callback: {e}")

    async def start_polling(self):
        """
        Start polling Data API for new trades (fallback to WebSocket).
        Runs indefinitely until stopped.
        """
        await self.initialize()
        self.running = True
        
        logger.info(f"Starting poll tracker for account: {self.account_address}")

        # Seed the baseline so restarts don't tweet historical trades
        try:
            existing_trades = await self.get_user_trades()
            seeded_trades: Dict[str, Any] = {}
            for trade in existing_trades:
                trade_key = self._trade_key(trade)
                if trade_key:
                    seeded_trades[trade_key] = trade
            self.last_trades = seeded_trades
            logger.info(f"Seeded {len(self.last_trades)} existing trades; monitoring only new trades")
        except Exception as e:
            logger.error(f"Failed to seed existing trades at startup: {e}")
        
        while self.running:
            try:
                trades = await self.get_user_trades()
                new_trades = await self._detect_new_trades(trades)
                
                for trade in new_trades:
                    logger.info(f"New trade detected: {trade}")
                    activity_data = {
                        "type": "trade",
                        "timestamp": datetime.now().isoformat(),
                        "trade": trade
                    }
                    await self._notify_callbacks(activity_data)
                
                await asyncio.sleep(self.poll_interval)
                
            except asyncio.CancelledError:
                logger.info("Polling cancelled")
                break
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
                await asyncio.sleep(self.poll_interval)

    async def stop(self):
        """Stop monitoring."""
        self.running = False
        await self.shutdown()

    def format_trade_for_tweet(self, trade: Dict[str, Any], template: str = "default") -> str:
        """
        Format trade data for Twitter post.
        
        Args:
            trade: Trade object from API
            template: Tweet template style
            
        Returns:
            Formatted tweet text
        """
        if template == "minimal":
            return self._format_minimal(trade)
        elif template == "detailed":
            return self._format_detailed(trade)
        else:
            return self._format_default(trade)

    def _format_default(self, trade: Dict[str, Any]) -> str:
        """Default tweet format."""
        title = trade.get("title", "Unknown Market")
        outcome = trade.get("outcome", "Unknown")
        size = trade.get("size", 0)
        price = trade.get("price", 0)
        screen_name = (
            trade.get("name")
            or trade.get("pseudonym")
            or self._short_wallet(trade.get("proxyWallet", ""))
            or "Unknown Trader"
        )
        
        return (
            f"📊 Just traded on Polymarket!\n\n"
            f"Trader: {screen_name}\n"
            f"{title}\n"
            f"{outcome}\n"
            f"Size: {size} @ ${price:.2f}\n\n"
            f"#Polymarket #Prediction"
        )

    @staticmethod
    def _short_wallet(wallet: str) -> str:
        """Return shortened wallet address for display."""
        if not wallet or len(wallet) < 10:
            return wallet
        return f"{wallet[:6]}...{wallet[-4:]}"

    def _format_detailed(self, trade: Dict[str, Any]) -> str:
        """Detailed tweet format."""
        title = trade.get("title", "Unknown Market")
        outcome = trade.get("outcome", "Unknown")
        size = trade.get("size", 0)
        price = trade.get("price", 0)
        side = trade.get("side", "UNKNOWN")
        
        return f"🚀 Polymarket Trade!\n\n{title}\n📌 {outcome}\n🔀 {side}\n💰 {size} shares @ ${price:.2f}\n\n#Trading #Crypto"

    def _format_minimal(self, trade: Dict[str, Any]) -> str:
        """Minimal tweet format."""
        outcome = trade.get("outcome", "Unknown")
        size = trade.get("size", 0)
        price = trade.get("price", 0)
        
        return f"{outcome}: {size} @ ${price:.2f} 📈 #Polymarket"

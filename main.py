import asyncio
import logging
import os
from dotenv import load_dotenv
from polymarket_tracker import PolymarketTracker
from twitter_client import TwitterClient
from typing import Dict, Any

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('polymarket_tracker.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class PolymarketTwitterBot:
    """Main bot that monitors Polymarket and tweets trades."""

    def __init__(self):
        """Initialize the bot with configuration from environment variables."""
        self.polymarket_address = os.getenv("POLYMARKET_ACCOUNT_ADDRESS")
        self.poll_interval = int(os.getenv("POLL_INTERVAL", 5))
        self.tweet_template = os.getenv("TWEET_TEMPLATE", "default")

        if not self.polymarket_address or self.polymarket_address == "0x":
            raise ValueError("❌ POLYMARKET_ACCOUNT_ADDRESS not configured in .env")

        # Initialize Twitter client
        try:
            self.twitter = TwitterClient(
                api_key=os.getenv("TWITTER_API_KEY"),
                api_secret=os.getenv("TWITTER_API_SECRET"),
                access_token=os.getenv("TWITTER_ACCESS_TOKEN"),
                access_secret=os.getenv("TWITTER_ACCESS_SECRET"),
                bearer_token=os.getenv("TWITTER_BEARER_TOKEN")
            )
        except Exception as e:
            logger.error(f"Failed to initialize Twitter client: {e}")
            raise

        # Initialize Polymarket tracker
        self.tracker = PolymarketTracker(
            account_address=self.polymarket_address,
            poll_interval=self.poll_interval
        )
        
        # Register callback for new trades
        self.tracker.register_activity_callback(self.on_new_trade)

    async def on_new_trade(self, activity_data: Dict[str, Any]):
        """
        Callback when a new trade is detected.
        
        Args:
            activity_data: Activity event data
        """
        try:
            if activity_data.get("type") == "trade":
                trade = activity_data.get("trade", {})
                
                # Format and post tweet
                tweet_text = self.tracker.format_trade_for_tweet(trade, self.tweet_template)
                
                logger.info(f"Posting tweet: {tweet_text}")
                success = await self.twitter.tweet(tweet_text)
                
                if success:
                    logger.info("✅ Trade tweet posted successfully!")
                else:
                    logger.error("❌ Failed to post trade tweet")
                    
        except Exception as e:
            logger.error(f"Error handling new trade: {e}")

    async def validate_setup(self) -> bool:
        """
        Validate bot configuration before starting.
        
        Returns:
            True if setup is valid, False otherwise
        """
        logger.info("🔍 Validating setup...")

        # Validate Twitter credentials
        if not self.twitter.validate_credentials():
            logger.error("❌ Twitter credentials invalid")
            return False
        logger.info("✅ Twitter credentials valid")

        # Validate Polymarket address format
        if not self.polymarket_address.startswith("0x") or len(self.polymarket_address) != 42:
            logger.error("❌ Invalid Polymarket address format (should be 0x-prefixed, 40 hex chars)")
            return False
        logger.info(f"✅ Polymarket address valid: {self.polymarket_address}")

        # Test API connectivity
        try:
            positions = await self.tracker.get_user_positions()
            logger.info(f"✅ Successfully connected to Polymarket Data API (found {len(positions)} positions)")
        except Exception as e:
            logger.error(f"❌ Failed to connect to Polymarket Data API: {e}")
            return False

        return True

    async def start(self):
        """Start the monitoring bot."""
        try:
            logger.info("🚀 Starting Polymarket Twitter Tracker Bot...")

            # Validate setup
            if not await self.validate_setup():
                logger.error("❌ Setup validation failed. Please check your configuration.")
                return

            logger.info("✅ Setup validation passed!")
            logger.info(f"📊 Monitoring account: {self.polymarket_address}")
            logger.info(f"⏱️  Poll interval: {self.poll_interval} seconds")
            logger.info(f"🎨 Tweet template: {self.tweet_template}")
            logger.info("\n🔔 Now monitoring for trades... (Press Ctrl+C to stop)\n")

            # Start polling
            await self.tracker.start_polling()

        except KeyboardInterrupt:
            logger.info("\n⏹️  Stopping bot...")
            await self.shutdown()
        except Exception as e:
            logger.error(f"❌ Fatal error: {e}")
            await self.shutdown()

    async def shutdown(self):
        """Clean up resources."""
        logger.info("Cleaning up...")
        await self.tracker.stop()
        logger.info("✅ Bot stopped")


async def main():
    """Main entry point."""
    try:
        bot = PolymarketTwitterBot()
        await bot.start()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        print("\n📝 Please set up your .env file:")
        print("   1. Copy .env.example to .env")
        print("   2. Add your Polymarket account address")
        print("   3. Add your Twitter API v2 credentials")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

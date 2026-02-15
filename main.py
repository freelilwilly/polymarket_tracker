import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from polymarket_tracker import PolymarketTracker
from twitter_client import TwitterClient
from typing import Dict, Any, List

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('polymarket_tracker.log', maxBytes=10 * 1024 * 1024, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class PolymarketTwitterBot:
    """Main bot that monitors Polymarket and tweets trades."""

    def __init__(self):
        """Initialize the bot with configuration from environment variables."""
        self.polymarket_addresses = self._parse_account_addresses()
        self.poll_interval = int(os.getenv("POLL_INTERVAL", 5))
        self.tweet_template = os.getenv("TWEET_TEMPLATE", "default")
        self.dry_run = self._parse_bool(os.getenv("DRY_RUN", "false"))

        if not self.polymarket_addresses:
            raise ValueError(
                "❌ No Polymarket addresses configured. Set POLYMARKET_ACCOUNT_ADDRESSES or POLYMARKET_ACCOUNT_ADDRESS in .env"
            )

        # Initialize Twitter client (skip in dry-run mode)
        self.twitter = None
        if not self.dry_run:
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

        # Initialize Polymarket trackers
        self.trackers: List[PolymarketTracker] = []
        self.poll_tasks: List[asyncio.Task] = []
        for address in self.polymarket_addresses:
            tracker = PolymarketTracker(
                account_address=address,
                poll_interval=self.poll_interval
            )
            tracker.register_activity_callback(self._build_trade_callback(tracker, address))
            self.trackers.append(tracker)

    def _build_trade_callback(self, tracker: PolymarketTracker, address: str):
        """Create per-tracker callback for new trades."""
        async def on_new_trade(activity_data: Dict[str, Any]):
            try:
                if activity_data.get("type") == "trade":
                    trade = activity_data.get("trade", {})

                    # Format and post tweet
                    tweet_text = tracker.format_trade_for_tweet(trade, self.tweet_template)

                    if self.dry_run:
                        logger.info(f"[DRY RUN] [{address}] Tweet suppressed: {tweet_text}")
                        return

                    logger.info(f"[{address}] Posting tweet: {tweet_text}")
                    success = await self.twitter.tweet(tweet_text)

                    if success:
                        logger.info(f"✅ [{address}] Trade tweet posted successfully!")
                    else:
                        logger.error(f"❌ [{address}] Failed to post trade tweet")

            except Exception as e:
                logger.error(f"Error handling new trade for {address}: {e}")

        return on_new_trade

    async def validate_setup(self) -> bool:
        """
        Validate bot configuration before starting.
        
        Returns:
            True if setup is valid, False otherwise
        """
        logger.info("🔍 Validating setup...")

        # Validate Twitter credentials
        if self.dry_run:
            logger.info("🧪 Dry run enabled: skipping Twitter credential validation")
        else:
            if not self.twitter.validate_credentials():
                logger.error("❌ Twitter credentials invalid")
                return False
            logger.info("✅ Twitter credentials valid")

        # Validate Polymarket address format and connectivity
        for tracker in self.trackers:
            address = tracker.account_address
            if not address.startswith("0x") or len(address) != 42:
                logger.error(
                    f"❌ Invalid Polymarket address format for {address} (should be 0x-prefixed, 40 hex chars)"
                )
                return False
            logger.info(f"✅ Polymarket address valid: {address}")

            try:
                positions = await tracker.get_user_positions()
                logger.info(
                    f"✅ Successfully connected to Polymarket Data API for {address} (found {len(positions)} positions)"
                )
            except Exception as e:
                logger.error(f"❌ Failed to connect to Polymarket Data API for {address}: {e}")
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
            logger.info(f"📊 Monitoring {len(self.trackers)} account(s): {', '.join(self.polymarket_addresses)}")
            logger.info(f"⏱️  Poll interval: {self.poll_interval} seconds")
            logger.info(f"🎨 Tweet template: {self.tweet_template}")
            logger.info(f"🧪 Dry run: {self.dry_run}")
            logger.info("\n🔔 Now monitoring for trades... (Press Ctrl+C to stop)\n")

            # Start polling all trackers concurrently
            self.poll_tasks = [asyncio.create_task(tracker.start_polling()) for tracker in self.trackers]
            await asyncio.gather(*self.poll_tasks)

        except KeyboardInterrupt:
            logger.info("\n⏹️  Stopping bot...")
            await self.shutdown()
        except Exception as e:
            logger.error(f"❌ Fatal error: {e}")
            await self.shutdown()

    async def shutdown(self):
        """Clean up resources."""
        logger.info("Cleaning up...")

        for tracker in self.trackers:
            await tracker.stop()

        for task in self.poll_tasks:
            if not task.done():
                task.cancel()

        if self.poll_tasks:
            await asyncio.gather(*self.poll_tasks, return_exceptions=True)

        logger.info("✅ Bot stopped")

    @staticmethod
    def _parse_bool(value: str) -> bool:
        """Parse a truthy/falsey environment string."""
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _parse_account_addresses() -> List[str]:
        """Parse account addresses from env, supporting single and multi-address variables."""
        addresses_value = os.getenv("POLYMARKET_ACCOUNT_ADDRESSES", "")
        if addresses_value.strip():
            addresses = [value.strip() for value in addresses_value.split(",") if value.strip()]
        else:
            fallback_address = os.getenv("POLYMARKET_ACCOUNT_ADDRESS", "").strip()
            addresses = [fallback_address] if fallback_address else []

        deduped: List[str] = []
        seen = set()
        for address in addresses:
            normalized = address.lower()
            if normalized not in seen:
                deduped.append(address)
                seen.add(normalized)

        return deduped


async def main():
    """Main entry point."""
    try:
        bot = PolymarketTwitterBot()
        await bot.start()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        print("\n📝 Please set up your .env file:")
        print("   1. Copy .env.example to .env")
        print("   2. Add your Polymarket account address(es)")
        print("   3. Add your Twitter API v2 credentials")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

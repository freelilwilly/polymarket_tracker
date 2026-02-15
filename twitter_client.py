import tweepy
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


class TwitterClient:
    """Handles Twitter API v2 interactions."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_secret: str,
        bearer_token: str
    ):
        """
        Initialize Twitter client.
        
        Args:
            api_key: Twitter API key
            api_secret: Twitter API secret
            access_token: Twitter access token
            access_secret: Twitter access token secret
            bearer_token: Twitter bearer token for API v2
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.access_secret = access_secret
        self.bearer_token = bearer_token
        
        self.client: Optional[tweepy.Client] = None
        self._initialize()

    def _initialize(self):
        """Initialize the Tweepy client."""
        try:
            self.client = tweepy.Client(
                bearer_token=self.bearer_token,
                consumer_key=self.api_key,
                consumer_secret=self.api_secret,
                access_token=self.access_token,
                access_token_secret=self.access_secret,
                wait_on_rate_limit=True
            )
            logger.info("Twitter client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Twitter client: {e}")
            raise

    async def tweet(self, text: str) -> bool:
        """
        Post a tweet.
        
        Args:
            text: Tweet content (max 280 characters)
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            logger.error("Twitter client not initialized")
            return False

        # Ensure tweet is within character limit
        if len(text) > 280:
            text = text[:277] + "..."
            logger.warning(f"Tweet truncated to 280 characters")

        try:
            response = self.client.create_tweet(text=text)
            tweet_id = response.data.get("id") if response.data else None
            logger.info(f"Tweet posted successfully: {tweet_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to post tweet: {e}")
            return False

    async def reply_to_tweet(self, tweet_id: str, text: str) -> bool:
        """
        Reply to an existing tweet.
        
        Args:
            tweet_id: ID of tweet to reply to
            text: Reply content
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            logger.error("Twitter client not initialized")
            return False

        if len(text) > 280:
            text = text[:277] + "..."

        try:
            response = self.client.create_tweet(text=text, reply_settings="public", in_reply_to_tweet_id=tweet_id)
            reply_id = response.data.get("id") if response.data else None
            logger.info(f"Reply posted successfully: {reply_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to post reply: {e}")
            return False

    async def tweet_with_media(self, text: str, media_path: str) -> bool:
        """
        Post a tweet with media attachment.
        
        Args:
            text: Tweet content
            media_path: Path to media file
            
        Returns:
            True if successful, False otherwise
        """
        if not self.client:
            logger.error("Twitter client not initialized")
            return False

        if len(text) > 280:
            text = text[:277] + "..."

        try:
            # Upload media first
            media = self.client.upload_media(media_path)
            response = self.client.create_tweet(
                text=text,
                media_ids=[media]
            )
            tweet_id = response.data.get("id") if response.data else None
            logger.info(f"Tweet with media posted successfully: {tweet_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to post tweet with media: {e}")
            return False

    def get_user_info(self) -> Dict[str, Any]:
        """
        Get authenticated user information.
        
        Returns:
            User information dictionary
        """
        if not self.client:
            logger.error("Twitter client not initialized")
            return {}

        try:
            user = self.client.get_me()
            logger.info(f"Retrieved user info: {user.data}")
            return user.data if user.data else {}
        except Exception as e:
            logger.error(f"Failed to get user info: {e}")
            return {}

    def validate_credentials(self) -> bool:
        """
        Validate Twitter credentials.
        
        Returns:
            True if credentials are valid, False otherwise
        """
        try:
            user_info = self.get_user_info()
            if user_info and "id" in user_info:
                logger.info(f"Credentials valid. Logged in as: {user_info.get('username')}")
                return True
            else:
                logger.error("Failed to validate credentials")
                return False
        except Exception as e:
            logger.error(f"Credential validation failed: {e}")
            return False

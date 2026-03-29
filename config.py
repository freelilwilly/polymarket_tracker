"""Configuration management for the copy trade bot."""
import os
from typing import Optional

from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Config:
    """Configuration settings loaded from environment variables."""
    
    # API Credentials (US Trading Account)
    # Get these from https://polymarket.us/developer
    POLYMARKET_KEY_ID: str = os.getenv("POLYMARKET_KEY_ID", "")
    POLYMARKET_SECRET_KEY: str = os.getenv("POLYMARKET_SECRET_KEY", "")
    
    # API Endpoints - US Platform
    US_API_BASE_URL: str = "https://api.polymarket.us"
    
    # Public APIs (International - NOT geo-blocked for reading)
    GAMMA_API_BASE_URL: str = "https://gamma-api.polymarket.com"
    CLOB_API_BASE_URL: str = "https://clob.polymarket.com"
    DATA_API_BASE_URL: str = "https://data-api.polymarket.com"
    ANALYTICS_API_BASE_URL: str = "https://polymarketanalytics.com/api"
    
    # Trading Configuration
    BASE_RISK_PERCENT: float = float(os.getenv("BASE_RISK_PERCENT", "0.01"))
    STARTING_BANKROLL: float = float(os.getenv("STARTING_BANKROLL", "1000"))
    MAX_POSITION_SIZE_PER_MARKET: float = float(os.getenv("MAX_POSITION_SIZE_PER_MARKET", "0.25"))
    MAX_PRICE_TOLERANCE: float = float(os.getenv("MAX_PRICE_TOLERANCE", "0.01"))
    LIQUIDATION_PRICE: float = float(os.getenv("LIQUIDATION_PRICE", "0.98"))
    ENABLE_AUTO_LIQUIDATION: bool = os.getenv("ENABLE_AUTO_LIQUIDATION", "true").lower() in ("true", "1", "yes")
    ALLOW_BUY_SHORT: bool = os.getenv("ALLOW_BUY_SHORT", "true").lower() in ("true", "1", "yes")
    MIN_BUY_PRICE: float = float(os.getenv("MIN_BUY_PRICE", "0.00"))
    MAX_BUY_PRICE: float = float(os.getenv("MAX_BUY_PRICE", "0.90"))
    SPORTS_ONLY: bool = os.getenv("SPORTS_ONLY", "true").lower() in ("true", "1", "yes")
    
    # Trader Selection Criteria
    MIN_WIN_RATE: float = float(os.getenv("MIN_WIN_RATE", "75"))
    MIN_TRADES_PER_DAY: float = float(os.getenv("MIN_TRADES_PER_DAY", "1"))
    MAX_TRADES_PER_DAY: float = float(os.getenv("MAX_TRADES_PER_DAY", "60"))
    TOP_N_USERS: int = int(os.getenv("TOP_N_USERS", "25"))
    CANDIDATE_LIMIT: int = int(os.getenv("CANDIDATE_LIMIT", "500"))
    REQUIRED_TRADER_TAGS: Optional[str] = os.getenv("REQUIRED_TRADER_TAGS", None)
    
    # Polling Configuration
    SCAN_INTERVAL_SECONDS: int = int(os.getenv("SCAN_INTERVAL_SECONDS", "30"))
    DAILY_POLL_SECONDS: int = int(os.getenv("DAILY_POLL_SECONDS", "86400"))
    TRADE_POLL_SECONDS: int = int(os.getenv("TRADE_POLL_SECONDS", "1"))
    TRADE_POLL_CONCURRENCY: int = int(os.getenv("TRADE_POLL_CONCURRENCY", "10"))
    TRADE_PAGE_SIZE: int = int(os.getenv("TRADE_PAGE_SIZE", "200"))
    TRADE_MAX_PAGES_PER_POLL: int = int(os.getenv("TRADE_MAX_PAGES_PER_POLL", "1"))
    TRADE_MAX_PAGES_PER_POLL_BURST: int = int(os.getenv("TRADE_MAX_PAGES_PER_POLL_BURST", "3"))
    TRADE_DUPLICATE_ANOMALY_RATIO: float = float(os.getenv("TRADE_DUPLICATE_ANOMALY_RATIO", "0.60"))
    TRADE_ADAPTIVE_FETCH_ENABLED: bool = os.getenv("TRADE_ADAPTIVE_FETCH_ENABLED", "false").lower() in ("true", "1", "yes")
    POTENTIAL_DUPLICATE_ALERT_THRESHOLD: int = int(os.getenv("POTENTIAL_DUPLICATE_ALERT_THRESHOLD", "5"))
    COPY_LARGEST_BUY_PER_CYCLE_ENABLED: bool = os.getenv("COPY_LARGEST_BUY_PER_CYCLE_ENABLED", "true").lower() in ("true", "1", "yes")
    NON_SPORTS_SKIP_CACHE_SECONDS: int = int(os.getenv("NON_SPORTS_SKIP_CACHE_SECONDS", "1800"))
    US_UNTRADABLE_CACHE_SECONDS: int = int(os.getenv("US_UNTRADABLE_CACHE_SECONDS", "1800"))
    MARKET_INFO_CACHE_SECONDS: int = int(os.getenv("MARKET_INFO_CACHE_SECONDS", "300"))
    MARKET_INFO_NEGATIVE_CACHE_SECONDS: int = int(os.getenv("MARKET_INFO_NEGATIVE_CACHE_SECONDS", "120"))
    MARKET_INFO_WARNING_COOLDOWN_SECONDS: int = int(os.getenv("MARKET_INFO_WARNING_COOLDOWN_SECONDS", "120"))
    MARKET_INFO_WARNING_THRESHOLD: int = int(os.getenv("MARKET_INFO_WARNING_THRESHOLD", "3"))
    BUY_PENDING_RECONCILE_SECONDS: int = int(os.getenv("BUY_PENDING_RECONCILE_SECONDS", "180"))
    BUY_PENDING_RECHECK_SECONDS: int = int(os.getenv("BUY_PENDING_RECHECK_SECONDS", "20"))

    # Owner-link recovery and copied SELL policy
    POSITION_OWNER_RECOVERY_TTL_SECONDS: int = int(os.getenv("POSITION_OWNER_RECOVERY_TTL_SECONDS", "600"))
    POSITION_SYNC_MISS_THRESHOLD: int = int(os.getenv("POSITION_SYNC_MISS_THRESHOLD", "3"))
    SELL_OWNER_CONDITIONAL_ALLOW_ENABLED: bool = os.getenv("SELL_OWNER_CONDITIONAL_ALLOW_ENABLED", "true").lower() in ("true", "1", "yes")

    # Copied SELL sizing behavior
    SELL_PERCENT_SIZING_ENABLED: bool = os.getenv("SELL_PERCENT_SIZING_ENABLED", "true").lower() in ("true", "1", "yes")
    SELL_PERCENT_FALLBACK_MAX_RATIO: float = float(os.getenv("SELL_PERCENT_FALLBACK_MAX_RATIO", "0.50"))
    
    # Size Normalization (percentile-based multiplier logic from polymarket_tracker)
    TAIL_MIN_MULTIPLIER: float = float(os.getenv("TAIL_MIN_MULTIPLIER", "0.9"))                         # Min multiplier (small trades)
    TAIL_MAX_MULTIPLIER: float = float(os.getenv("TAIL_MAX_MULTIPLIER", "1.6"))                         # Max multiplier (large trades)
    TAIL_MULTIPLIER_CURVE_POWER: float = float(os.getenv("TAIL_MULTIPLIER_CURVE_POWER", "1.35"))        # Percentile curve exponent
    TAIL_LOW_SIZE_THRESHOLD_RATIO: float = float(os.getenv("TAIL_LOW_SIZE_THRESHOLD_RATIO", "0.12"))    # Small trade threshold
    TAIL_LOW_SIZE_HAIRCUT_POWER: float = float(os.getenv("TAIL_LOW_SIZE_HAIRCUT_POWER", "0.5"))         # Small trade haircut power
    TAIL_LOW_SIZE_HAIRCUT_MIN_FACTOR: float = float(os.getenv("TAIL_LOW_SIZE_HAIRCUT_MIN_FACTOR", "0.35"))  # Min haircut factor
    TAIL_MAX_TRADE_NOTIONAL_PCT: float = float(os.getenv("TAIL_MAX_TRADE_NOTIONAL_PCT", "0.08"))        # Max 8% per trade

    # Test Mode Configuration
    TEST_EXCEL_WORKBOOK: str = os.getenv("TEST_EXCEL_WORKBOOK", "test_performance.xlsx")
    
    # Live Mode Configuration
    LIVE_EXCEL_WORKBOOK: str = os.getenv("LIVE_EXCEL_WORKBOOK", "live_performance.xlsx")
    
    # Google Sheets Configuration
    GOOGLE_SHEETS_ENABLED: bool = os.getenv("GOOGLE_SHEETS_ENABLED", "false").lower() in ("true", "1", "yes")
    GOOGLE_SHEETS_CREDENTIALS: str = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "google_credentials.json")
    GOOGLE_SHEET_ID: str = os.getenv("GOOGLE_SHEET_ID", "")

    # Logging
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
    LOG_FILE: str = os.getenv("LOG_FILE", "copytrade.log")
    
    # Slug Resolution
    SLUG_RESOLUTION_MAX_FALLBACKS: int = int(os.getenv("SLUG_RESOLUTION_MAX_FALLBACKS", "3"))
    SLUG_PERSIST_LEARNED_MAPPINGS: bool = os.getenv("SLUG_PERSIST_LEARNED_MAPPINGS", "true").lower() in ("true", "1", "yes")
    SLUG_LEARNED_MAPPINGS_FILE: str = os.getenv("SLUG_LEARNED_MAPPINGS_FILE", "learned_slug_mappings.json")
    
    @classmethod
    def validate(cls) -> None:
        """Validate required configuration."""
        if not cls.POLYMARKET_KEY_ID:
            raise ValueError("POLYMARKET_KEY_ID is required")
        if not cls.POLYMARKET_SECRET_KEY:
            raise ValueError("POLYMARKET_SECRET_KEY is required")

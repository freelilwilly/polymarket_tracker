# Polymarket Twitter Tracker 🚀

A real-time monitoring tool that tracks Polymarket account trades and automatically tweets them out. Built with Python, async/await, and Twitter API v2.

## Features

- **Real-time Monitoring**: Polls Polymarket Data API for new trades
- **Automatic Tweeting**: Posts formatted tweets for each new trade
- **Multiple Tweet Templates**: Choose between default, detailed, or minimal formats
- **Async/Await**: Efficient concurrent operations using Python's asyncio
- **Error Handling**: Robust error handling with comprehensive logging
- **Configurable**: Easy setup via environment variables

## Prerequisites

- Python 3.8+
- A Polymarket account with an Ethereum wallet address
- Twitter Developer Account with API v2 credentials

## Installation

### 1. Clone and Setup

```bash
cd polymarket_tracker
pip install -r requirements.txt
```

### 2. Configure Polymarket Account

```bash
# Copy the example config
cp .env.example .env

# Edit .env and add your details
```

### 3. Get Twitter API v2 Credentials

1. Go to [Twitter Developer Portal](https://developer.twitter.com/en/portal/dashboard)
2. Create a new application or use an existing one
3. Enable **OAuth 2.0** and **API v2** access
4. Generate the following credentials:
   - **API Key** (Consumer Key)
   - **API Secret** (Consumer Secret)
   - **Access Token**
   - **Access Token Secret**
   - **Bearer Token**

### 4. Configure .env File

```bash
# .env
POLYMARKET_ACCOUNT_ADDRESS=0x1234567890abcdef...  # Your 0x-prefixed wallet address

# Twitter Credentials
TWITTER_API_KEY=your_api_key
TWITTER_API_SECRET=your_api_secret
TWITTER_ACCESS_TOKEN=your_access_token
TWITTER_ACCESS_SECRET=your_access_secret
TWITTER_BEARER_TOKEN=your_bearer_token

# Settings
POLL_INTERVAL=5                    # Seconds between checks
TWEET_TEMPLATE=default             # Format: default, detailed, or minimal
```

## Usage

### Basic Usage

```bash
python main.py
```

The bot will:
1. ✅ Validate your configuration
2. ✅ Test Twitter credentials
3. ✅ Test Polymarket API connectivity
4. 🔔 Start monitoring your account for trades
5. 🐦 Automatically tweet new trades

### Tweet Templates

#### Default Template
```
📊 Just traded on Polymarket!

Market Title
Outcome
Size: X @ $Y.ZZ

#Polymarket #Prediction
```

#### Detailed Template
```
🚀 Polymarket Trade!

Market Title
📌 Outcome
🔀 BUY/SELL
💰 X shares @ $Y.ZZ

#Trading #Crypto
```

#### Minimal Template
```
Outcome: X @ $Y.ZZ 📈 #Polymarket
```

## Project Structure

```
polymarket_tracker/
├── main.py                 # Main bot script
├── polymarket_tracker.py   # Polymarket API integration
├── twitter_client.py       # Twitter API v2 client
├── requirements.txt        # Python dependencies
├── .env.example           # Example configuration
└── .gitignore
```

## Module Documentation

### `PolymarketTracker` (polymarket_tracker.py)

Handles Polymarket API interactions.

**Key Methods:**
- `get_user_positions()` - Fetch current positions
- `get_user_activity()` - Fetch activity history
- `get_user_trades()` - Fetch trade history
- `start_polling()` - Start monitoring for trades
- `format_trade_for_tweet()` - Format trade data for Twitter
- `register_activity_callback()` - Register callback on new trades

**Example:**
```python
tracker = PolymarketTracker("0x1234...", poll_interval=5)
await tracker.initialize()
trades = await tracker.get_user_trades()
```

### `TwitterClient` (twitter_client.py)

Handles Twitter API v2 interactions.

**Key Methods:**
- `tweet(text)` - Post a tweet
- `validate_credentials()` - Verify API credentials
- `get_user_info()` - Get authenticated user info

**Example:**
```python
twitter = TwitterClient(api_key, api_secret, access_token, access_secret, bearer_token)
await twitter.tweet("Hello, Polymarket!")
```

## Logging

All activity is logged to:
- **Console**: Real-time updates
- **File**: `polymarket_tracker.log` for historical records

Logs include:
- API requests and responses
- New trades detected
- Tweet posts (success/failure)
- Errors and exceptions

## API Reference

### Polymarket Data API

The tool uses these Polymarket endpoints:

- `GET /positions` - User's current positions
- `GET /activity` - User's activity history
- `GET /trades` - User's trade history

Base URL: `https://data-api.polymarket.com`

### Twitter API v2

- `POST /2/tweets` - Create a tweet
- `GET /2/users/me` - Get authenticated user

## Troubleshooting

### "POLYMARKET_ACCOUNT_ADDRESS not configured"
- Ensure your `.env` file exists and has the account address
- Address must be 0x-prefixed and 40 hex characters

### "Twitter credentials invalid"
- Verify all Twitter credentials in `.env`
- Ensure credentials have API v2 and OAuth access
- Check [Twitter Developer Portal](https://developer.twitter.com)

### "Failed to connect to Polymarket Data API"
- Verify the account address exists and has positions
- Check your internet connection
- Polymarket API may be temporarily down

### Tweets not posting
- Check tweet length (max 280 characters)
- Verify Twitter credentials are enabled
- Check rate limits (15 tweets per 15 minutes)
- Review `polymarket_tracker.log` for details

## Rate Limits

- **Polymarket API**: Respects standard HTTP rate limits
- **Twitter API v2**: 300 requests per 15 minutes (read), 450 requests per 15 minutes (write)

The bot includes built-in rate limit handling and will wait if needed.

## Advanced Usage

### Custom Tweet Formatting

Modify the `format_trade_for_tweet()` method in `PolymarketTracker` to create custom formats:

```python
def custom_format(self, trade: Dict[str, Any]) -> str:
    outcome = trade.get("outcome", "Unknown")
    size = trade.get("size", 0)
    return f"🎯 {outcome} | {size} shares | #MyCustomFormat"
```

### Multiple Accounts

Create separate `.env` files for each account:

```bash
python main.py .env.account1
python main.py .env.account2
```

Modify `main.py` to accept env file argument:

```python
load_dotenv(sys.argv[1] if len(sys.argv) > 1 else '.env')
```

## Security Best Practices

1. **Never commit `.env`** - Add to `.gitignore`
2. **Use environment variables** - Don't hardcode credentials
3. **Rotate credentials regularly** - Use Twitter's credential rotation
4. **Monitor API usage** - Watch for unusual activity
5. **Use rate limiting** - Adjust `POLL_INTERVAL` to avoid API abuse

## Contributing

To extend the tool:

1. Add new tweet templates to `format_trade_for_tweet()`
2. Integrate additional APIs (Discord, Slack, etc.)
3. Add position change tracking
4. Implement WebSocket for real-time updates

## License

MIT License - See LICENSE file for details

## Disclaimer

This tool is for educational and personal use only. The author is not responsible for any trades, losses, or outcomes from using this tool. Always validate trades before placing them.

## Support

For issues or questions:
1. Check the logs: `polymarket_tracker.log`
2. Verify the [Polymarket API docs](https://docs.polymarket.com)
3. Check [Twitter API docs](https://developer.twitter.com/en/docs)
4. Review the code comments

---

**Made for Polymarket traders who want real-time notifications** 📊🐦

# Polymarket Copy Trading Bot

Automatically copy trades from top-performing traders on Polymarket's sports markets.

## Features

- **Dual Mode Operation**: Test (simulation) and Live (real money) modes
- **Top Trader Selection**: Automatically identifies and follows profitable traders
- **Sports Markets Only**: US API limitation restricts trading to sports markets
- **Position Management**: 
  - Per-market exposure caps (25% of balance max per market)
  - Automatic position tracking
  - Real-time P&L calculation
- **Risk Controls**:
  - Price range filters (MIN_BUY_PRICE / MAX_BUY_PRICE)
   - Early observed-price prefilter for copied BUY events
  - Market exposure limits
  - Over-sell protection (live API verification)
   - US-untradable market cache to avoid repeated futile quote/sizing work
- **Auto-Liquidation** (Optional):
   - Automatically places `LIQUIDATION_PRICE` limit orders on positions
  - Toggle with ENABLE_AUTO_LIQUIDATION setting
- **Excel Tracking**: Comprehensive performance tracking in Excel workbooks
- **Slug Learning**: Automatically learns market slug mappings for improved matching

## How It Works

1. **Trader Selection**:
   - Fetches top traders from Polymarket Analytics API
   - Filters by win rate and overall gain
   - Selects configurable number of traders to monitor

2. **Trade Monitoring**:
   - Monitors selected traders' recent trades via Data API
   - Deduplicates trades to avoid processing same trade multiple times
   - Filters for sports markets only

3. **Trade Execution**:
   - **BUY signals**: Opens new positions when monitored traders buy
       - Skips immediately when copied observed price is outside configured range
       - Validates current quote is within configured range
     - Checks market exposure caps
     - Verifies NO-side trading is enabled if applicable
       - Caches US-untradable slugs after confirmed US order rejection
   - **SELL signals**: Closes positions when monitored traders sell
     - Verifies position exists before selling
     - Protects against over-selling with live API verification

4. **Position Management**:
   - Tracks all open positions with entry prices
   - Per-market exposure cap: **25% of balance maximum per market**
   - Syncs with API to detect external position changes
   - Persists state to `positions_state.json`

5. **Auto-Liquidation** (Optional):
   - When enabled, automatically places $0.98 limit orders on positions
   - Monitors orders for fills
   - Recreates orders if canceled/expired
   - Can be toggled with ENABLE_AUTO_LIQUIDATION setting

## Installation

1. Clone repository:
```bash
git clone <repository-url>
cd polymarket_tracker
```

2. Create virtual environment:
```bash
python -m venv .venv
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # Linux/Mac
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Configure environment:
```bash
cp .env.example .env
# Edit .env with your settings
```

## Configuration

All configuration is done via `.env` file. See `.env.example` for full details.

### Required Settings (Live Mode Only)

```env
POLYMARKET_KEY_ID=your_api_key_id
POLYMARKET_SECRET_KEY=your_base64_encoded_secret
```

### Trading Parameters

```env
BASE_RISK_PERCENT=0.01                # Base notional = 1% of account balance
TAIL_MAX_TRADE_NOTIONAL_PCT=0.08      # Hard per-trade cap as % of account balance
MAX_POSITION_SIZE_PER_MARKET=0.25    # 25% max per market
TOP_N_USERS=25                        # Number of traders to monitor
MIN_WIN_RATE=75                       # Minimum win rate threshold
MIN_TRADES_PER_DAY=1                  # Minimum activity threshold
MAX_TRADES_PER_DAY=75                 # Maximum activity threshold
SCAN_INTERVAL_SECONDS=30             # Time between monitoring cycles
US_UNTRADABLE_CACHE_SECONDS=1800     # Cache US-untradable slugs for 30 minutes
```

### Risk Controls

```env
ALLOW_BUY_SHORT=true                 # Enable NO-side trading
ENABLE_AUTO_LIQUIDATION=true         # Enable $0.98 limit orders
MIN_BUY_PRICE=0.10                   # Minimum price filter
MAX_BUY_PRICE=0.90                   # Maximum price filter
```

### Sports-Only Limitation

**IMPORTANT**: The US API only supports sports markets. The bot automatically filters for sports markets (NBA, NFL, NHL, MLB, etc.). Non-sports markets are automatically skipped.

## Usage

### Test Mode (Simulated Trading)

Test mode uses NO authentication and simulates trades without real money:

```bash
run_test.bat  # Windows
# or
python main_test.py
```

- Uses simulated balance ($1000 default)
- No real API credentials required
- Creates `test_performance.xlsx`
- Perfect for testing strategy changes

### Live Mode (Real Money)

Live mode requires API credentials and executes real trades:

```bash
run_live.bat  # Windows
# or
python main_live.py
```

- Requires valid API credentials in `.env`
- Executes real trades with real money
- Creates `live_performance.xlsx`
- **USE WITH CAUTION**

## Excel Tracking

Both modes generate Excel workbooks with three sheets:

1. **Balance History**: Time-series balance tracking
2. **Trades**: Log of all buy/sell operations
3. **Open Positions**: Current positions with real-time P&L

## Position Management Details

### Per-Market Exposure Cap

The bot enforces a **25% maximum exposure per market** (configurable via MAX_POSITION_SIZE_PER_MARKET):

- Prevents over-concentration in single markets
- Calculated as: current_market_exposure + new_investment ≤ balance × 0.25
- Example: With $1000 balance, max $250 can be invested across all positions in one market

### Position Syncing

The bot periodically syncs local positions with API positions to detect:
- Positions closed externally (via web UI or other tools)
- Share count mismatches
- Stale position data

### State Persistence

All positions are persisted to `positions_state.json`:
- Survives bot restarts
- Tracks entry prices, invested amounts
- Includes monitored trader info

## Auto-Liquidation

When `ENABLE_AUTO_LIQUIDATION=true`:

1. **Order Creation**:
   - Automatically places $0.98 limit orders on all positions
   - Orders created in next monitoring cycle after position opens

2. **Order Monitoring**:
   - Checks order status every cycle
   - Detects fills and updates position tracking
   - Recreates orders if canceled/expired

3. **Order Cancellation**:
   - Automatically cancels liquidation order when trader signals SELL
   - Allows immediate market-price exit instead of waiting for $0.98

4. **Disabling**:
   - Set `ENABLE_AUTO_LIQUIDATION=false` to disable
   - Bot will only execute trades based on trader signals

## Price Filters

Configurable price range to avoid extreme positions:

- `MIN_BUY_PRICE`: Reject buys below this price (default: 0.10)
- `MAX_BUY_PRICE`: Reject buys above this price (default: 0.90)

Example: With defaults, bot only buys positions priced between $0.10-$0.90.

## NO-Side Trading

The US API supports buying NO tokens via `ORDER_INTENT_BUY_SHORT`:

**IMPORTANT**: Despite the name "BUY_SHORT", this creates a regular LONG position on the NO outcome, NOT a leveraged short position.

- `ALLOW_BUY_SHORT=true`: Enables buying NO-side tokens
- `ALLOW_BUY_SHORT=false`: Only buys YES-side tokens

## File Structure

```
api_client.py              # API communication
config.py                  # Configuration management
slug_converter.py          # Market slug mapping
sports_filter.py           # Sports market detection
trader_selector.py         # Top trader selection
trade_monitor.py           # Trade monitoring
trade_executor.py          # Trade execution
position_manager.py        # Position tracking
liquidation_manager.py     # Auto-liquidation logic
excel_tracker.py           # Excel workbook tracking
utils.py                   # Utility functions
main_live.py               # Live mode entry point
main_test.py               # Test mode entry point
```

## Runtime Files

These files are generated at runtime and should NOT be committed:

- `positions_state.json` - Position tracking state
- `learned_slug_mappings.json` - Learned market slug mappings
- `*.xlsx` - Excel performance workbooks
- `copytrade.log` - Application logs
- `.env` - Environment configuration

All are excluded via `.gitignore`.

## Logging

Logs are written to:
- `copytrade.log` (file)
- Console (stdout)

Log level: INFO (configurable in main_*.py files)

Account summary log cadence:
- `Account: cash=... | positions=... | equity=... | ... open` logs once per minute on an independent timer.
- Balance/position refresh execution cadence remains unchanged.

## Safety Features

1. **Over-Sell Protection**: Verifies position exists in API before selling
2. **Market Exposure Caps**: Prevents over-concentration (25% per market)
3. **Price Filters**: Avoids buying extreme prices
4. **Position Syncing**: Detects external position changes
5. **State Persistence**: Survives restarts without losing position data
6. **Sports-Only Filter**: Automatically skips non-sports markets (US API limitation)

## Troubleshooting

### "Missing API credentials" Error
- Ensure `.env` file exists and contains valid POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY
- Only required for Live mode

### "No traders selected" Error
- Analytics API may be unavailable
- Try adjusting TOP_N_USERS, MIN_WIN_RATE, or MIN_TRADES_PER_DAY settings

### Positions Not Syncing
- Check internet connection
- Verify API credentials are valid
- Check `copytrade.log` for errors

### Non-Sports Markets Being Skipped
- This is expected behavior - US API only supports sports markets
- The sports filter automatically detects: NBA, NFL, NHL, MLB, MMA, Boxing, Soccer, etc.

### "Cannot get buy price for market"
- This indicates current quote retrieval could not find an actionable buy quote.
- Copied BUY events outside `MIN_BUY_PRICE`/`MAX_BUY_PRICE` are now skipped before quote lookup.
- If US order placement confirms a slug is untradable, the slug is cached for `US_UNTRADABLE_CACHE_SECONDS` to reduce repeated calls.

### Non-trade wallet activity (e.g., REDEEM)
- Non-BUY/SELL activity is ignored silently and not treated as a copy-trade signal.

## Disclaimer

**USE AT YOUR OWN RISK**. This bot executes real financial transactions in Live mode. The developers assume NO responsibility for financial losses. Always test thoroughly in Test mode before using Live mode.

## License

MIT

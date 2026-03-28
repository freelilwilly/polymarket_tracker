# Quick Start Guide

Get the Polymarket copy trading bot running in under 5 minutes.

## Prerequisites

- Python 3.8 or higher
- Polymarket API credentials (for Live mode only)
- Windows/Linux/Mac

## Step 1: Setup

```bash
# Clone and navigate to directory
cd polymarket_tracker

# Create virtual environment
python -m venv .venv

# Activate virtual environment
.venv\Scripts\activate  # Windows
source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt
```

## Step 2: Configure

```bash
# Copy example configuration
cp .env.example .env

# Edit .env with your settings
# (Only needed for Live mode - Test mode works without credentials)
```

### Minimal Test Mode Configuration

For testing, you can use `.env.example` as-is:

```env
# Default test mode settings work out of the box
BASE_RISK_PERCENT=0.01
TAIL_MAX_TRADE_NOTIONAL_PCT=0.08
SCAN_INTERVAL_SECONDS=30
US_UNTRADABLE_CACHE_SECONDS=1800
TOP_N_USERS=25
MIN_WIN_RATE=75
MIN_TRADES_PER_DAY=1
```

### Live Mode Configuration

For real trading, add your API credentials:

```env
POLYMARKET_KEY_ID=your_api_key_id_here
POLYMARKET_SECRET_KEY=your_base64_encoded_secret_here
```

## Step 3: Run Test Mode

Always start with test mode to verify everything works:

```bash
# Windows
run_test.bat

# Linux/Mac
python main_test.py
```

You should see:
```
================================================================================
TEST MODE - SIMULATED TRADING (NO REAL MONEY)
================================================================================
Simulated balance: $1000.00
Selecting top traders...
Monitoring 15 traders
```

Press `Ctrl+C` to stop.

## Step 4: Review Results

Check `test_performance.xlsx` to see:
- Balance history over time
- All simulated trades
- Current open positions with P&L

## Step 5: Run Live Mode (Optional)

**ONLY after testing thoroughly:**

```bash
# Windows
run_live.bat

# Linux/Mac
python main_live.py
```

You should see:
```
================================================================================
LIVE TRADING MODE - REAL MONEY
================================================================================
Starting balance: $XXX.XX
```

## Key Settings to Adjust

### Base Risk Sizing
```env
BASE_RISK_PERCENT=0.01       # 1% of equity base sizing
TAIL_MAX_TRADE_NOTIONAL_PCT=0.08  # hard cap per trade
```
Start small by lowering `BASE_RISK_PERCENT` (for example 0.25%-0.50%).

### Market Exposure
```env
MAX_POSITION_SIZE_PER_MARKET=0.25  # 25% max per market
```
Default 25% prevents over-concentration in single markets.

### Price Filters
```env
MIN_BUY_PRICE=0.10  # Don't buy below $0.10
MAX_BUY_PRICE=0.90  # Don't buy above $0.90
```
Protects against extreme/illiquid positions. Copied BUY events outside this range are skipped before quote lookup.

### US Untradable Cache
```env
US_UNTRADABLE_CACHE_SECONDS=1800  # Cache slugs confirmed untradable in US for 30 minutes
```
Reduces repeated quote/sizing work for markets that US order placement has already rejected.

### Auto-Liquidation
```env
ENABLE_AUTO_LIQUIDATION=true  # Place $0.98 limit orders
```
Automatically creates profit-taking orders at $0.98.

### NO-Side Trading
```env
ALLOW_BUY_SHORT=true  # Enable buying NO tokens
```
Set to `false` to only buy YES-side positions.

## Monitoring the Bot

### Real-time Logs

Watch the console output or check `copytrade.log`:
```bash
tail -f copytrade.log  # Linux/Mac
Get-Content copytrade.log -Wait -Tail 20  # Windows PowerShell
```

Account summary logs are emitted once per minute on an independent timer:
`Account: cash=... | positions=... | equity=... | ... open`

### Excel Tracking

Open the Excel workbook while bot is running:
- Test mode: `test_performance.xlsx`
- Live mode: `live_performance.xlsx`

Refresh Excel to see latest data.

### Position Status

Check `positions_state.json` to see current positions:
```json
{
  "positions": {
    "market-slug|yes": {
      "market_slug": "nba-lal-gsw-2026-03-28",
      "outcome": "yes",
      "shares": 250.50,
      "entry_price": 0.4500,
      "invested": 112.73
    }
  }
}
```

## Common Workflows

### Testing a Configuration Change

1. Stop the bot (`Ctrl+C`)
2. Edit `.env`
3. Delete `test_performance.xlsx` (for fresh start)
4. Run `run_test.bat` or `python main_test.py`

### Starting Fresh

Delete runtime files to reset:
```bash
# Windows PowerShell
Remove-Item positions_state.json, learned_slug_mappings.json, *.xlsx, copytrade.log -ErrorAction SilentlyContinue

# Linux/Mac
rm -f positions_state.json learned_slug_mappings.json *.xlsx copytrade.log
```

### Switching Modes

Test and Live modes use separate files:
- State: Different `positions_state.json` content
- Excel: Different workbook files
- Can run one after another without interference

## Typical Bot Cycle

At configured maintenance cadence (`SCAN_INTERVAL_SECONDS`), the bot:

1. **Syncs Positions**: Checks API for position changes
2. **Manages Liquidation**: Creates/monitors $0.98 limit orders (if enabled)
3. **Monitors Traders**: Fetches recent trades from top traders
4. **Processes Signals**:
   - BUY: Opens new position if criteria met
   - SELL: Closes existing position
5. **Updates Excel**: Logs balance and position status
6. **Waits**: Sleeps until next cycle

Separately, trader monitoring runs at `TRADE_POLL_SECONDS`, and account summary logging runs once per minute on its own timer.

## Stopping the Bot

Press `Ctrl+C` to gracefully shutdown:
```
Shutdown requested by user
Shutting down...
Shutdown complete
```

The bot will:
- Cancel all liquidation orders (Live mode)
- Save slug mappings
- Close Excel tracker
- Shut down API client

## Next Steps

- Read the full [README.md](README.md) for detailed documentation
- Review `.env.example` for all available settings
- Monitor test mode for 24+ hours before going live
- Start with low `BASE_RISK_PERCENT` in live mode

## Troubleshooting

### "No traders selected"
- Analytics API may be temporarily unavailable
- Try lowering `MIN_WIN_RATE` or `MIN_TRADES_PER_DAY`, or increasing `TOP_N_USERS`

### "Cannot get price for market"
- Market may be inactive or resolved
- Order book may be empty (low liquidity)
- Normal - bot will skip this trade

### Non-BUY/SELL activity in trader feed
- Events like `REDEEM` are ignored silently and not treated as copy-trade signals.

### Excel file locked
- Close Excel before running bot
- Or let bot update in background and refresh manually

### Sports markets only
- US API limitation - only sports markets supported
- Bot automatically filters for NBA, NFL, NHL, MLB, etc.
- Non-sports markets will be skipped automatically

## Safety Reminders

1. **Test First**: Always run test mode extensively before live mode
2. **Start Small**: Use low `BASE_RISK_PERCENT` initially (for example 0.25%-0.50%)
3. **Monitor Closely**: Watch the first few hours of live trading
4. **Set Limits**: Use price filters and exposure caps
5. **Keep Credentials Safe**: Never commit `.env` file

## Support

Check `copytrade.log` for detailed error messages and debugging information.

---

**DISCLAIMER**: This bot trades with real money in Live mode. Use at your own risk. Always test thoroughly first.

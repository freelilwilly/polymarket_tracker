# Polymarket Twitter Tracker (V2)

V2 is now the default bot. Running `python main.py` executes the top-user selector + live polling workflow from `main_v2.py`.

## What V2 does

- Rebuilds a ranked set of top users on a daily cadence.
- Filters by win rate and trading frequency.
- Polls selected wallets continuously for new trades only.
- Posts to X/Twitter in live mode, or writes to `V2_OUTPUT_FILE` in dry-run mode.
- Can record a live tail-trading simulation to an Excel workbook instead of posting.

## Quick Start

1. Install dependencies:

```bash
pip install -r requirements.txt
```

2. Configure `.env` (use `.env.example` as template):

```dotenv
# Twitter API v2
TWITTER_API_KEY=...
TWITTER_API_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_SECRET=...
TWITTER_BEARER_TOKEN=...

# V2 behavior
DRY_RUN=false
V2_TOP_USERS=10
V2_MIN_WIN_RATE=80
V2_MIN_TRADES_PER_DAY=1
V2_MAX_TRADES_PER_DAY=50
V2_TRADE_POLL_SECONDS=30
V2_TRADE_PAGE_SIZE=200
V2_TRADE_MAX_PAGES_PER_POLL=10
V2_DAILY_POLL_SECONDS=86400
V2_ANALYTICS_ROWS_RETRY_SECONDS=15
V2_OUTPUT_FILE=test_output.txt
V2_EXCEL_MODE=false
V2_EXCEL_WORKBOOK=tail_performance.xlsx
V2_TAIL_STARTING_BANKROLL=1000
V2_TAIL_BASE_RISK_PCT=0.025
V2_TAIL_MIN_MULTIPLIER=0.9
V2_TAIL_MAX_MULTIPLIER=1.6
V2_TAIL_MULTIPLIER_CURVE_POWER=1.35
V2_TAIL_LOW_SIZE_THRESHOLD_RATIO=0.12
V2_TAIL_LOW_SIZE_HAIRCUT_POWER=0.5
V2_TAIL_LOW_SIZE_HAIRCUT_MIN_FACTOR=0.35
V2_TAIL_MAX_TRADE_NOTIONAL_PCT=0.08
V2_TAIL_MAX_MARKET_NOTIONAL_PCT=0.25
V2_TAIL_MAX_ACCOUNT_NOTIONAL_PCT=0.30
```

3. Run:

```bash
python main.py
```

## Modes

- `DRY_RUN=true`: no tweets are posted; output is appended to `V2_OUTPUT_FILE`.
- `DRY_RUN=false`: tweets are posted through configured Twitter credentials.
- `V2_RUN_ONCE=true`: run one rebuild + one poll cycle, then exit.
- `V2_EXCEL_MODE=true`: no tweets are posted; trades are simulated into `V2_EXCEL_WORKBOOK` with live summary metrics.

For high-activity wallets, increase coverage per poll using `V2_TRADE_PAGE_SIZE` and `V2_TRADE_MAX_PAGES_PER_POLL`.

## Notes

- V2 uses Polymarket Analytics only for candidate selection and retries until rows are returned.
- The legacy v1 implementation is no longer the default entrypoint.

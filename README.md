# Polymarket Twitter Tracker (V2)

V2 is now the default bot. Running `python main.py` executes the top-user selector + live polling workflow from `main_v2.py`.

## What V2 does

- Rebuilds a ranked set of top users on a daily cadence.
- Filters by win rate and trading frequency.
- Polls selected wallets continuously for new trades only.
- Posts to X/Twitter in live mode, or writes to `V2_OUTPUT_FILE` in dry-run mode.

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
V2_MAX_TRADES_PER_DAY=25
V2_TRADE_POLL_SECONDS=60
V2_TRADE_PAGE_SIZE=200
V2_TRADE_MAX_PAGES_PER_POLL=10
V2_DAILY_POLL_SECONDS=86400
V2_OUTPUT_FILE=test_output.txt
```

3. Run:

```bash
python main.py
```

## Modes

- `DRY_RUN=true`: no tweets are posted; output is appended to `V2_OUTPUT_FILE`.
- `DRY_RUN=false`: tweets are posted through configured Twitter credentials.
- `V2_RUN_ONCE=true`: run one rebuild + one poll cycle, then exit.

For high-activity wallets, increase coverage per poll using `V2_TRADE_PAGE_SIZE` and `V2_TRADE_MAX_PAGES_PER_POLL`.

## Notes

- V2 uses Polymarket Analytics as primary candidate source with fallback to Polymarket leaderboard.
- The legacy v1 implementation is no longer the default entrypoint.

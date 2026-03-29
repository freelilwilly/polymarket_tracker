"""Google Sheets performance tracking (mirrors ExcelTracker interface)."""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import gspread
    from google.oauth2.service_account import Credentials

    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False


class GoogleSheetsTracker:
    """Google Sheets tracker with the same interface as ExcelTracker."""

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    def __init__(self, credentials_file: str, sheet_id: str):
        self.sheet_id = sheet_id
        self.gc = None
        self.spreadsheet = None
        self._balance_ws = None
        self._trades_ws = None
        self._positions_ws = None

        if not GSPREAD_AVAILABLE:
            logger.warning("gspread not installed; Google Sheets tracking disabled")
            return

        try:
            creds = Credentials.from_service_account_file(credentials_file, scopes=self.SCOPES)
            self.gc = gspread.authorize(creds)
            self.spreadsheet = self.gc.open_by_key(sheet_id)
            self._ensure_worksheets()
            logger.info(f"Google Sheets tracker initialized: {self.spreadsheet.title}")
        except Exception as e:
            logger.exception(f"Failed to initialize Google Sheets tracker: {e}")
            self.spreadsheet = None

    def _ensure_worksheets(self):
        existing = [ws.title for ws in self.spreadsheet.worksheets()]

        if "Balance History" not in existing:
            ws = self.spreadsheet.add_worksheet("Balance History", rows=1000, cols=6)
            ws.append_row(["Timestamp", "Balance", "Invested", "Available", "Total Positions", "P&L"])
        self._balance_ws = self.spreadsheet.worksheet("Balance History")

        if "Trades" not in existing:
            ws = self.spreadsheet.add_worksheet("Trades", rows=1000, cols=9)
            ws.append_row(["Timestamp", "Market", "Outcome", "Side", "Shares", "Price", "Amount", "Trader", "Status"])
        self._trades_ws = self.spreadsheet.worksheet("Trades")

        if "Open Positions" not in existing:
            ws = self.spreadsheet.add_worksheet("Open Positions", rows=1000, cols=11)
            ws.append_row(["Market", "Outcome", "Shares", "Entry Price", "Invested",
                           "Current Price", "Current Value", "P&L", "P&L %", "Opened", "Trader"])
        self._positions_ws = self.spreadsheet.worksheet("Open Positions")

        # Remove default Sheet1 if our sheets were just created
        if "Sheet1" in existing and len(existing) > 1:
            try:
                self.spreadsheet.del_worksheet(self.spreadsheet.worksheet("Sheet1"))
            except Exception:
                pass

    def log_balance(self, balance: float, invested: float, total_positions: int, pnl: Optional[float] = None):
        if not self._balance_ws:
            return
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            available = balance - invested
            row = [timestamp, f"{balance:.2f}", f"{invested:.2f}", f"{available:.2f}",
                   total_positions, f"{pnl:.2f}" if pnl is not None else "N/A"]
            self._balance_ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            logger.warning(f"Google Sheets log_balance error: {e}")

    def log_trade(self, market_slug: str, outcome: str, side: str, shares: float,
                  price: float, trader: Optional[str] = None, status: str = "pending"):
        if not self._trades_ws:
            return
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            amount = shares * price
            row = [timestamp, market_slug, outcome.upper(), side.upper(),
                   f"{shares:.2f}", f"{price:.4f}", f"{amount:.2f}", trader or "N/A", status]
            self._trades_ws.append_row(row, value_input_option="USER_ENTERED")
        except Exception as e:
            logger.warning(f"Google Sheets log_trade error: {e}")

    def update_positions(self, positions: list[dict[str, Any]],
                         position_pnl_map: Optional[dict[str, dict[str, float]]] = None):
        if not self._positions_ws:
            return
        try:
            # Clear all rows below header, then write fresh
            self._positions_ws.resize(rows=1)
            self._positions_ws.resize(rows=max(2, len(positions) + 1))

            rows = []
            for position in positions:
                market_slug = position["market_slug"]
                outcome = position["outcome"]
                shares = position["shares"]
                entry_price = position["entry_price"]
                invested = position.get("invested", shares * entry_price)
                opened_at = position.get("opened_at", "N/A")
                trader = position.get("monitored_trader", "N/A")

                if isinstance(opened_at, str) and opened_at != "N/A":
                    try:
                        dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                        opened_at = dt.strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        pass

                current_price = "N/A"
                current_value = "N/A"
                pnl = "N/A"
                pnl_pct = "N/A"

                if position_pnl_map:
                    position_key = f"{market_slug}|{outcome.lower()}"
                    pnl_data = position_pnl_map.get(position_key)
                    if pnl_data:
                        cv = pnl_data.get("current_value")
                        if cv is not None and shares > 0:
                            current_price = f"{cv / shares:.4f}"
                            current_value = f"{cv:.2f}"
                            pnl = f"{pnl_data.get('pnl', 0):.2f}"
                            pnl_pct = f"{pnl_data.get('pnl_pct', 0):.2f}%"

                rows.append([market_slug, outcome.upper(), f"{shares:.2f}", f"{entry_price:.4f}",
                             f"{invested:.2f}", current_price, current_value, pnl, pnl_pct,
                             opened_at, trader])

            if rows:
                self._positions_ws.update(f"A2:K{len(rows) + 1}", rows, value_input_option="USER_ENTERED")
        except Exception as e:
            logger.warning(f"Google Sheets update_positions error: {e}")

    def close(self):
        logger.info("Google Sheets tracker closed")

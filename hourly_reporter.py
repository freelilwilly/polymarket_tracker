import argparse
import os
import time
from datetime import datetime

from openpyxl import load_workbook


def to_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def print_report(workbook: str, label: str, warn_roi: float):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("=" * 72, flush=True)
    print(f"HOURLY TAIL METRICS | PROFILE {label} | {timestamp}", flush=True)
    print("-" * 72, flush=True)

    if not os.path.exists(workbook):
        print(f"Workbook: {workbook}", flush=True)
        print("Status: workbook not found", flush=True)
        print("=" * 72, flush=True)
        return

    wb = load_workbook(workbook, data_only=True)
    if "Summary" not in wb.sheetnames:
        print(f"Workbook: {workbook}", flush=True)
        print("Status: Summary sheet missing", flush=True)
        print("=" * 72, flush=True)
        return

    ws = wb["Summary"]

    run_start = ws.cell(row=2, column=1).value
    last_update = ws.cell(row=2, column=2).value
    start_bankroll = to_float(ws.cell(row=2, column=3).value, 0.0)
    trades = int(to_float(ws.cell(row=2, column=4).value, 0.0))
    realized_pnl = to_float(ws.cell(row=2, column=5).value, 0.0)
    realized_gains = to_float(ws.cell(row=2, column=6).value, 0.0)
    realized_losses = to_float(ws.cell(row=2, column=7).value, 0.0)
    unsold_value = to_float(ws.cell(row=2, column=8).value, 0.0)
    open_notional = to_float(ws.cell(row=2, column=9).value, 0.0)
    ending_equity = to_float(ws.cell(row=2, column=10).value, 0.0)
    roi = to_float(ws.cell(row=2, column=11).value, 0.0)

    print(f"Workbook: {workbook}", flush=True)
    print(f"Run Start: {run_start}", flush=True)
    print(f"Last Update: {last_update}", flush=True)
    print(f"Trades: {trades}", flush=True)
    print(f"Starting Bankroll: {start_bankroll:.2f}", flush=True)
    print(f"Realized PnL: {realized_pnl:.2f}", flush=True)
    print(f"Realized Gains: {realized_gains:.2f}", flush=True)
    print(f"Realized Losses: {realized_losses:.2f}", flush=True)
    print(f"Unsold Value: {unsold_value:.2f}", flush=True)
    print(f"Open Notional: {open_notional:.2f}", flush=True)
    print(f"Ending Equity: {ending_equity:.2f}", flush=True)
    print(f"ROI: {roi:.2f}%", flush=True)

    if roi <= warn_roi:
        print(f"WARNING: ROI {roi:.2f}% <= {warn_roi:.2f}%", flush=True)

    print("=" * 72, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Hourly workbook reporter")
    parser.add_argument("--workbook", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--warn-roi", type=float, default=-25.0)
    parser.add_argument("--interval-seconds", type=int, default=3600)
    args = parser.parse_args()

    interval_seconds = max(60, args.interval_seconds)

    print(
        f"Starting hourly reporter for profile {args.label} (interval={interval_seconds}s)",
        flush=True,
    )

    while True:
        try:
            print_report(args.workbook, args.label, args.warn_roi)
        except Exception as exc:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print("=" * 72, flush=True)
            print(f"HOURLY TAIL METRICS | PROFILE {args.label} | {now}", flush=True)
            print("-" * 72, flush=True)
            print(f"Reporter error: {exc}", flush=True)
            print("=" * 72, flush=True)

        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()

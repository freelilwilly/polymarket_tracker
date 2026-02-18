import argparse
import asyncio
import os
import webbrowser
from contextlib import contextmanager
from typing import Dict, List, Tuple

from main_v2 import PolymarketTopUsersLiveBot


@contextmanager
def temporary_env(updates: Dict[str, str]):
    original: Dict[str, str] = {}
    missing_keys: List[str] = []

    for key, value in updates.items():
        if key in os.environ:
            original[key] = os.environ[key]
        else:
            missing_keys.append(key)
        os.environ[key] = value

    try:
        yield
    finally:
        for key in updates:
            if key in original:
                os.environ[key] = original[key]
            elif key in missing_keys and key in os.environ:
                del os.environ[key]


async def get_selected_wallets_for_profile(profile: str) -> Tuple[str, List[Dict[str, str]]]:
    if profile == "80":
        env = {
            "V2_MIN_WIN_RATE": "80",
            "V2_TOP_USERS": "10",
            "V2_CANDIDATE_LIMIT": "300",
            "DRY_RUN": "true",
            "V2_EXCEL_MODE": "false",
        }
    elif profile == "75":
        env = {
            "V2_MIN_WIN_RATE": "75",
            "V2_TOP_USERS": "99999",
            "V2_CANDIDATE_LIMIT": "300",
            "DRY_RUN": "true",
            "V2_EXCEL_MODE": "false",
        }
    else:
        raise ValueError(f"Unsupported profile: {profile}")

    with temporary_env(env):
        bot = PolymarketTopUsersLiveBot()
        await bot.initialize()
        try:
            selected = await bot.select_top_users()
        finally:
            await bot.shutdown()

    rows: List[Dict[str, str]] = []
    for item in selected:
        wallet = str(item.get("wallet") or "").strip()
        display_name = str(item.get("display_name") or wallet).strip()
        if wallet:
            rows.append({"wallet": wallet, "display_name": display_name})

    return profile, rows


def build_urls(rows: List[Dict[str, str]], url_template: str) -> List[str]:
    urls: List[str] = []
    seen = set()
    for item in rows:
        wallet = item["wallet"]
        url = url_template.format(wallet=wallet)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open Polymarket Analytics pages for currently selected accounts"
    )
    parser.add_argument(
        "--profile",
        choices=["80", "75", "both"],
        default="both",
        help="Which selection profile to use",
    )
    parser.add_argument(
        "--url-template",
        default=os.getenv("ANALYTICS_TRADER_URL_TEMPLATE", "https://polymarketanalytics.com/traders/{wallet}"),
        help="Trader page URL template. Use {wallet} as placeholder.",
    )
    parser.add_argument(
        "--max-tabs",
        type=int,
        default=40,
        help="Maximum number of browser tabs to open",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print URLs, do not open browser tabs",
    )
    args = parser.parse_args()

    profiles = ["80", "75"] if args.profile == "both" else [args.profile]

    combined_rows: List[Dict[str, str]] = []
    for profile in profiles:
        profile_name, rows = await get_selected_wallets_for_profile(profile)
        print(f"PROFILE={profile_name} SELECTED={len(rows)}")
        for item in rows:
            print(f"  {item['display_name']} | {item['wallet']}")
        combined_rows.extend(rows)

    unique_urls = build_urls(combined_rows, args.url_template)

    print(f"\nURL_TEMPLATE={args.url_template}")
    print(f"UNIQUE_URLS={len(unique_urls)}")

    if args.max_tabs > 0:
        unique_urls = unique_urls[: args.max_tabs]

    for index, url in enumerate(unique_urls, start=1):
        print(f"[{index}] {url}")

    if args.dry_run:
        print("\nDRY_RUN enabled; no tabs opened.")
        return

    for url in unique_urls:
        webbrowser.open_new_tab(url)

    print(f"\nOpened {len(unique_urls)} tab(s).")


if __name__ == "__main__":
    asyncio.run(main())

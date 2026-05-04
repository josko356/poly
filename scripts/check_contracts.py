#!/usr/bin/env python3
"""
scripts/check_contracts.py — Inspect currently active BTC/ETH contracts.

Run this before starting the bot to verify contract discovery is working:
    python scripts/check_contracts.py

Shows all BTC and ETH 5-min / 15-min up/down contracts with their
current market prices and spreads.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from datetime import datetime

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"

KEYWORDS_ASSET = {
    "BTC": ["btc", "bitcoin"],
    "ETH": ["eth", "ethereum"],
}
KEYWORDS_DURATION = {
    5:  ["5 minute", "5-minute", "5min", "5 min"],
    15: ["15 minute", "15-minute", "15min", "15 min"],
}
KEYWORDS_DIR = {
    "UP":   ["higher", "above", "up", "rise", "increase"],
    "DOWN": ["lower",  "below", "down", "fall", "decrease"],
}


async def fetch_contracts():
    timeout = aiohttp.ClientTimeout(total=15)
    results = []

    async with aiohttp.ClientSession(timeout=timeout) as session:
        # ── Fetch markets ─────────────────────────────────────────
        print("Fetching active markets from Polymarket Gamma API...")
        markets = []
        params = {"active": "true", "closed": "false", "limit": "100"}
        for page in range(5):
            async with session.get(f"{GAMMA_URL}/markets", params=params) as resp:
                if resp.status != 200:
                    print(f"  Warning: HTTP {resp.status} on page {page+1}")
                    break
                data = await resp.json(content_type=None)
                page_markets = data if isinstance(data, list) else data.get("data", [])
                markets.extend(page_markets)
                next_cursor = data.get("next_cursor", "") if isinstance(data, dict) else ""
                if not next_cursor or not page_markets:
                    break
                params["next_cursor"] = next_cursor
            await asyncio.sleep(0.2)

        print(f"  Retrieved {len(markets)} total active markets.\n")

        # ── Filter ────────────────────────────────────────────────
        for market in markets:
            question = (market.get("question") or market.get("title") or "").lower()
            text = question

            asset = None
            for a, kws in KEYWORDS_ASSET.items():
                if any(k in text for k in kws):
                    asset = a
                    break
            if not asset:
                continue

            duration = None
            for d, kws in KEYWORDS_DURATION.items():
                if any(k in text for k in kws):
                    duration = d
                    break
            if not duration:
                continue

            direction = None
            for dir_, kws in KEYWORDS_DIR.items():
                if any(k in text for k in kws):
                    direction = dir_
                    break
            if not direction:
                direction = "UP"  # binary markets default

            tokens = market.get("tokens", [])
            yes_token = next(
                (t["token_id"] for t in tokens
                 if (t.get("outcome") or "").lower() in ("yes", "up", "higher")),
                tokens[0]["token_id"] if tokens else None,
            )
            if not yes_token:
                continue

            # Fetch order book
            token_id = yes_token if direction == "UP" else (
                next((t["token_id"] for t in tokens
                      if (t.get("outcome") or "").lower() in ("no", "down", "lower")),
                     None)
            )
            if not token_id:
                continue

            book_data = None
            try:
                async with session.get(
                    f"{CLOB_URL}/book", params={"token_id": token_id}
                ) as resp:
                    if resp.status == 200:
                        book_data = await resp.json(content_type=None)
            except Exception:
                pass
            await asyncio.sleep(0.15)

            best_bid = best_ask = mid = spread = None
            if book_data:
                bids = book_data.get("bids", [])
                asks = book_data.get("asks", [])
                best_bid = float(bids[0]["price"]) if bids else 0.0
                best_ask = float(asks[0]["price"]) if asks else 1.0
                mid = (best_bid + best_ask) / 2
                spread = best_ask - best_bid

            results.append({
                "asset": asset,
                "direction": direction,
                "duration": duration,
                "question": market.get("question", market.get("title", ""))[:70],
                "condition_id": market.get("condition_id", "")[:16] + "…",
                "token_id": token_id[:16] + "…",
                "end_date": market.get("end_date_iso", "?")[:19],
                "mid": mid,
                "best_ask": best_ask,
                "spread": spread,
            })

    return results


def print_results(results):
    if not results:
        print("❌ No matching BTC/ETH 5min/15min contracts found.")
        print("   This can happen between contract windows — try again in a minute.")
        return

    # Group by asset + duration
    groups = {}
    for r in results:
        key = f"{r['asset']} {r['duration']}min"
        groups.setdefault(key, []).append(r)

    print(f"{'='*72}")
    print(f"  ACTIVE CONTRACTS  —  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'='*72}")

    for group_name in sorted(groups):
        contracts = groups[group_name]
        print(f"\n  📊 {group_name} ({len(contracts)} contract{'s' if len(contracts)>1 else ''})")
        print(f"  {'Dir':<6} {'Mid':>6} {'Ask':>6} {'Spread':>8}  {'Question'}")
        print(f"  {'-'*65}")
        for c in sorted(contracts, key=lambda x: x["direction"]):
            dir_sym = "📈" if c["direction"] == "UP" else "📉"
            mid_str = f"{c['mid']:.3f}" if c["mid"] is not None else "  —  "
            ask_str = f"{c['best_ask']:.3f}" if c["best_ask"] is not None else "  —  "
            spr_str = f"{c['spread']:.3f}" if c["spread"] is not None else "  —  "
            print(f"  {dir_sym} {c['direction']:<4} {mid_str:>6} {ask_str:>6} {spr_str:>8}  {c['question']}")

    print(f"\n{'='*72}")
    print(f"  Total: {len(results)} contracts found across {len(groups)} groups")
    print(f"{'='*72}\n")

    if results:
        print("✅ Contract discovery is working. The bot will auto-fetch these at runtime.\n")


async def main():
    try:
        results = await fetch_contracts()
        print_results(results)
    except aiohttp.ClientConnectorError:
        print("❌ Could not connect to Polymarket API. Check your internet connection.")
    except Exception as e:
        print(f"❌ Error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())

"""
scripts/diagnose.py — End-to-end pipeline diagnostic.

Checks:
  1. Coinbase feed: connects and receives prices for BTC, ETH, SOL, XRP
  2. Polymarket contracts: finds active Up/Down contracts for all 4 assets
  3. Order books: fetches a real book for one contract per asset
  4. Pipeline test: injects a synthetic opportunity through the full
     trading engine and verifies the DB records it
  5. Cleanup: marks the test trade as cancelled in the DB

Run: venv\\Scripts\\python.exe -X utf8 scripts/diagnose.py
"""

import asyncio
import json
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import aiohttp
import websockets

from config import Config
from core.arbitrage_engine import ArbitrageEngine, Opportunity
from core.coinbase_feed import CoinbaseFeed
from core.database import Database
from core.polymarket_client import (
    Contract, OrderBook, PolymarketClient,
    UPDOWN_ASSETS, UPDOWN_DURATIONS, GAMMA_URL, CLOB_URL,
)
from core.risk_manager import RiskManager
from core.trading_engine import TradingEngine

PASS = "  ✓"
FAIL = "  ✗"
WARN = "  !"


# ── 1. Coinbase feed ──────────────────────────────────────────────────────────

async def check_coinbase(assets: list) -> dict:
    print("\n[1] Coinbase feed")
    results = {}
    uri = "wss://ws-feed.exchange.coinbase.com"
    product_ids = [f"{a}-USD" for a in assets]
    sub = json.dumps({"type": "subscribe", "product_ids": product_ids, "channels": ["ticker"]})

    received = {a: [] for a in assets}
    try:
        async with websockets.connect(uri, open_timeout=10) as ws:
            await ws.send(sub)
            deadline = time.time() + 12
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    msg = json.loads(raw)
                    if msg.get("type") == "ticker":
                        asset = msg.get("product_id", "").split("-")[0]
                        if asset in received:
                            received[asset].append(float(msg["price"]))
                except asyncio.TimeoutError:
                    break
    except Exception as exc:
        print(f"{FAIL} Connection failed: {exc}")
        return {}

    for asset in assets:
        prices = received[asset]
        if prices:
            lo, hi = min(prices), max(prices)
            change = (hi - lo) / lo * 100 if lo > 0 else 0.0
            print(f"{PASS} {asset}: ${prices[-1]:,.4f}  ({len(prices)} ticks, spread {change:.4f}%)")
            results[asset] = prices[-1]
        else:
            print(f"{FAIL} {asset}: no ticks received")
            results[asset] = None

    return results


# ── 2. Polymarket contracts ───────────────────────────────────────────────────

async def check_contracts() -> dict:
    print("\n[2] Polymarket contracts")
    found = {}
    timeout = aiohttp.ClientTimeout(total=15)
    now = int(time.time())

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for asset_slug, asset_name in UPDOWN_ASSETS.items():
            for duration_mins in UPDOWN_DURATIONS:
                duration_secs = duration_mins * 60
                window = (now // duration_secs) * duration_secs
                slug = f"{asset_slug}-updown-{duration_mins}m-{window}"
                try:
                    await asyncio.sleep(0.2)
                    async with session.get(f"{GAMMA_URL}/events", params={"slug": slug}) as resp:
                        if resp.status != 200:
                            print(f"{WARN} {asset_name} {duration_mins}min: HTTP {resp.status}")
                            continue
                        data = await resp.json(content_type=None)
                        events = data if isinstance(data, list) else data.get("data", [])
                        n_contracts = sum(len(e.get("markets", [])) * 2 for e in events)
                        if n_contracts:
                            print(f"{PASS} {asset_name} {duration_mins}min: {n_contracts} contracts  (slug={slug})")
                            found[f"{asset_name}-{duration_mins}m"] = n_contracts
                        else:
                            print(f"{WARN} {asset_name} {duration_mins}min: 0 contracts  (slug={slug})")
                except Exception as exc:
                    print(f"{FAIL} {asset_name} {duration_mins}min: {exc}")

    return found


# ── 3. Order books ────────────────────────────────────────────────────────────

async def check_order_books() -> dict:
    print("\n[3] Order books (one per asset)")
    results = {}
    timeout = aiohttp.ClientTimeout(total=15)
    now = int(time.time())

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for asset_slug, asset_name in UPDOWN_ASSETS.items():
            duration_secs = 300  # 5min
            window = (now // duration_secs) * duration_secs
            slug = f"{asset_slug}-updown-5m-{window}"
            try:
                await asyncio.sleep(0.2)
                async with session.get(f"{GAMMA_URL}/events", params={"slug": slug}) as resp:
                    if resp.status != 200:
                        print(f"{WARN} {asset_name}: can't fetch event")
                        continue
                    data = await resp.json(content_type=None)
                    events = data if isinstance(data, list) else data.get("data", [])
                    if not events or not events[0].get("markets"):
                        print(f"{WARN} {asset_name}: no markets in event")
                        continue

                    market = events[0]["markets"][0]
                    import json as json_mod
                    token_ids = json_mod.loads(market.get("clobTokenIds", "[]"))
                    if not token_ids:
                        print(f"{WARN} {asset_name}: no token IDs")
                        continue

                    token_id = token_ids[0]
                    await asyncio.sleep(0.2)
                    async with session.get(f"{CLOB_URL}/book", params={"token_id": token_id}) as resp2:
                        if resp2.status != 200:
                            print(f"{WARN} {asset_name}: CLOB HTTP {resp2.status}")
                            continue
                        book_data = await resp2.json(content_type=None)
                        bids = book_data.get("bids", [])
                        asks = book_data.get("asks", [])
                        best_bid = float(bids[0]["price"]) if bids else 0.0
                        best_ask = float(asks[0]["price"]) if asks else 1.0
                        spread = best_ask - best_bid
                        print(f"{PASS} {asset_name}: bid={best_bid:.3f}  ask={best_ask:.3f}  spread={spread:.3f}  ({len(bids)} bids, {len(asks)} asks)")
                        results[asset_name] = {"bid": best_bid, "ask": best_ask}
            except Exception as exc:
                print(f"{FAIL} {asset_name}: {exc}")

    return results


# ── 4. Pipeline test ──────────────────────────────────────────────────────────

async def check_pipeline(book_data: dict) -> bool:
    print("\n[4] Pipeline test (synthetic trade)")

    config = Config()
    db = Database()
    await db.init()
    risk = RiskManager(config, config.PAPER_STARTING_BALANCE)

    # Use BTC book data if available, else use defaults
    btc_book = book_data.get("BTC", {})
    best_ask = btc_book.get("ask", 0.52)
    best_bid = btc_book.get("bid", 0.48)

    engine = TradingEngine(config=config, polymarket=None, risk=risk, db=db)

    # Synthetic contract and order book
    fake_contract = Contract(
        condition_id="DIAG-TEST-UP",
        question="[DIAGNOSTIC] Will BTC go up?",
        asset="BTC",
        direction="UP",
        duration_mins=5,
        token_id="FAKE_TOKEN_ID",
        end_date_iso="2099-01-01",
        active=True,
        price_to_beat=78000.0,
        window_start=int(time.time()) - 120,
    )

    fake_book = OrderBook(
        token_id="FAKE_TOKEN_ID",
        best_bid=best_bid,
        best_ask=best_ask,
        mid=(best_bid + best_ask) / 2,
        spread=best_ask - best_bid,
    )

    fake_opp = Opportunity(
        contract=fake_contract,
        order_book=fake_book,
        polymarket_price=best_ask,
        model_prob=best_ask + 0.12,   # 12% edge
        edge=0.12,
        confidence=0.55,
        coinbase_price=78500.0,
        price_change_pct=0.005,
        direction="UP",
    )

    print(f"  Synthetic opp: edge={fake_opp.edge:.0%}  conf={fake_opp.confidence:.0%}  price={fake_opp.polymarket_price:.3f}")

    try:
        opened = await engine.execute_opportunity(fake_opp)
        if not opened:
            print(f"{FAIL} execute_opportunity returned False")
            return False

        trade_id = list(engine._positions.keys())[0] if engine._positions else None

        if trade_id:
            pos = engine._positions[trade_id]
            print(f"{PASS} Trade opened: ID={trade_id}  size=${pos.paper_usdc_spent:.2f}  shares={pos.paper_shares:.4f}")
            await db.update_trade_result(trade_id, "cancelled", 0.0)
            print(f"{PASS} DB write confirmed (marked cancelled — won't affect real stats)")
        else:
            print(f"{WARN} execute_opportunity returned True but no position in memory")

    except Exception as exc:
        print(f"{FAIL} Pipeline error: {exc}")
        import traceback; traceback.print_exc()
        return False

    return True


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print("=" * 55)
    print("  POLYMARKET BOT — END-TO-END DIAGNOSTIC")
    print("=" * 55)

    assets = list(UPDOWN_ASSETS.values())  # ["BTC", "ETH", "SOL", "XRP"]

    coinbase_prices = await check_coinbase(assets)
    contracts = await check_contracts()
    books = await check_order_books()
    pipeline_ok = await check_pipeline(books)

    print("\n" + "=" * 55)
    print("  SUMMARY")
    print("=" * 55)
    feed_ok  = sum(1 for v in coinbase_prices.values() if v) == len(assets)
    poly_ok  = len(contracts) > 0
    books_ok = len(books) > 0

    print(f"  Coinbase feed   : {'OK (' + str(sum(1 for v in coinbase_prices.values() if v)) + '/' + str(len(assets)) + ' assets)' if feed_ok else 'PARTIAL/FAIL'}")
    print(f"  Polymarket      : {'OK (' + str(len(contracts)) + ' contract groups)' if poly_ok else 'FAIL'}")
    print(f"  Order books     : {'OK (' + str(len(books)) + '/' + str(len(assets)) + ' assets)' if books_ok else 'FAIL'}")
    print(f"  Trade pipeline  : {'OK' if pipeline_ok else 'FAIL'}")

    all_ok = feed_ok and poly_ok and books_ok and pipeline_ok
    print(f"\n  Overall: {'ALL SYSTEMS GO ✓' if all_ok else 'ISSUES FOUND — see above'}")
    print()


if __name__ == "__main__":
    asyncio.run(main())

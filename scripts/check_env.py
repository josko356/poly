"""
scripts/check_env.py -- Validate .env before going live.

Usage:
    python scripts/check_env.py

Checks:
  1. All required keys present and non-empty
  2. Format validation (private key, address, token, chat_id)
  3. Private key -> address derivation matches POLYGON_ADDRESS
  4. Live: Telegram getMe + test message
  5. Live: Polygon USDC balance via RPC
  6. Live: Coinbase WebSocket reachability (optional)
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

import aiohttp

PASS  = "[  OK  ]"
FAIL  = "[ FAIL ]"
WARN  = "[ WARN ]"
INFO  = "[ INFO ]"

USDC_CONTRACT = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
POLYGON_RPCS  = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://rpc-mainnet.matic.quiknode.pro",
]


def _e(key: str) -> str:
    return os.getenv(key, "").strip()


# -- Format checks -------------------------------------------------------------

def check_formats() -> bool:
    print("\n-- Format & presence -----------------------------------------")
    all_ok = True

    # Live trading flags
    flags = {
        "LIVE_TRADING_ENABLED":           _e("LIVE_TRADING_ENABLED"),
        "LIVE_TRADING_CONFIRMED":         _e("LIVE_TRADING_CONFIRMED"),
        "LIVE_TRADING_RISK_ACKNOWLEDGED": _e("LIVE_TRADING_RISK_ACKNOWLEDGED"),
    }
    for k, v in flags.items():
        if v.lower() == "true":
            print(f"{PASS} {k} = true")
        else:
            print(f"{WARN} {k} = '{v}'  (not 'true' -- live trading will be disabled)")

    # Polygon private key
    pk = _e("POLYGON_PRIVATE_KEY")
    pk_clean = pk[2:] if pk.startswith("0x") else pk
    if not pk:
        print(f"{FAIL} POLYGON_PRIVATE_KEY  missing")
        all_ok = False
    elif len(pk_clean) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", pk_clean):
        print(f"{FAIL} POLYGON_PRIVATE_KEY  wrong format (expected 64 hex chars)")
        all_ok = False
    else:
        print(f"{PASS} POLYGON_PRIVATE_KEY  format OK ({pk_clean[:6]}...{pk_clean[-4:]})")

    # Polygon address
    addr = _e("POLYGON_ADDRESS")
    if not addr:
        print(f"{FAIL} POLYGON_ADDRESS  missing")
        all_ok = False
    elif not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr):
        print(f"{FAIL} POLYGON_ADDRESS  wrong format (expected 0x + 40 hex chars)")
        all_ok = False
    else:
        print(f"{PASS} POLYGON_ADDRESS  format OK ({addr[:8]}...{addr[-4:]})")

    # Telegram token
    tg_token = _e("TELEGRAM_BOT_TOKEN")
    if not tg_token:
        print(f"{WARN} TELEGRAM_BOT_TOKEN  missing (Telegram alerts disabled)")
    elif not re.fullmatch(r"\d+:[A-Za-z0-9_-]{35,}", tg_token):
        print(f"{FAIL} TELEGRAM_BOT_TOKEN  wrong format (expected 'digits:35+chars')")
        all_ok = False
    else:
        print(f"{PASS} TELEGRAM_BOT_TOKEN  format OK")

    # Telegram chat_id
    tg_chat = _e("TELEGRAM_CHAT_ID")
    if not tg_chat:
        print(f"{WARN} TELEGRAM_CHAT_ID  missing (Telegram alerts disabled)")
    elif not re.fullmatch(r"-?\d+", tg_chat):
        print(f"{FAIL} TELEGRAM_CHAT_ID  wrong format (expected a number, e.g. 123456789)")
        all_ok = False
    else:
        print(f"{PASS} TELEGRAM_CHAT_ID  format OK ({tg_chat})")

    # Coinbase (optional)
    cb_key = _e("COINBASE_API_KEY")
    cb_sec = _e("COINBASE_API_SECRET")
    if cb_key and cb_sec:
        print(f"{PASS} COINBASE_API_KEY / SECRET  present (authenticated feed)")
    else:
        print(f"{INFO} COINBASE_API_KEY / SECRET  not set (public feed -- works fine)")

    return all_ok


# -- Key -> address derivation --------------------------------------------------

def check_key_matches_address() -> bool:
    print("\n-- Private key -> address derivation -------------------------")
    pk    = _e("POLYGON_PRIVATE_KEY")
    addr  = _e("POLYGON_ADDRESS")
    if not pk or not addr:
        print(f"{WARN} Skipping -- POLYGON_PRIVATE_KEY or POLYGON_ADDRESS missing")
        return True

    try:
        from eth_account import Account
        derived = Account.from_key(pk).address
        if derived.lower() == addr.lower():
            print(f"{PASS} Private key matches address  ({derived[:8]}...{derived[-4:]})")
            return True
        else:
            print(f"{FAIL} Private key does NOT match address!")
            print(f"       Key derives:  {derived}")
            print(f"       .env has:     {addr}")
            return False
    except ImportError:
        print(f"{WARN} eth_account not installed -- skipping key/address match check")
        print(f"       Install: pip install eth-account")
        return True
    except Exception as exc:
        print(f"{FAIL} Could not derive address from key: {exc}")
        return False


# -- Network checks ------------------------------------------------------------

async def check_telegram() -> bool:
    print("\n-- Telegram --------------------------------------------------")
    token   = _e("TELEGRAM_BOT_TOKEN")
    chat_id = _e("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print(f"{WARN} Skipping -- token or chat_id not set")
        return True

    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        # 1. getMe
        try:
            async with s.get(f"https://api.telegram.org/bot{token}/getMe") as r:
                data = await r.json()
                if r.status == 200 and data.get("ok"):
                    name = data["result"]["username"]
                    print(f"{PASS} Token valid -- bot is @{name}")
                else:
                    print(f"{FAIL} getMe failed: {data.get('description', data)}")
                    return False
        except Exception as exc:
            print(f"{FAIL} Could not reach Telegram API: {exc}")
            return False

        # 2. Send test message
        payload = {
            "chat_id": chat_id,
            "text": "✅ <b>check_env.py</b> -- Telegram connection verified.",
            "parse_mode": "HTML",
        }
        try:
            async with s.post(
                f"https://api.telegram.org/bot{token}/sendMessage", json=payload
            ) as r:
                data = await r.json()
                if r.status == 200:
                    print(f"{PASS} Test message sent to chat {chat_id}")
                    return True
                desc = data.get("description", str(data))
                if "bot was blocked" in desc or "chat not found" in desc or "Forbidden" in desc:
                    print(f"{FAIL} Message failed: {desc}")
                    print(f"       -> Did you send /start to @{name} in Telegram?")
                else:
                    print(f"{FAIL} Message failed ({r.status}): {desc}")
                return False
        except Exception as exc:
            print(f"{FAIL} Send failed: {exc}")
            return False


async def check_polygon_balance() -> bool:
    print("\n-- Polygon USDC balance --------------------------------------")
    addr = _e("POLYGON_ADDRESS")
    if not addr:
        print(f"{WARN} Skipping -- POLYGON_ADDRESS not set")
        return True

    padded  = addr[2:].lower().zfill(64)
    data    = f"0x70a08231{padded}"
    payload = {"jsonrpc": "2.0", "method": "eth_call",
               "params": [{"to": USDC_CONTRACT, "data": data}, "latest"], "id": 1}

    timeout = aiohttp.ClientTimeout(total=5)
    for rpc in POLYGON_RPCS:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(rpc, json=payload) as r:
                    if r.status != 200:
                        continue
                    result = (await r.json(content_type=None)).get("result", "")
                    if result and result != "0x":
                        balance = int(result, 16) / 1_000_000
                        if balance == 0:
                            print(f"{FAIL} USDC balance: $0.00 -- deposit USDC before live trading")
                            return False
                        else:
                            print(f"{PASS} USDC balance: ${balance:.2f} on {addr[:8]}...{addr[-4:]}")
                            return True
        except Exception:
            continue

    print(f"{WARN} All Polygon RPCs unreachable -- could not read balance")
    return True  # not a blocking failure


async def check_coinbase() -> bool:
    print("\n-- Coinbase WebSocket ----------------------------------------")
    import websockets
    url = "wss://ws-feed.exchange.coinbase.com"
    sub = '{"type":"subscribe","product_ids":["BTC-USD"],"channels":["ticker"]}'
    try:
        async with websockets.connect(url, open_timeout=8) as ws:
            await ws.send(sub)
            for _ in range(5):
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                import json
                d = json.loads(msg)
                if d.get("type") == "ticker":
                    price = d.get("price", "?")
                    print(f"{PASS} Coinbase feed live -- BTC-USD: ${float(price):,.2f}")
                    return True
        print(f"{WARN} Connected but no ticker in 5 messages")
        return True
    except Exception as exc:
        print(f"{FAIL} Coinbase WS unreachable: {exc}")
        return False


# -- Entry point ---------------------------------------------------------------

async def main():
    print("=" * 62)
    print("  POLYMARKET BOT -- .env validation")
    print("=" * 62)

    results = []
    results.append(check_formats())
    results.append(check_key_matches_address())
    results.append(await check_telegram())
    results.append(await check_polygon_balance())
    results.append(await check_coinbase())

    print("\n" + "=" * 62)
    if all(results):
        print("  ALL CHECKS PASSED -- ready for live trading.")
    else:
        failed = results.count(False)
        print(f"  {failed} CHECK(S) FAILED -- fix the above before going live.")
    print("=" * 62 + "\n")


if __name__ == "__main__":
    asyncio.run(main())

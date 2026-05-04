import asyncio
import aiohttp
import json
import time

async def check():
    timeout = aiohttp.ClientTimeout(total=10)
    headers = {"User-Agent": "Mozilla/5.0"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:

        now = int(time.time())
        window = (now // 300) * 300

        # Get full event structure for current BTC 5m window
        async with session.get("https://gamma-api.polymarket.com/events", params={"slug": f"btc-updown-5m-{window}"}) as resp:
            event = (await resp.json(content_type=None))[0]

        print("=== EVENT ===")
        print(json.dumps({k: event[k] for k in event if k not in ["description"]}, default=str, indent=2)[:2000])

        # Get full market data
        async with session.get("https://gamma-api.polymarket.com/markets", params={"slug": f"btc-updown-5m-{window}"}) as resp:
            market = await resp.json(content_type=None)
            if isinstance(market, list):
                market = market[0]

        print("\n=== MARKET ===")
        print(json.dumps(market, default=str, indent=2)[:3000])

        # Try to find order book via condition_id
        cid = market.get("conditionId", "")
        token_ids = [t.get("token_id","") for t in market.get("tokens", [])]
        print(f"\nCondition ID: {cid}")
        print(f"Token IDs: {token_ids}")

        # Try CLOB book endpoint
        if token_ids:
            async with session.get("https://clob.polymarket.com/book", params={"token_id": token_ids[0]}) as resp:
                print(f"\nCLOB book for token[0]: status={resp.status}")
                if resp.status == 200:
                    book = await resp.json(content_type=None)
                    print(json.dumps(book, default=str)[:300])

asyncio.run(check())

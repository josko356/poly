import asyncio, sys, json
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

async def test():
    import aiohttp, websockets

    print("=== CLOB WS path search on ws-subscriptions-clob.polymarket.com ===")
    paths = ["/", "/ws", "/ws/", "/subscribe", "/v1/ws", "/market", "/books"]
    for path in paths:
        url = "wss://ws-subscriptions-clob.polymarket.com" + path
        try:
            async with websockets.connect(url, open_timeout=4) as ws:
                print(f"  CONNECTED: {url}")
                await ws.close()
        except Exception as e:
            msg = str(e)[:70]
            print(f"  {url}: {msg}")

asyncio.run(test())

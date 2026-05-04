"""
core/chainlink_feed.py — Chainlink oracle price reader via Polygon RPC.

Polymarket settles contracts using Chainlink oracle prices, NOT Coinbase prices.
Reading oracle prices directly means:
  1. price_to_beat is set from the exact oracle price at window open
  2. The probability model uses the actual settlement reference, not a proxy
  3. Divergence between Coinbase and Chainlink is detected and handled
"""

import asyncio
import logging
import time
from typing import Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",   # free, no key needed
    "https://1rpc.io/matic",                     # free, privacy-focused
    "https://rpc-mainnet.matic.quiknode.pro",    # free public tier
]

CHAINLINK_FEEDS = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "SOL": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
    "XRP": "0x785ba89291f676b5386652eB12b30cF361020694",
}

LATEST_ROUND_DATA = "0xfeaf968c"   # latestRoundData() function selector
POLL_INTERVAL     = 5.0            # seconds between full poll cycles
STALENESS_LIMIT   = 30.0           # seconds before a cached price is considered stale
SANITY_TOLERANCE  = 0.25           # max allowed divergence from Coinbase (25%)
TIMEOUT = aiohttp.ClientTimeout(total=4)


class ChainlinkFeed:
    """
    Polls Chainlink aggregator contracts on Polygon for the oracle prices
    that Polymarket uses to settle UP/DOWN contracts.

    Usage:
        feed = ChainlinkFeed(config.ASSETS)
        await feed.start()
        price = feed.get_price("BTC")          # returns float or None
        price = feed.get_validated("BTC", coinbase_price=95000)  # sanity-checked
    """

    def __init__(self, assets: list):
        self._assets = [a.split("-")[0] for a in assets]
        self._prices: Dict[str, float] = {}
        self._timestamps: Dict[str, float] = {}
        self._session: Optional[aiohttp.ClientSession] = None
        self._task: Optional[asyncio.Task] = None
        self._rpc_index = 0

    async def start(self):
        self._session = aiohttp.ClientSession(timeout=TIMEOUT)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("ChainlinkFeed started for: %s", self._assets)

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._session:
            await self._session.close()

    # ── Public API ────────────────────────────────────────────────

    def get_price(self, asset: str) -> Optional[float]:
        """Latest oracle price, or None if stale/unavailable."""
        if time.time() - self._timestamps.get(asset, 0) > STALENESS_LIMIT:
            return None
        return self._prices.get(asset)

    def get_validated(self, asset: str, coinbase_price: float) -> Optional[float]:
        """
        Returns oracle price only if it's within SANITY_TOLERANCE of coinbase_price.
        Falls back to None if prices diverge too much (likely wrong address or stale oracle).
        """
        oracle = self.get_price(asset)
        if oracle is None or coinbase_price <= 0:
            return None
        ratio = abs(oracle - coinbase_price) / coinbase_price
        if ratio > SANITY_TOLERANCE:
            logger.warning(
                "Chainlink/Coinbase divergence too large for %s: oracle=%.2f cb=%.2f (%.1f%%)",
                asset, oracle, coinbase_price, ratio * 100,
            )
            return None
        return oracle

    # ── Background poll ───────────────────────────────────────────

    async def _poll_loop(self):
        await asyncio.sleep(2.0)  # let session stabilise
        while True:
            for asset in self._assets:
                if asset not in CHAINLINK_FEEDS:
                    continue
                price = await self._fetch_price(asset)
                if price is not None and price > 0:
                    old = self._prices.get(asset)
                    self._prices[asset] = price
                    self._timestamps[asset] = time.time()
                    if old is None:
                        logger.info("Chainlink %s/USD oracle: $%.4f", asset, price)
                    else:
                        logger.debug("Chainlink %s/USD: $%.4f", asset, price)
            await asyncio.sleep(POLL_INTERVAL)

    async def _fetch_price(self, asset: str) -> Optional[float]:
        contract = CHAINLINK_FEEDS[asset]
        rpc = POLYGON_RPCS[self._rpc_index % len(POLYGON_RPCS)]
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": contract, "data": LATEST_ROUND_DATA}, "latest"],
            "id": 1,
        }
        try:
            async with self._session.post(rpc, json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                result = data.get("result", "")
                if not result or result == "0x":
                    return None
                # Response: 5 × 32-byte fields (plus 0x prefix = 322 hex chars)
                # Fields: (roundId, answer, startedAt, updatedAt, answeredInRound)
                clean = result[2:]
                if len(clean) < 320:
                    return None
                answer_hex = clean[64:128]   # second field = answer
                answer = int(answer_hex, 16)
                # int256 two's complement: negative prices are theoretically impossible
                # but a buggy oracle can return them — treat as invalid
                if answer > (1 << 255):
                    answer -= (1 << 256)
                if answer <= 0:
                    return None
                # Chainlink USD feeds use 8 decimal places
                return answer / 1e8
        except Exception as exc:
            logger.debug("Chainlink RPC error %s (%s): %s", asset, rpc, exc)
            self._rpc_index += 1  # rotate RPC on failure
            return None

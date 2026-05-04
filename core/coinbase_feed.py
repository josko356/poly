"""
core/coinbase_feed.py — Coinbase Advanced Trade WebSocket feed.

Autentikacija: HMAC-SHA256 s Coinbase Developer Platform ključem.
  - API Key ID  = path oblika organizations/.../apiKeys/...
  - API Secret  = base64 string s CDP portala

Ako nema ključa → javni WebSocket (radi i bez ključa).
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import statistics
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

logger = logging.getLogger(__name__)

WS_URL_PUBLIC = "wss://ws-feed.exchange.coinbase.com"   # Exchange/Pro endpoint (CDP deprecated)
WS_URL_USER   = "wss://ws-feed.exchange.coinbase.com"

RECONNECT_DELAY     = 3
MAX_RECONNECT_DELAY = 60
HISTORY_SECONDS     = 120

BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
BINANCE_SYMBOLS = {"BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL", "XRPUSDT": "XRP"}


@dataclass
class PriceTick:
    asset: str
    price: float
    timestamp: float
    volume_24h: float = 0.0
    bid: float = 0.0
    ask: float = 0.0


class PriceHistory:
    def __init__(self, max_seconds: int = HISTORY_SECONDS):
        self.max_seconds = max_seconds
        self._ticks: deque = deque()

    def add(self, tick: PriceTick):
        self._ticks.append(tick)
        self._purge()

    def _purge(self):
        cutoff = time.time() - self.max_seconds
        while self._ticks and self._ticks[0].timestamp < cutoff:
            self._ticks.popleft()

    def latest(self) -> Optional[PriceTick]:
        return self._ticks[-1] if self._ticks else None

    def price_change_pct(self, lookback_seconds: int = 10) -> float:
        now = time.time()
        old = [t for t in self._ticks if t.timestamp <= now - lookback_seconds]
        latest = self.latest()
        if not old or not latest or old[-1].price == 0:
            return 0.0
        return (latest.price - old[-1].price) / old[-1].price

    def realized_vol_annual(self, lookback_secs: int = 120) -> float:
        """Annualizirana realizirana volatilnost iz nedavnih log-povrata. Fallback=0.80."""
        now = time.time()
        ticks = [t for t in self._ticks if t.timestamp >= now - lookback_secs]
        if len(ticks) < 6:
            return 0.80
        prices = [t.price for t in ticks]
        log_returns = [
            math.log(prices[i + 1] / prices[i])
            for i in range(len(prices) - 1)
            if prices[i] > 0
        ]
        if len(log_returns) < 5:
            return 0.80
        dt_secs = lookback_secs / len(log_returns)
        per_year = 365.25 * 24 * 3600 / dt_secs
        return statistics.stdev(log_returns) * math.sqrt(per_year)


def _build_signature(
    api_key: str,
    api_secret_b64: str,
    channel: str,
    product_ids: list,
) -> tuple:
    """
    HMAC-SHA256 signature za Coinbase CDP WebSocket auth.

    Format:
      message   = timestamp + channel + product_ids_comma_joined
      key       = base64_decode(api_secret)
      signature = HMAC-SHA256(key, message).hexdigest()
    """
    timestamp = str(int(time.time()))
    message = timestamp + channel + ",".join(product_ids)

    try:
        secret_bytes = base64.b64decode(api_secret_b64)
    except Exception:
        secret_bytes = api_secret_b64.encode("utf-8")

    sig = hmac.new(
        secret_bytes,
        message.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    return timestamp, sig


class CoinbaseFeed:
    """
    Async WebSocket client za Coinbase Advanced Trade.
    Autentikacija: HMAC-SHA256 (CDP Developer Platform format).
    Auto-reconnect s exponential backoff.
    """

    def __init__(self, config, on_tick: Optional[Callable] = None):
        self.config = config
        self.on_tick = on_tick
        self._history: Dict[str, PriceHistory] = {
            a.split("-")[0]: PriceHistory() for a in config.ASSETS
        }
        self._running = False
        self._connected = False
        self._task: Optional[asyncio.Task] = None
        self._binance_task: Optional[asyncio.Task] = None

    def latest(self, asset: str) -> Optional[PriceTick]:
        h = self._history.get(asset)
        return h.latest() if h else None

    def price_change_pct(self, asset: str, lookback_seconds: int = 10) -> float:
        h = self._history.get(asset)
        return h.price_change_pct(lookback_seconds) if h else 0.0

    def is_fresh(self, asset: str, max_age: float = 5.0) -> bool:
        tick = self.latest(asset)
        return tick is not None and (time.time() - tick.timestamp) < max_age

    def realized_vol_annual(self, asset: str, lookback_secs: int = 120) -> float:
        h = self._history.get(asset)
        return h.realized_vol_annual(lookback_secs) if h else 0.80

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def start(self):
        self._running = True
        mode = "HMAC-SHA256" if self.config.coinbase_auth_enabled else "public (bez kljuca)"
        logger.info("Coinbase feed (%s) za: %s", mode, self.config.ASSETS)
        self._task = asyncio.create_task(self._connect_loop())
        self._binance_task: Optional[asyncio.Task] = asyncio.create_task(self._binance_loop())

    async def stop(self):
        self._running = False
        self._connected = False
        if self._task:
            self._task.cancel()
        if self._binance_task:
            self._binance_task.cancel()

    async def _connect_loop(self):
        delay = RECONNECT_DELAY
        while self._running:
            try:
                await self._run_ws()
                delay = RECONNECT_DELAY
            except (ConnectionClosedError, ConnectionClosedOK):
                logger.warning("Coinbase WS zatvoren, reconnect za %ds...", delay)
            except Exception as exc:
                logger.error("Coinbase WS greska: %s, retry za %ds...", exc, delay)
            finally:
                self._connected = False
            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    async def _run_ws(self):
        # Coinbase Exchange/Pro WebSocket (CDP advanced-trade-api endpoint je zastario)
        subscribe_msg = {
            "type": "subscribe",
            "product_ids": self.config.ASSETS,
            "channels": ["ticker"],
        }

        async with websockets.connect(
            WS_URL_PUBLIC, ping_interval=20, ping_timeout=10, close_timeout=5,
        ) as ws:
            await ws.send(json.dumps(subscribe_msg))
            self._connected = True
            logger.info("Spojen: Coinbase Exchange WebSocket (public feed)")

            async for raw in ws:
                if not self._running:
                    break
                try:
                    self._handle_message(json.loads(raw))
                except Exception as exc:
                    logger.debug("Parse error: %s", exc)

    async def _binance_loop(self):
        """Binance WebSocket — okida 50-200ms prije Coinbasea, daje raniji signal."""
        delay = RECONNECT_DELAY
        while self._running:
            try:
                streams = "/".join(f"{s.lower()}@aggTrade" for s in BINANCE_SYMBOLS)
                url = f"{BINANCE_WS_URL}?streams={streams}"
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    logger.info("Spojen: Binance WebSocket (secondary feed)")
                    delay = RECONNECT_DELAY
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            envelope = json.loads(raw)
                            data = envelope.get("data", {})
                            if data.get("e") != "aggTrade":
                                continue
                            symbol = data.get("s", "")
                            asset = BINANCE_SYMBOLS.get(symbol)
                            if not asset or asset not in self._history:
                                continue
                            price = float(data["p"])
                            tick = PriceTick(
                                asset=asset, price=price, timestamp=time.time(),
                                volume_24h=0.0, bid=price, ask=price,
                            )
                            self._history[asset].add(tick)
                            if self.on_tick:
                                asyncio.create_task(self._fire(tick))
                        except Exception:
                            pass
            except (ConnectionClosedError, ConnectionClosedOK):
                logger.debug("Binance WS closed, reconnect za %ds...", delay)
            except Exception as exc:
                logger.debug("Binance WS greska: %s", exc)
            if self._running:
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    def _handle_message(self, msg: dict):
        msg_type = msg.get("type", "")

        if msg_type == "error":
            logger.error("Coinbase WS error: %s", msg.get("message", msg))
            return

        # Exchange/Pro format: ravna ticker poruka po transakciji
        if msg_type != "ticker":
            return

        asset = msg.get("product_id", "").split("-")[0]
        if asset not in self._history:
            return

        try:
            price = float(msg["price"])
            tick = PriceTick(
                asset=asset,
                price=price,
                timestamp=time.time(),
                volume_24h=float(msg.get("volume_24h", 0) or 0),
                bid=float(msg.get("best_bid", price) or price),
                ask=float(msg.get("best_ask", price) or price),
            )
            self._history[asset].add(tick)
            if self.on_tick:
                asyncio.create_task(self._fire(tick))
        except (KeyError, ValueError, TypeError):
            pass

    async def _fire(self, tick: PriceTick):
        try:
            if asyncio.iscoroutinefunction(self.on_tick):
                await self.on_tick(tick)
            else:
                self.on_tick(tick)
        except Exception as exc:
            logger.error("on_tick greska: %s", exc)

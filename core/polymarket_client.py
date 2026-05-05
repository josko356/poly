"""
core/polymarket_client.py — Polymarket CLOB API klijent.

Odgovornosti:
  1. Automatski otkriva aktivne BTC/ETH 5-min i 15-min UP/DOWN ugovore
  2. Dohvaca live order bookove za te ugovore
  3. Postavlja i otkazuje naloge (samo u live modu)
  4. Periodicno osvjezava listu ugovora (trzista istjecu i otvaraju se nova)
"""

import asyncio
import json as json_mod
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"
WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=5)
WS_TIMEOUT      = aiohttp.ClientTimeout(total=None, connect=10)
RATE_LIMIT_DELAY = 0.05  # 50ms izmedju zahtjeva (~20 req/s)
BOOK_REFRESH_INTERVAL = 5.0   # REST fallback interval kada je WebSocket aktivan
BOOK_CACHE_TTL = 3.0          # sekunde prije nego sto se cached order book smatra zastarjelim
BOOK_BATCH_SIZE = 8           # istovremeni REST dohvati po batchu

# Asseti za pracenje (prefiks sluga → naziv asseta na Coinbaseu)
# Trajanja dolaze iz config.UPDOWN_DURATIONS (zadano: [5, 15])
UPDOWN_ASSETS = {"btc": "BTC", "eth": "ETH", "sol": "SOL", "xrp": "XRP"}


# ── Modeli podataka ───────────────────────────────────────────────────────────

@dataclass
class Contract:
    condition_id: str
    question: str
    asset: str          # "BTC" | "ETH"
    direction: str      # "UP" | "DOWN"
    duration_mins: int  # 5 | 15
    token_id: str       # specificni YES/NO token ID
    end_date_iso: str
    active: bool = True
    last_price: float = 0.5
    yes_token_id: str = ""
    no_token_id: str = ""
    window_start: int = 0       # unix timestamp otvaranja prozora (iz sluga)
    price_to_beat: float = 0.0  # Chainlink cijena pri otvaranju prozora; postavlja se pri prvom skeniranju


@dataclass
class OrderBook:
    token_id: str
    best_bid: float     # najvisa cijena po kojoj netko kupuje (= cijena "YES" ako prodajemo)
    best_ask: float     # najniza cijena po kojoj netko prodaje (= sto mi placamo)
    mid: float
    spread: float
    best_ask_size: float = 0.0   # dionice dostupne po best ask (dubina likvidnosti)
    timestamp: float = field(default_factory=time.time)


# ── Klijent ───────────────────────────────────────────────────────────────────

class PolymarketClient:
    """
    Asinkroni Polymarket klijent.
    - Dohvaca aktivne ugovore s Gamma API-ja svakih `refresh_interval` sekundi.
    - Dohvaca CLOB order bookove na zahtjev.
    - Omata py-clob-client za postavljanje naloga u live modu.
    """

    def __init__(
        self,
        config,
        refresh_interval: int = 60,
    ):
        self.config = config
        self.refresh_interval = refresh_interval
        self._contracts: Dict[str, Contract] = {}   # condition_id → Contract
        self._order_books: Dict[str, OrderBook] = {}
        self._book_levels: Dict[str, Dict] = {}     # token_id → {buys: {price: size}, sells: {price: size}}
        self._session: Optional[aiohttp.ClientSession] = None
        self._refresh_task: Optional[asyncio.Task] = None
        self._book_refresh_task: Optional[asyncio.Task] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._ws = None                              # aktivna aiohttp WebSocket konekcija
        self._ws_subscribed: set = set()             # token_ids trenutno pretplaceni putem WS
        self._clob_client = None  # postavlja se u start() ako je live mod
        self._last_request = 0.0
        self._rate_lock = asyncio.Lock()

    # ── Zivotni ciklus ────────────────────────────────────────────

    async def start(self):
        self._session = aiohttp.ClientSession(timeout=REQUEST_TIMEOUT)
        await self._refresh_contracts()  # pocetno ucitavanje
        self._refresh_task = asyncio.create_task(self._refresh_loop())
        self._book_refresh_task = asyncio.create_task(self._book_refresh_loop())
        self._ws_task = asyncio.create_task(self._ws_book_loop())

        if self.config.is_live_trading:
            await self._init_clob_client()

        logger.info(
            "PolymarketClient started. Found %d active contracts.",
            len(self._contracts)
        )

    async def stop(self):
        if self._ws_task:
            self._ws_task.cancel()
        if self._refresh_task:
            self._refresh_task.cancel()
        if self._book_refresh_task:
            self._book_refresh_task.cancel()
        if self._session:
            await self._session.close()

    # ── Javni API ─────────────────────────────────────────────────

    def get_contracts(self) -> List[Contract]:
        return list(self._contracts.values())

    def get_contract(self, condition_id: str) -> Optional[Contract]:
        return self._contracts.get(condition_id)

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Vraci cached order book odmah; poziva live fetch ako je zastario."""
        cached = self._order_books.get(token_id)
        if cached and (time.time() - cached.timestamp) < BOOK_CACHE_TTL:
            return cached
        return await self._fetch_book(token_id)

    async def _fetch_book(self, token_id: str) -> Optional[OrderBook]:
        """Live fetch s ogranicenjem brzine — koristi se kao fallback i u pozadinskoj petlji."""
        await self._rate_limit()
        try:
            async with self._session.get(
                f"{CLOB_URL}/book", params={"token_id": token_id}
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
                return self._parse_order_book(token_id, data)
        except Exception as exc:
            logger.debug("Order book fetch failed for %s: %s", token_id, exc)
            return None

    async def _book_refresh_loop(self):
        """
        Kontinuirano osvjezava sve order bookove putem HTTP pollinga svakih 1.5s.
        Odrzava cache svjezim kako get_order_book() nikad ne blokira na vrucoj putanji.
        """
        await asyncio.sleep(2.0)  # pricekaj da se ugovori ucitaju
        while True:
            try:
                token_ids = list({c.token_id for c in self._contracts.values()})
                for i in range(0, len(token_ids), BOOK_BATCH_SIZE):
                    batch = token_ids[i : i + BOOK_BATCH_SIZE]
                    await asyncio.gather(
                        *[self._fetch_book(tid) for tid in batch],
                        return_exceptions=True,
                    )
                    if i + BOOK_BATCH_SIZE < len(token_ids):
                        await asyncio.sleep(0.05)
                logger.debug("Book cache refreshed: %d tokens", len(token_ids))
            except Exception as exc:
                logger.debug("Book refresh loop error: %s", exc)
            await asyncio.sleep(BOOK_REFRESH_INTERVAL)

    # ── WebSocket feed order booka ────────────────────────────────

    async def _ws_book_loop(self):
        """
        WebSocket primarni feed za azuriranja order booka u stvarnom vremenu.
        Spaja se na Polymarket CLOB WS i pretplacuje na sve aktivne token ID-jeve.
        REST polling (_book_refresh_loop) radi kao spori fallback uz ovu petlju.
        Automatski se ponovno spaja pri svakom kvaru.
        """
        await asyncio.sleep(3.0)  # pricekaj da se pocetno REST ucitavanje dovrsi
        while True:
            try:
                async with aiohttp.ClientSession(timeout=WS_TIMEOUT) as ws_session:
                    async with ws_session.ws_connect(
                        WS_URL,
                        heartbeat=20,
                        receive_timeout=90,
                    ) as ws:
                        self._ws = ws
                        self._ws_subscribed.clear()

                        # Pretplati se na sve trenutne token ID-jeve
                        token_ids = list({c.token_id for c in self._contracts.values()})
                        if token_ids:
                            await ws.send_json({"type": "MARKET", "assets_ids": token_ids})
                            self._ws_subscribed.update(token_ids)
                            logger.info("WS connected — subscribed %d token streams", len(token_ids))

                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    self._process_ws_message(json_mod.loads(msg.data))
                                except Exception:
                                    pass
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("WS disconnected: %s — reconnecting in 5s", exc)
            finally:
                self._ws = None
            await asyncio.sleep(5.0)

    def _process_ws_message(self, data):
        event = data.get("event_type")
        token_id = data.get("asset_id") or data.get("market")
        if not token_id:
            return

        if event == "book":
            # Puni snapshot — rekonstruiraj mapu razina i izvuci najbolje cijene
            buys  = {float(e["price"]): float(e["size"]) for e in data.get("buys",  []) if float(e.get("size", 0)) > 0}
            sells = {float(e["price"]): float(e["size"]) for e in data.get("sells", []) if float(e.get("size", 0)) > 0}
            self._book_levels[token_id] = {"buys": buys, "sells": sells}
            self._recompute_book(token_id)

        elif event == "price_change":
            levels = self._book_levels.get(token_id)
            if levels is None:
                return  # jos nema snapshota — cekaj
            for change in data.get("changes", []):
                price = float(change["price"])
                size  = float(change["size"])
                bucket = "buys" if change["side"] == "BUY" else "sells"
                if size == 0:
                    levels[bucket].pop(price, None)
                else:
                    levels[bucket][price] = size
            self._recompute_book(token_id)

    def _recompute_book(self, token_id: str):
        """Izvuci best bid/ask iz mape razina i zapisi u _order_books."""
        levels = self._book_levels.get(token_id)
        if not levels:
            return
        buys  = levels["buys"]
        sells = levels["sells"]

        best_bid = max(buys.keys())  if buys  else 0.0
        best_ask = min(sells.keys()) if sells else 1.0
        best_ask_size = sells.get(best_ask, 0.0) if sells else 0.0

        mid    = (best_bid + best_ask) / 2 if best_bid and best_ask else (best_bid or best_ask)
        spread = best_ask - best_bid if best_bid and best_ask else 1.0

        self._order_books[token_id] = OrderBook(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread=spread,
            best_ask_size=best_ask_size,
        )

    async def _ws_subscribe_new(self, new_token_ids: list):
        """Pretplati novootkrivene token ugovore na live WS konekciju."""
        if not self._ws or not new_token_ids:
            return
        try:
            await self._ws.send_json({"type": "MARKET", "assets_ids": new_token_ids})
            self._ws_subscribed.update(new_token_ids)
            logger.info("WS subscribed %d new tokens", len(new_token_ids))
        except Exception as exc:
            logger.debug("WS subscribe error: %s", exc)

    async def get_mid_price(self, token_id: str) -> Optional[float]:
        book = await self.get_order_book(token_id)
        return book.mid if book else None

    async def place_market_order(
        self,
        token_id: str,
        side: str,              # "buy" | "sell"
        usdc_amount: float = 0.0,
        shares: float = 0.0,    # za sell naloge: broj dionica za prodaju
    ) -> Optional[dict]:
        """
        Postavi market order i cekaj CONFIRMED status.
        Poziva se samo u live modu.

        Buy:  navedi usdc_amount — velicina se racuna iz ask cijene.
        Sell: navedi shares — prodaje se tocno taj broj dionica po bid cijeni.

        V2 CLOB napomena (travanj 2026): ~35% profitabilnih BUY naloga se vraca on-chain.
        MATCHED ≠ namiren — cekamo CONFIRMED prije nego upisemo poziciju.
        Vraca potvrdjeni order dict, ili None ako fill nije uspio/vracen je.
        """
        if not self.config.is_live_trading:
            raise RuntimeError("place_market_order called in paper-trading mode!")

        if not self._clob_client:
            logger.error("CLOB client not initialised.")
            return None

        try:
            from py_clob_client.clob_types import MarketOrderArgs

            book = await self.get_order_book(token_id)
            if not book:
                return None

            if side == "sell":
                if shares <= 0:
                    logger.error("Sell order zahtijeva shares > 0")
                    return None
                size = round(shares, 2)
            else:
                price = book.best_ask
                size  = round(usdc_amount / price, 2)

            clob_side = "SELL" if side == "sell" else "BUY"
            order_args = MarketOrderArgs(token_id=token_id, amount=size, side=clob_side)
            signed_order = self._clob_client.create_market_order(order_args)
            resp = self._clob_client.post_order(signed_order)

            order_id = (resp or {}).get("orderID") or (resp or {}).get("order_id")
            if not order_id:
                logger.warning("Order placed but no order_id returned: %s", resp)
                return resp

            logger.info("Order submitted %s — polling for CONFIRMED status", order_id)

            # Polliraj za CONFIRMED (ispunjen + namiren na Polygonu) — do 30s
            confirmed = await self._poll_order_confirmed(order_id, timeout=30.0)
            if confirmed:
                logger.info("Order CONFIRMED: %s", order_id)
                return confirmed
            else:
                logger.warning("Order %s did not reach CONFIRMED within 30s — treating as failed", order_id)
                return None

        except Exception as exc:
            logger.error("Order placement failed: %s", exc)
            return None

    async def _poll_order_confirmed(self, order_id: str, timeout: float = 30.0) -> Optional[dict]:
        """Pollaj CLOB za status naloga dok ne bude CONFIRMED ili CANCELED/timeout."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                order = await asyncio.get_running_loop().run_in_executor(
                    None, self._clob_client.get_order, order_id
                )
                status = (order or {}).get("status", "")
                if status == "CONFIRMED":
                    return order
                if status in ("CANCELED", "UNMATCHED"):
                    logger.warning("Order %s status: %s", order_id, status)
                    return None
            except Exception as exc:
                logger.debug("Poll order error: %s", exc)
            await asyncio.sleep(1.0)
        return None

    # ── Otkrivanje ugovora ────────────────────────────────────────

    async def _refresh_loop(self):
        while True:
            await asyncio.sleep(self.refresh_interval)
            try:
                await self._refresh_contracts()
            except Exception as exc:
                logger.warning("Contract refresh error: %s", exc)

    async def _refresh_contracts(self):
        """
        Otkriva aktivna Up/Down trzista putem konstrukcije sluga.
        Format sluga: {asset}-updown-{duration}m-{unix_window_start}
        Svaki asset+trajanje dobiva trenutni prozor + sljedeci prozor prethodno dohvacen.
        """
        found = {}
        now = int(time.time())

        for asset_slug, asset_name in UPDOWN_ASSETS.items():
            for duration_mins in self.config.UPDOWN_DURATIONS:
                duration_secs = duration_mins * 60
                current_window = (now // duration_secs) * duration_secs
                # Dohvati trenutni i sljedeci prozor da budemo spremni prije isteka
                for window_start in [current_window, current_window + duration_secs]:
                    slug = f"{asset_slug}-updown-{duration_mins}m-{window_start}"
                    try:
                        await self._rate_limit()
                        async with self._session.get(
                            f"{GAMMA_URL}/events",
                            params={"slug": slug},
                        ) as resp:
                            if resp.status != 200:
                                continue
                            data = await resp.json(content_type=None)
                            events = data if isinstance(data, list) else data.get("data", [])
                            for event in events:
                                for market in event.get("markets", []):
                                    contracts = self._parse_updown_market(
                                        market, asset_name, duration_mins
                                    )
                                    for c in contracts:
                                        found[c.condition_id] = c
                    except Exception as exc:
                        logger.debug("Slug fetch error %s: %s", slug, exc)

        if found:
            all_new_tokens = {c.token_id for c in found.values()}
            unseen = list(all_new_tokens - self._ws_subscribed)
            self._contracts = found
            logger.info(
                "Contract refresh: %d active Up/Down contracts found.",
                len(found),
            )
            if unseen:
                asyncio.create_task(self._ws_subscribe_new(unseen))
        else:
            logger.warning("No Up/Down contracts found — retaining previous list.")

    def _parse_updown_market(
        self, market: dict, asset: str, duration_mins: int
    ) -> List[Contract]:
        """
        Parsira Gamma event market dict u UP + DOWN Contract objekte.
        Vraca praznu listu ako trziste nije dostupno za trading ili nedostaju token ID-jevi.
        """
        if market.get("closed") or not market.get("active", True):
            return []

        # Token ID-jevi su pohranjeni kao JSON string u clobTokenIds
        try:
            token_ids = json_mod.loads(market.get("clobTokenIds", "[]"))
        except (ValueError, TypeError):
            return []

        if len(token_ids) < 2:
            return []

        up_token_id   = token_ids[0]
        down_token_id = token_ids[1]

        try:
            prices = json_mod.loads(market.get("outcomePrices", "[0.5, 0.5]"))
            up_price = float(prices[0])
        except (ValueError, TypeError, IndexError):
            up_price = 0.5

        condition_id = market.get("conditionId", market.get("id", ""))
        end_date     = market.get("endDate", market.get("endDateIso", ""))
        question     = market.get("question", "")

        # Izvuci window_start iz sluga (format: {asset}-updown-{duration}m-{timestamp})
        slug = market.get("slug", "")
        try:
            window_start = int(slug.split("-")[-1])
        except (ValueError, IndexError):
            window_start = 0

        return [
            Contract(
                condition_id=f"{condition_id}-UP",
                question=question,
                asset=asset,
                direction="UP",
                duration_mins=duration_mins,
                token_id=up_token_id,
                end_date_iso=str(end_date),
                active=True,
                last_price=up_price,
                yes_token_id=up_token_id,
                no_token_id=down_token_id,
                window_start=window_start,
            ),
            Contract(
                condition_id=f"{condition_id}-DOWN",
                question=question,
                asset=asset,
                direction="DOWN",
                duration_mins=duration_mins,
                token_id=down_token_id,
                end_date_iso=str(end_date),
                active=True,
                last_price=1.0 - up_price,
                yes_token_id=up_token_id,
                no_token_id=down_token_id,
                window_start=window_start,
            ),
        ]

    # ── Parsiranje order booka ────────────────────────────────────

    def _parse_order_book(self, token_id: str, data: dict) -> OrderBook:
        bids = data.get("bids", [])
        asks = data.get("asks", [])

        # Koristi max/min neovisno o redoslijedu sortiranja API-ja — zrcali WS _recompute_book
        best_bid = max((float(b["price"]) for b in bids), default=0.0)
        best_ask = min((float(a["price"]) for a in asks), default=1.0)
        best_ask_size = next(
            (float(a.get("size", 0)) for a in asks if float(a["price"]) == best_ask), 0.0
        ) if asks else 0.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask else best_bid or best_ask
        spread = best_ask - best_bid if best_bid and best_ask else 1.0

        book = OrderBook(
            token_id=token_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread=spread,
            best_ask_size=best_ask_size,
        )
        self._order_books[token_id] = book
        return book

    # ── Pomocne metode ────────────────────────────────────────────

    async def _rate_limit(self):
        async with self._rate_lock:
            now = time.time()
            elapsed = now - self._last_request
            if elapsed < RATE_LIMIT_DELAY:
                await asyncio.sleep(RATE_LIMIT_DELAY - elapsed)
            self._last_request = time.time()

    async def _init_clob_client(self):
        try:
            from py_clob_client.client import ClobClient
            self._clob_client = ClobClient(
                host=CLOB_URL,
                chain_id=137,  # Polygon mainnet (ne mjenjati)
                key=self.config.POLYGON_PRIVATE_KEY,
            )
            logger.info("CLOB client initialised for live trading.")
        except Exception as exc:
            logger.error("Failed to init CLOB client: %s", exc)

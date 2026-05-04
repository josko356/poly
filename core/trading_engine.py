"""
core/trading_engine.py — Paper and live trading execution.

Paper mode:
  - Simulates fills at best_ask + configurable slippage.
  - Tracks virtual P&L in memory and persists to SQLite.
  - Monitors open positions and resolves them when contracts expire.

Live mode:
  - Requires all three safety flags + wallet credentials in .env.
  - Calls PolymarketClient.place_market_order() for real execution.
  - All order results logged and monitored.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional

from .arbitrage_engine import Opportunity
from .database import Database, TradeRecord
from .kelly_sizer import KellySizer, SizingResult
from .polymarket_client import PolymarketClient
from .risk_manager import RiskManager

logger = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    trade_id: int
    opportunity: Opportunity
    sizing: SizingResult
    entry_time: float
    expected_expiry: float   # unix timestamp
    mode: str                # "paper" | "live"

    # Paper tracking
    paper_entry_price: float = 0.0
    paper_shares: float = 0.0
    paper_usdc_spent: float = 0.0


class TradingEngine:
    """
    Orchestrates trade execution for paper and live modes.
    """

    def __init__(
        self,
        config,
        polymarket: PolymarketClient,
        risk: RiskManager,
        db: Database,
        on_trade_open: Optional[Callable] = None,
        on_trade_close: Optional[Callable] = None,
        on_alert: Optional[Callable] = None,
    ):
        self.config = config
        self.polymarket = polymarket
        self.risk = risk
        self.db = db
        self.on_trade_open = on_trade_open
        self.on_trade_close = on_trade_close
        self.on_alert = on_alert

        self.sizer = KellySizer(config)
        self._positions: Dict[int, OpenPosition] = {}
        self._pending_keys: set = set()  # (asset, smjer, trajanje_min) koji se trenutno otvaraju
        self._monitor_task: Optional[asyncio.Task] = None
        self._recent_trades: list = []   # svi zatvoreni tradovi ove sesije (resetira se pri restartu)

    @property
    def mode(self) -> str:
        return "live" if self.config.is_live_trading else "paper"

    @property
    def open_positions(self) -> List[OpenPosition]:
        return list(self._positions.values())

    @property
    def recent_trades(self) -> list:
        """Zadnjih 10 tradova za prikaz u dashboardu."""
        return self._recent_trades[-10:]

    @property
    def session_trades(self) -> list:
        """Svi zatvoreni tradovi ove sesije — koristi se za izracun win ratea."""
        return self._recent_trades

    # ── Zivotni ciklus ────────────────────────────────────────────

    async def start(self):
        self._monitor_task = asyncio.create_task(self._monitor_positions())
        logger.info("TradingEngine started in %s mode.", self.mode.upper())

    async def stop(self):
        if self._monitor_task:
            self._monitor_task.cancel()

    # ── Glavna ulazna tocka ───────────────────────────────────────

    async def execute_opportunity(self, opp: Opportunity) -> bool:
        """Pokusaj izvrsiti otkrivenu arbitraznu priliku. Vraca True ako je trade otvoren."""
        # Pre-trade provjera rizika (bundle ima vlastiti sat-limit, ne trosi regularnu kvotu)
        can, reason = self.risk.can_trade(is_bundle=opp.is_bundle)
        if not can:
            logger.info("Trade blocked: %s", reason)
            return False

        # Zastita od duplikata — jedna pozicija po asset+smjer+trajanje.
        # _pending_keys zakljucava kljuc odmah, prije svakog awaita,
        # pa istovremeni skenovi koji stignu za vrijeme DB inserta takodje budu blokirani.
        if not opp.is_bundle:
            key = (opp.contract.asset, opp.direction, opp.contract.duration_mins)
            for pos in self._positions.values():
                if (not pos.opportunity.is_bundle and
                    pos.opportunity.contract.asset == opp.contract.asset and
                    pos.opportunity.direction == opp.direction and
                    pos.opportunity.contract.duration_mins == opp.contract.duration_mins):
                    logger.debug(
                        "Duplicate skipped: already have %s %s %dmin open",
                        opp.contract.asset, opp.direction, opp.contract.duration_mins,
                    )
                    return False
            if key in self._pending_keys:
                logger.debug("Pending duplicate skipped: %s %s %dmin", *key)
                return False
            self._pending_keys.add(key)
        else:
            key = (opp.contract.asset, "BUNDLE", opp.contract.duration_mins)
            for pos in self._positions.values():
                if (pos.opportunity.is_bundle and
                    pos.opportunity.contract.asset == opp.contract.asset and
                    pos.opportunity.contract.duration_mins == opp.contract.duration_mins):
                    logger.debug(
                        "Bundle duplicate skipped: already have %s %dmin bundle open",
                        opp.contract.asset, opp.contract.duration_mins,
                    )
                    return False
            if key in self._pending_keys:
                logger.debug("Pending bundle duplicate skipped: %s %dmin", opp.contract.asset, opp.contract.duration_mins)
                return False
            self._pending_keys.add(key)

        # Odredivanje velicine pozicije
        if opp.is_bundle:
            # Bundle je bez rizika gubitka — Kelly ne vrijedi, koristimo fiksni % slobodnog balansa.
            # Live mod koristi manji postotak: ako rollback propadne, naked pozicija je samo 20% ne 40%.
            if self.mode == "live":
                bundle_pct = getattr(self.config, "BUNDLE_POSITION_PCT_LIVE", 0.20)
            else:
                bundle_pct = getattr(self.config, "BUNDLE_POSITION_PCT", 0.40)
            usdc = max(self.config.MIN_TRADE_USDC,
                       min(self.risk.balance * bundle_pct, self.risk.balance * 0.90))
            sizing = SizingResult(
                kelly_fraction=bundle_pct, half_kelly=bundle_pct,
                position_pct=bundle_pct, usdc_amount=usdc,
                shares=usdc / max(opp.polymarket_price, 0.01),
                entry_price=opp.polymarket_price,
                expected_value=usdc * opp.edge,
            )
        else:
            sizing = self.sizer.size(
                portfolio_balance=self.risk.balance,
                model_prob=opp.model_prob,
                entry_price=opp.polymarket_price,
            )

        if sizing.usdc_amount < self.config.MIN_TRADE_USDC:
            logger.debug("Trade too small (%.2f USDC) — skipping.", sizing.usdc_amount)
            self._pending_keys.discard(key)
            return False

        try:
            if opp.is_bundle:
                return await self._open_bundle_trade(opp, sizing)
            elif self.mode == "paper":
                return await self._open_paper_trade(opp, sizing)
            else:
                return await self._open_live_trade(opp, sizing)
        finally:
            self._pending_keys.discard(key)

    # ── Paper trading ─────────────────────────────────────────────

    async def _open_paper_trade(self, opp: Opportunity, sizing: SizingResult) -> bool:
        # Simulacija slippagea
        fill_price = opp.polymarket_price * (1 + self.config.PAPER_FILL_SLIPPAGE)
        fill_price = min(fill_price, 0.99)

        shares = sizing.usdc_amount / fill_price
        usdc_spent = shares * fill_price

        # Risk manager oduzima od balansa
        self.risk.on_trade_opened(usdc_spent)

        # Spremi u bazu
        record = TradeRecord(
            id=None,
            timestamp=datetime.utcnow().isoformat(),
            mode="paper",
            asset=opp.contract.asset,
            contract_id=opp.contract.condition_id,
            contract_question=opp.contract.question,
            direction=opp.direction,
            duration_mins=opp.contract.duration_mins,
            entry_price=fill_price,
            shares=shares,
            usdc_spent=usdc_spent,
            edge=opp.edge,
            confidence=opp.confidence,
            kelly_size=sizing.half_kelly,
            polymarket_prob=opp.polymarket_price,
            model_prob=opp.model_prob,
            coinbase_price=opp.coinbase_price,
            status="open",
            pnl=None,
            exit_timestamp=None,
        )
        trade_id = await self.db.insert_trade(record)

        # Prati u memoriji
        expiry = time.time() + (opp.contract.duration_mins * 60)
        pos = OpenPosition(
            trade_id=trade_id,
            opportunity=opp,
            sizing=sizing,
            entry_time=time.time(),
            expected_expiry=expiry,
            mode="paper",
            paper_entry_price=fill_price,
            paper_shares=shares,
            paper_usdc_spent=usdc_spent,
        )
        self._positions[trade_id] = pos

        logger.info(
            "[PAPER] Otvoren %s %s %dmin | price=%.3f | edge=%.1f%% | conf=%.1f%% | $%.2f",
            opp.contract.asset, opp.direction, opp.contract.duration_mins,
            fill_price, opp.edge * 100, opp.confidence * 100, usdc_spent,
        )

        if self.on_trade_open:
            await self._safe_callback(self.on_trade_open, pos)

        return True

    async def _open_bundle_trade(self, opp: Opportunity, sizing: SizingResult) -> bool:
        """Bundle arbitraza: kupovina UP + DOWN tokena po ukupnoj cijeni < $1.
        Garantirani profit pri isteku bez obzira na ishod.
        Live mod: dva odvojena CLOB ordera s rollbackom ako druga noga propadne.
        """
        if self.mode == "live":
            return await self._open_live_bundle(opp, sizing)


        total_cost_per_pair = opp.polymarket_price  # up_ask + down_ask
        fill_cost = total_cost_per_pair * (1 + self.config.PAPER_FILL_SLIPPAGE)
        fill_cost = min(fill_cost, 0.99)

        pairs = sizing.usdc_amount / fill_cost
        usdc_spent = pairs * fill_cost
        guaranteed_pnl = pairs * opp.edge  # opp.edge = 1 - total_cost - fees

        self.risk.on_trade_opened(usdc_spent, is_bundle=True)

        record = TradeRecord(
            id=None,
            timestamp=datetime.utcnow().isoformat(),
            mode="paper",
            asset=opp.contract.asset,
            contract_id=opp.contract.condition_id,
            contract_question=f"[BUNDLE] {opp.contract.question}",
            direction="BUNDLE",
            duration_mins=opp.contract.duration_mins,
            entry_price=fill_cost,
            shares=pairs,
            usdc_spent=usdc_spent,
            edge=opp.edge,
            confidence=1.0,
            kelly_size=sizing.half_kelly,
            polymarket_prob=opp.polymarket_price,
            model_prob=1.0,
            coinbase_price=opp.coinbase_price,
            status="open",
            pnl=None,
            exit_timestamp=None,
        )
        trade_id = await self.db.insert_trade(record)

        expiry = time.time() + (opp.contract.duration_mins * 60)
        pos = OpenPosition(
            trade_id=trade_id,
            opportunity=opp,
            sizing=sizing,
            entry_time=time.time(),
            expected_expiry=expiry,
            mode="paper",
            paper_entry_price=fill_cost,
            paper_shares=pairs,
            paper_usdc_spent=usdc_spent,
        )
        # Spremi zagarantovani PnL za zatvaranje
        pos._guaranteed_pnl = guaranteed_pnl
        self._positions[trade_id] = pos

        logger.info(
            "[PAPER] BUNDLE %s %dmin | cost=%.3f | guaranteed_profit=%.1f%% | $%.2f",
            opp.contract.asset, opp.contract.duration_mins,
            fill_cost, opp.edge * 100, usdc_spent,
        )

        if self.on_trade_open:
            await self._safe_callback(self.on_trade_open, pos)

        return True

    # ── Live trading ──────────────────────────────────────────────

    async def _open_live_bundle(self, opp: Opportunity, sizing: SizingResult) -> bool:
        """
        Live bundle egzekucija: dva CLOB ordera (UP + DOWN).
        Ako DOWN noga propadne, pokusavamo rollback UP noge.
        Ako rollback propadne, salje alert — pozicija zahtijeva rucnu intervenciju.
        """
        up_contract = opp.contract
        down_contract = opp._bundle_down_contract
        up_ask = opp.order_book.best_ask
        down_ask = opp._bundle_down_book.best_ask
        total_per_par = up_ask + down_ask

        parovi = sizing.usdc_amount / total_per_par
        up_usdc = parovi * up_ask
        down_usdc = parovi * down_ask

        logger.info("[LIVE BUNDLE] %s %dmin | UP=%.3f DOWN=%.3f | parovi=%.1f | $%.2f",
                    up_contract.asset, up_contract.duration_mins,
                    up_ask, down_ask, parovi, sizing.usdc_amount)

        # Noga 1: UP
        up_result = await self.polymarket.place_market_order(
            token_id=up_contract.token_id, side="buy", usdc_amount=up_usdc,
        )
        if not up_result:
            logger.error("[LIVE BUNDLE] UP noga propala — nema gubitka, odustajemo")
            return False

        up_shares = float(up_result.get("size", parovi))
        up_fill = float(up_result.get("price", up_ask))

        # Noga 2: DOWN
        down_result = await self.polymarket.place_market_order(
            token_id=down_contract.token_id, side="buy", usdc_amount=down_usdc,
        )
        if not down_result:
            logger.error("[LIVE BUNDLE] DOWN noga propala — pokusaj rollbacka UP noge")
            rollback = await self.polymarket.place_market_order(
                token_id=up_contract.token_id, side="sell", shares=up_shares,
            )
            if rollback:
                logger.info("[LIVE BUNDLE] Rollback UP uspio — nema gubitka")
            else:
                msg = (f"BUNDLE ROLLBACK PROPAO — naked UP pozicija ostaje!\n"
                       f"Token: {up_contract.token_id}\nDionice: {up_shares:.2f}\n"
                       f"Provjeri rucno na Polymarketu.")
                logger.error("[LIVE BUNDLE] %s", msg)
                if self.on_alert:
                    await self._safe_callback(self.on_alert, msg)
            return False

        down_shares = float(down_result.get("size", parovi))
        down_fill = float(down_result.get("price", down_ask))
        usdc_spent = up_shares * up_fill + down_shares * down_fill
        guaranteed_pnl = min(up_shares, down_shares) * (1.0 - up_fill - down_fill)

        self.risk.on_trade_opened(usdc_spent, is_bundle=True)

        record = TradeRecord(
            id=None, timestamp=datetime.utcnow().isoformat(), mode="live",
            asset=up_contract.asset,
            contract_id=up_contract.condition_id,
            contract_question=f"[BUNDLE] {up_contract.question}",
            direction="BUNDLE", duration_mins=up_contract.duration_mins,
            entry_price=(up_fill + down_fill), shares=min(up_shares, down_shares),
            usdc_spent=usdc_spent, edge=opp.edge, confidence=1.0,
            kelly_size=sizing.half_kelly, polymarket_prob=opp.polymarket_price,
            model_prob=1.0, coinbase_price=opp.coinbase_price,
            status="open", pnl=None, exit_timestamp=None,
        )
        trade_id = await self.db.insert_trade(record)

        expiry = time.time() + (up_contract.duration_mins * 60)
        pos = OpenPosition(
            trade_id=trade_id, opportunity=opp, sizing=sizing,
            entry_time=time.time(), expected_expiry=expiry, mode="live",
            paper_entry_price=(up_fill + down_fill),
            paper_shares=min(up_shares, down_shares),
            paper_usdc_spent=usdc_spent,
        )
        pos._guaranteed_pnl = guaranteed_pnl
        self._positions[trade_id] = pos

        logger.info("[LIVE BUNDLE] Obje noge potvrdjene | $%.2f utroseno | garantirani profit: $%.2f",
                    usdc_spent, guaranteed_pnl)

        if self.on_trade_open:
            await self._safe_callback(self.on_trade_open, pos)

        return True

    async def _open_live_trade(self, opp: Opportunity, sizing: SizingResult) -> bool:
        # Tvrdi limit: nikad vise od MAX_LIVE_TRADE_USDC po tradu
        max_cap = getattr(self.config, "MAX_LIVE_TRADE_USDC", 50.0)
        usdc_to_spend = min(sizing.usdc_amount, max_cap)
        if usdc_to_spend < sizing.usdc_amount:
            logger.info(
                "[LIVE] Kelly sized $%.2f → capped at $%.2f (MAX_LIVE_TRADE_USDC)",
                sizing.usdc_amount, usdc_to_spend,
            )

        logger.info(
            "[LIVE] Placing order: %s %s %dmin | $%.2f",
            opp.contract.asset, opp.direction,
            opp.contract.duration_mins, usdc_to_spend,
        )

        result = await self.polymarket.place_market_order(
            token_id=opp.contract.token_id,
            side="buy",
            usdc_amount=usdc_to_spend,
        )

        if not result:
            logger.error("[LIVE] Order placement failed or not confirmed on-chain.")
            return False

        # Parse fill details from response
        fill_price = result.get("price", opp.polymarket_price)
        shares = result.get("size", usdc_to_spend / max(fill_price, 0.001))
        usdc_spent = shares * fill_price

        # Zastita od slippagea: odbaci ako je fill cijena drasticno losija od modelirane
        max_slip = getattr(self.config, "MAX_LIVE_SLIPPAGE_PCT", 0.015)
        if fill_price > opp.polymarket_price * (1 + max_slip):
            logger.error(
                "[LIVE] SLIPPAGE ABORT: fill=%.4f model=%.4f (%.1f%% > %.1f%% limit) — not booking position",
                fill_price, opp.polymarket_price,
                (fill_price / opp.polymarket_price - 1) * 100, max_slip * 100,
            )
            return False

        self.risk.on_trade_opened(usdc_spent)

        record = TradeRecord(
            id=None,
            timestamp=datetime.utcnow().isoformat(),
            mode="live",
            asset=opp.contract.asset,
            contract_id=opp.contract.condition_id,
            contract_question=opp.contract.question,
            direction=opp.direction,
            duration_mins=opp.contract.duration_mins,
            entry_price=fill_price,
            shares=shares,
            usdc_spent=usdc_spent,
            edge=opp.edge,
            confidence=opp.confidence,
            kelly_size=sizing.half_kelly,
            polymarket_prob=opp.polymarket_price,
            model_prob=opp.model_prob,
            coinbase_price=opp.coinbase_price,
            status="open",
            pnl=None,
            exit_timestamp=None,
        )
        trade_id = await self.db.insert_trade(record)

        expiry = time.time() + (opp.contract.duration_mins * 60)
        pos = OpenPosition(
            trade_id=trade_id,
            opportunity=opp,
            sizing=sizing,
            entry_time=time.time(),
            expected_expiry=expiry,
            mode="live",
            paper_entry_price=fill_price,
            paper_shares=shares,
            paper_usdc_spent=usdc_spent,
        )
        self._positions[trade_id] = pos

        if self.on_trade_open:
            await self._safe_callback(self.on_trade_open, pos)

        return True

    # ── Pracenje pozicija ─────────────────────────────────────────

    async def _monitor_positions(self):
        """Periodicna provjera otvorenih pozicija za rani izlaz ili istek."""
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for pos in list(self._positions.values()):
                # Bundle pozicije uvijek pobijede pri isteku — preskoci provjeru ranog izlaza
                if pos.opportunity.is_bundle:
                    if now >= pos.expected_expiry + 30:
                        await self._resolve_bundle(pos)
                    continue

                # Provjere ranog izlaza / rane pobjede
                book = await self.polymarket.get_order_book(pos.opportunity.contract.token_id)
                if book and book.mid > 0:
                    # Rana pobjeda: zakljucaj profit kad token jasno dobiva
                    if book.mid >= self.config.EARLY_WIN_THRESHOLD and now < pos.expected_expiry - 30:
                        await self._close_position_early_win(pos, book.mid)
                        continue
                    # Rani izlaz: smanji gubitke kad token jasno gubi
                    threshold = self.config.EARLY_EXIT_THRESHOLD * pos.paper_entry_price
                    if book.mid < threshold:
                        await self._close_position_early(pos, book.mid)
                        continue

                # Provjera isteka
                if now >= pos.expected_expiry + 30:
                    await self._resolve_position(pos)

    async def _close_position_early(self, pos: OpenPosition, current_mid: float):
        """Izlaz iz gubitnicke pozicije prije isteka radi povrata preostale vrijednosti."""
        exit_price = max(current_mid - self.config.PAPER_FILL_SLIPPAGE, 0.01)
        proceeds = pos.paper_shares * exit_price
        pnl = proceeds - pos.paper_usdc_spent

        self.risk.on_trade_closed(proceeds, pnl)
        self._positions.pop(pos.trade_id, None)
        await self.db.update_trade_result(pos.trade_id, "exited", pnl)

        self._recent_trades.append({
            "id": pos.trade_id,
            "asset": pos.opportunity.contract.asset,
            "direction": pos.opportunity.direction,
            "duration": pos.opportunity.contract.duration_mins,
            "entry_price": pos.paper_entry_price,
            "pnl": pnl,
            "status": "exited",
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(
            "[PAPER] PRIJEVREMENI IZLAZ %s %s %dmin | entry=%.3f exit=%.3f | P&L=%.2f USDC (saved vs full loss: %.2f)",
            pos.opportunity.contract.asset, pos.opportunity.direction,
            pos.opportunity.contract.duration_mins,
            pos.paper_entry_price, exit_price, pnl,
            proceeds,
        )

        if self.on_trade_close:
            await self._safe_callback(self.on_trade_close, pos, pnl, "exited")

    async def _close_position_early_win(self, pos: OpenPosition, current_mid: float):
        """Zakljucaj profit ranom prodajom kad token dostigne EARLY_WIN_THRESHOLD."""
        exit_price = min(current_mid - self.config.PAPER_FILL_SLIPPAGE, 0.98)
        proceeds = pos.paper_shares * exit_price
        pnl = proceeds - pos.paper_usdc_spent

        self.risk.on_trade_closed(proceeds, pnl)
        self._positions.pop(pos.trade_id, None)
        await self.db.update_trade_result(pos.trade_id, "won", pnl)

        self._recent_trades.append({
            "id": pos.trade_id,
            "asset": pos.opportunity.contract.asset,
            "direction": pos.opportunity.direction,
            "duration": pos.opportunity.contract.duration_mins,
            "entry_price": pos.paper_entry_price,
            "pnl": pnl,
            "status": "won",
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(
            "[PAPER] PRIJEVREMENA POBJEDA %s %s %dmin | entry=%.3f exit=%.3f | P&L=+%.2f USDC",
            pos.opportunity.contract.asset, pos.opportunity.direction,
            pos.opportunity.contract.duration_mins,
            pos.paper_entry_price, exit_price, pnl,
        )

        if self.on_trade_close:
            await self._safe_callback(self.on_trade_close, pos, pnl, "won")

    async def _resolve_bundle(self, pos: OpenPosition):
        """Bundle arb uvijek pobijedi — jedan od dva tokena uvijek isplati $1."""
        pnl = getattr(pos, "_guaranteed_pnl", pos.paper_usdc_spent * pos.opportunity.edge)
        gross_return = pos.paper_usdc_spent + pnl

        self.risk.on_trade_closed(gross_return, pnl)
        self._positions.pop(pos.trade_id, None)
        await self.db.update_trade_result(pos.trade_id, "won", pnl)

        self._recent_trades.append({
            "id": pos.trade_id,
            "asset": pos.opportunity.contract.asset,
            "direction": "BUNDLE",
            "duration": pos.opportunity.contract.duration_mins,
            "entry_price": pos.paper_entry_price,
            "pnl": pnl,
            "status": "won",
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(
            "[PAPER] BUNDLE DOBIVEN %s %dmin | P&L=+%.2f USDC",
            pos.opportunity.contract.asset, pos.opportunity.contract.duration_mins, pnl,
        )

        if self.on_trade_close:
            await self._safe_callback(self.on_trade_close, pos, pnl, "won")

    async def _resolve_position(self, pos: OpenPosition):
        """Zatvori isteklu poziciju."""
        try:
            if pos.mode == "paper":
                await self._resolve_paper(pos)
            else:
                await self._resolve_live(pos)
        except Exception as exc:
            logger.error("Position resolution error for trade %d: %s", pos.trade_id, exc)
            self._positions.pop(pos.trade_id, None)

    async def _resolve_paper(self, pos: OpenPosition):
        """Simulacija zatvaranja paper trada. Provjeravamo Polymarket cijenu tokena — >= 0.95 je pobjeda, <= 0.05 je gubitak."""
        token_id = pos.opportunity.contract.token_id
        book = await self.polymarket.get_order_book(token_id)

        # Namireni ugovori imaju cijenu blizu 0 ili 1
        if book and book.mid >= 0.95:
            won = True
        elif book and book.mid <= 0.05:
            won = False
        else:
            # Ugovor jos nije namiren — provjeri opet
            # Produzi ocekivani rok za jos 30s
            pos.expected_expiry = time.time() + 30
            self._positions[pos.trade_id] = pos
            return

        if won:
            # Pobjeda: $1 po dionici. Neto profit = dionice*(1-ulazna_cijena).
            pnl = pos.paper_shares * (1.0 - pos.paper_entry_price)
            gross_return = pos.paper_shares
            status = "won"
        else:
            # Gubitak: ulozeni iznos vec oduzet pri otvaranju, nema povrata
            pnl = -pos.paper_usdc_spent
            gross_return = 0.0
            status = "lost"

        self.risk.on_trade_closed(gross_return, pnl)
        self._positions.pop(pos.trade_id, None)

        await self.db.update_trade_result(pos.trade_id, status, pnl)

        self._recent_trades.append({
            "id": pos.trade_id,
            "asset": pos.opportunity.contract.asset,
            "direction": pos.opportunity.direction,
            "duration": pos.opportunity.contract.duration_mins,
            "entry_price": pos.paper_entry_price,
            "pnl": pnl,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(
            "[PAPER] %s %s %s %dmin | P&L=%.2f USDC",
            "DOBIVEN" if won else "IZGUBLJEN",
            pos.opportunity.contract.asset,
            pos.opportunity.direction,
            pos.opportunity.contract.duration_mins,
            pnl,
        )

        if self.on_trade_close:
            await self._safe_callback(self.on_trade_close, pos, pnl, status)

    async def _resolve_live(self, pos: OpenPosition):
        """Provjera live namirenja putem CLOB-a."""
        token_id = pos.opportunity.contract.token_id
        book = await self.polymarket.get_order_book(token_id)

        if not book:
            pos.expected_expiry = time.time() + 60
            self._positions[pos.trade_id] = pos
            return

        if book.mid >= 0.95:
            won = True
        elif book.mid <= 0.05:
            won = False
        else:
            pos.expected_expiry = time.time() + 30
            self._positions[pos.trade_id] = pos
            return

        pnl = (pos.paper_shares * (1.0 - pos.paper_entry_price)) if won else (-pos.paper_usdc_spent)
        gross_return = pos.paper_shares if won else 0.0
        status = "won" if won else "lost"

        self.risk.on_trade_closed(gross_return, pnl)
        self._positions.pop(pos.trade_id, None)
        await self.db.update_trade_result(pos.trade_id, status, pnl)

        self._recent_trades.append({
            "id": pos.trade_id,
            "asset": pos.opportunity.contract.asset,
            "direction": pos.opportunity.direction,
            "duration": pos.opportunity.contract.duration_mins,
            "entry_price": pos.paper_entry_price,
            "pnl": pnl,
            "status": status,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info("[LIVE] %s P&L=%.2f", "DOBIVEN" if won else "IZGUBLJEN", pnl)

        if self.on_trade_close:
            await self._safe_callback(self.on_trade_close, pos, pnl, status)

    # ── Pomocne metode ────────────────────────────────────────────

    async def _safe_callback(self, cb, *args):
        try:
            if asyncio.iscoroutinefunction(cb):
                await cb(*args)
            else:
                cb(*args)
        except Exception as exc:
            logger.error("Callback error: %s", exc)

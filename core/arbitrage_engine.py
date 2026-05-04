"""
core/arbitrage_engine.py — Latency-arbitrage opportunity detector.

FIXES vs. Linux version:
  - _normal_cdf and helpers called via class, not self (fixes NoneType error)
  - _estimate_up_probability and _confidence_score are now proper @staticmethods
  - All internal calls use ArbitrageEngine.method() pattern
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import List, Optional

from .chainlink_feed import ChainlinkFeed
from .coinbase_feed import CoinbaseFeed, PriceTick
from .polymarket_client import Contract, OrderBook, PolymarketClient

logger = logging.getLogger(__name__)


@dataclass
class Opportunity:
    contract: Contract
    order_book: OrderBook
    polymarket_price: float
    model_prob: float
    edge: float
    confidence: float
    coinbase_price: float
    price_change_pct: float
    direction: str
    timestamp: float = 0.0
    is_bundle: bool = False

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    @property
    def is_actionable(self) -> bool:
        return self.edge > 0 and self.confidence > 0


class ArbitrageEngine:
    def __init__(
        self,
        config,
        feed: CoinbaseFeed,
        polymarket: PolymarketClient,
        chainlink: Optional[ChainlinkFeed] = None,
    ):
        self.config = config
        self.feed = feed
        self.polymarket = polymarket
        self.chainlink = chainlink
        self._last_scan: dict = {}
        self._scan_interval = 0.1

    async def scan(self, asset: str) -> List[Opportunity]:
        last = self._last_scan.get(asset, 0.0)
        if time.time() - last < self._scan_interval:
            return []
        self._last_scan[asset] = time.time()

        if not self.feed.is_fresh(asset, max_age=self.config.PRICE_STALENESS_LIMIT):
            return []

        tick = self.feed.latest(asset)
        if not tick:
            return []

        price_change = self.feed.price_change_pct(asset, self.config.PRICE_WINDOW_SECONDS)

        if abs(price_change) < self.config.LAG_THRESHOLD_PCT:
            return []

        logger.info(
            "LAG signal %s: price=%.2f Δ=%.3f%% — scanning contracts",
            asset, tick.price, price_change * 100,
        )

        # Only evaluate contracts in the signal direction — halves API calls
        direction = "UP" if price_change > 0 else "DOWN"
        contracts = [
            c for c in self.polymarket.get_contracts()
            if c.asset == asset and c.active and c.direction == direction
        ]
        if not contracts:
            return []

        # Capture price_to_beat on first encounter (approximates Chainlink price at window open)
        for contract in contracts:
            if contract.price_to_beat == 0.0 and tick.price > 0:
                contract.price_to_beat = tick.price
                logger.debug(
                    "price_to_beat set: %s %s %dmin → %.2f",
                    asset, contract.direction, contract.duration_mins, tick.price,
                )

        books = await asyncio.gather(
            *[self.polymarket.get_order_book(c.token_id) for c in contracts],
            return_exceptions=True,
        )

        opportunities: List[Opportunity] = []
        for contract, book in zip(contracts, books):
            if isinstance(book, Exception) or book is None:
                continue
            opp = self._evaluate(contract, book, tick, price_change)
            if opp and opp.is_actionable:
                opportunities.append(opp)

        bundle_opps = self._scan_bundles(asset)
        opportunities.extend(bundle_opps)

        if opportunities:
            logger.info(
                "Found %d opportunity(ies) for %s (Δ=%.3f%%, %d bundle)",
                len(opportunities), asset, price_change * 100, len(bundle_opps),
            )
        return opportunities

    def _evaluate(
        self,
        contract: Contract,
        book: OrderBook,
        tick: PriceTick,
        price_change_pct: float,
    ) -> Optional[Opportunity]:
        book_age_ms = int((time.time() - book.timestamp) * 1000)
        market_price = book.best_ask
        if market_price <= 0 or market_price >= 1:
            logger.info(
                "  ✗ %s %s %dmin: invalid ask=%.3f (book_age=%dms)",
                contract.asset, contract.direction, contract.duration_mins,
                market_price, book_age_ms,
            )
            return None

        # Reject deep OTM contracts — model probability is unreliable below MIN_MARKET_PRICE.
        # A 9-cent option means market assigns ~9% probability; our log-normal model
        # can diverge by 30+ percentage points here, producing false high-edge signals.
        if market_price < self.config.MIN_MARKET_PRICE:
            logger.info(
                "  ✗ %s %s %dmin: deep OTM ask=%.3f < %.2f (book_age=%dms)",
                contract.asset, contract.direction, contract.duration_mins,
                market_price, self.config.MIN_MARKET_PRICE, book_age_ms,
            )
            return None

        elapsed_secs = int(time.time()) - contract.window_start if contract.window_start > 0 else 0

        # Don't enter in the last minute — market is already efficient near expiry
        time_remaining = contract.duration_mins * 60 - elapsed_secs
        if time_remaining < self.config.MIN_WINDOW_SECS_REMAINING:
            logger.info(
                "  ✗ %s %s %dmin: only %ds remaining (min=%ds) (book_age=%dms)",
                contract.asset, contract.direction, contract.duration_mins,
                time_remaining, self.config.MIN_WINDOW_SECS_REMAINING, book_age_ms,
            )
            return None

        # Liquidity depth: ensure enough shares available at best ask
        min_shares = self.config.MIN_TRADE_USDC / max(market_price, 0.01)
        if book.best_ask_size > 0 and book.best_ask_size < min_shares * 0.5:
            logger.info(
                "  ✗ %s %s %dmin: low liquidity ask_size=%.1f < %.1f needed (book_age=%dms)",
                contract.asset, contract.direction, contract.duration_mins,
                book.best_ask_size, min_shares * 0.5, book_age_ms,
            )
            return None

        # Dynamic realized volatility (more accurate than hardcoded 80%)
        annual_vol = self.feed.realized_vol_annual(contract.asset, lookback_secs=120)
        annual_vol = max(0.10, min(3.0, annual_vol))

        # Use Chainlink oracle price as the settlement reference when available.
        # Polymarket settles against Chainlink, not Coinbase — divergence matters.
        oracle_price = None
        if self.chainlink:
            oracle_price = self.chainlink.get_validated(contract.asset, tick.price)
        effective_price = oracle_price if oracle_price else tick.price

        # If oracle is available and price_to_beat not set yet, bootstrap it now
        if oracle_price and contract.price_to_beat == 0.0:
            contract.price_to_beat = oracle_price
            logger.debug(
                "price_to_beat (oracle) set: %s %s %dmin → %.4f",
                contract.asset, contract.direction, contract.duration_mins, oracle_price,
            )

        # Build an effective tick using oracle price for the probability model
        effective_tick = PriceTick(
            asset=tick.asset,
            price=effective_price,
            timestamp=tick.timestamp,
            volume_24h=tick.volume_24h,
            bid=tick.bid,
            ask=tick.ask,
        )

        model_prob_up = ArbitrageEngine._estimate_up_probability(
            price_change_pct=price_change_pct,
            duration_mins=contract.duration_mins,
            tick=effective_tick,
            book=book,
            momentum_weight=self.config.MOMENTUM_WEIGHT,
            book_weight=self.config.BOOK_WEIGHT,
            price_to_beat=contract.price_to_beat,
            elapsed_secs=elapsed_secs,
            annual_vol=annual_vol,
        )

        model_prob = (1.0 - model_prob_up) if contract.direction == "DOWN" else model_prob_up
        edge = model_prob - market_price - self.config.TAKER_FEE
        confidence = ArbitrageEngine._confidence_score(
            price_change_pct=price_change_pct,
            edge=edge,
            book=book,
            duration_mins=contract.duration_mins,
        )

        # Chainlink oracle boost: when Coinbase price diverges >0.4% from the last on-chain
        # oracle value, a deviation-triggered round update is imminent — this is the highest-
        # confidence signal available. Cap at 0.99 to avoid false certainty.
        if oracle_price and tick.price > 0:
            oracle_deviation = abs(tick.price - oracle_price) / oracle_price
            if oracle_deviation >= 0.004:
                boosted = min(0.99, confidence * 1.15)
                logger.debug(
                    "Oracle deviation %.2f%% → confidence boost %.2f→%.2f for %s",
                    oracle_deviation * 100, confidence, boosted, contract.asset,
                )
                confidence = boosted

        if (
            edge < self.config.MIN_EDGE
            or confidence < self.config.MIN_CONFIDENCE
            or abs(price_change_pct) < self.config.LAG_THRESHOLD_PCT
        ):
            reasons = []
            if edge < self.config.MIN_EDGE:
                reasons.append(f"edge={edge:.3f}<{self.config.MIN_EDGE}")
            if confidence < self.config.MIN_CONFIDENCE:
                reasons.append(f"conf={confidence:.2f}<{self.config.MIN_CONFIDENCE}")
            if abs(price_change_pct) < self.config.LAG_THRESHOLD_PCT:
                reasons.append(f"Δ={price_change_pct*100:.3f}%<{self.config.LAG_THRESHOLD_PCT*100:.3f}%")
            logger.info(
                "  ✗ %s %s %dmin: ask=%.3f model=%.3f edge=%.3f conf=%.2f book_age=%dms | %s",
                contract.asset, contract.direction, contract.duration_mins,
                market_price, model_prob, edge, confidence, book_age_ms, " | ".join(reasons),
            )
            return None

        if oracle_price:
            logger.debug(
                "Oracle price used for %s: %.4f (Coinbase: %.4f, diff=%.2f%%)",
                contract.asset, oracle_price, tick.price,
                abs(oracle_price - tick.price) / tick.price * 100,
            )

        logger.info(
            "  ✓ %s %s %dmin: ask=%.3f model=%.3f edge=%.3f conf=%.2f book_age=%dms — TRADE",
            contract.asset, contract.direction, contract.duration_mins,
            market_price, model_prob, edge, confidence, book_age_ms,
        )

        return Opportunity(
            contract=contract,
            order_book=book,
            polymarket_price=market_price,
            model_prob=model_prob,
            edge=edge,
            confidence=confidence,
            coinbase_price=effective_price,
            price_change_pct=price_change_pct,
            direction=contract.direction,
        )

    def _scan_bundles(self, asset: str) -> list:
        """
        Bundle arbitrage: if UP.ask + DOWN.ask < 1 - fees, buying both guarantees profit.
        No price movement needed — works in flat markets.
        """
        all_contracts = self.polymarket.get_contracts()
        # Group by market (pair UP+DOWN from same condition)
        markets: dict = {}
        for c in all_contracts:
            if c.asset != asset or not c.active:
                continue
            base = c.condition_id.replace("-UP", "").replace("-DOWN", "")
            markets.setdefault(base, {})[c.direction] = c

        opportunities = []
        for base_id, dirs in markets.items():
            if "UP" not in dirs or "DOWN" not in dirs:
                continue
            up_c = dirs["UP"]
            down_c = dirs["DOWN"]

            up_book = self.polymarket._order_books.get(up_c.token_id)
            down_book = self.polymarket._order_books.get(down_c.token_id)
            if not up_book or not down_book:
                continue

            # Freshness check (max 4 seconds)
            if time.time() - up_book.timestamp > 4.0 or time.time() - down_book.timestamp > 4.0:
                continue

            # Per-leg sanity: both legs must be priced like active binary contracts.
            # $0.01 asks come from expired/illiquid markets with stale resting orders —
            # they produce fake 98% "guaranteed profit" and massive over-sized positions.
            if (up_book.best_ask < self.config.MIN_MARKET_PRICE or
                    down_book.best_ask < self.config.MIN_MARKET_PRICE or
                    up_book.best_ask > 1.0 - self.config.MIN_MARKET_PRICE or
                    down_book.best_ask > 1.0 - self.config.MIN_MARKET_PRICE):
                logger.debug(
                    "Bundle %s %dmin skipped: leg out of range (UP=%.3f DOWN=%.3f min=%.2f)",
                    asset, up_c.duration_mins,
                    up_book.best_ask, down_book.best_ask, self.config.MIN_MARKET_PRICE,
                )
                continue

            total_cost = up_book.best_ask + down_book.best_ask
            fees = total_cost * self.config.TAKER_FEE * 2
            guaranteed_profit = 1.0 - total_cost - fees

            if guaranteed_profit < self.config.BUNDLE_MIN_PROFIT:
                continue

            # Liquidity depth: ensure enough shares exist to fill a minimum-size trade.
            # Paper simulator assumes infinite depth — live trading does not.
            min_shares = self.config.MIN_TRADE_USDC / max(up_book.best_ask, 0.01)
            if (up_book.best_ask_size > 0 and up_book.best_ask_size < min_shares * 0.5) or \
               (down_book.best_ask_size > 0 and down_book.best_ask_size < min_shares * 0.5):
                logger.debug(
                    "  Bundle %s %dmin: low liquidity (UP depth=%.0f DOWN depth=%.0f need=%.0f)",
                    asset, up_c.duration_mins,
                    up_book.best_ask_size, down_book.best_ask_size, min_shares * 0.5,
                )
                continue

            # Represent as UP opportunity with special flag; trading engine handles both legs
            opp = Opportunity(
                contract=up_c,
                order_book=up_book,
                polymarket_price=total_cost,
                model_prob=1.0,
                edge=guaranteed_profit,
                confidence=1.0,
                coinbase_price=self.feed.latest(asset).price if self.feed.latest(asset) else 0.0,
                price_change_pct=0.0,
                direction="BUNDLE",
                is_bundle=True,
            )
            # Attach down contract info via a custom attribute
            opp._bundle_down_contract = down_c
            opp._bundle_down_book = down_book
            opportunities.append(opp)
            logger.info(
                "BUNDLE arb %s: UP=%.3f + DOWN=%.3f = %.3f  profit=%.1f%%",
                asset, up_book.best_ask, down_book.best_ask, total_cost, guaranteed_profit * 100,
            )

        return opportunities

    # ── Static helpers (no self dependency — fixes the NoneType bug) ──────────

    @staticmethod
    def _estimate_up_probability(
        price_change_pct: float,
        duration_mins: int,
        tick: PriceTick,
        book: OrderBook,
        momentum_weight: float = 0.60,
        book_weight: float = 0.40,
        price_to_beat: float = 0.0,
        elapsed_secs: int = 0,
        annual_vol: float = 0.80,
    ) -> float:
        """
        P(price_at_expiry >= price_to_beat) using log-normal model.

        When price_to_beat is known (primary model):
          - 50% price-to-beat delta: N(log(current/target) / vol_remaining)
          - 30% momentum: recent Coinbase drift
          - 20% order book: market's implied probability

        Fallback (no price_to_beat yet):
          - Original momentum + order book model
        """
        remaining_secs = max(10, duration_mins * 60 - elapsed_secs)
        remaining_years = remaining_secs / (365.25 * 24 * 3600)
        vol = annual_vol * math.sqrt(remaining_years)

        # Order book implied probability (market consensus)
        prob_book = max(0.05, min(0.95, 1.0 - book.best_ask))

        if price_to_beat > 0 and tick.price > 0 and vol > 0:
            # Primary signal: how far current price is from the target
            # Positive log_delta → already above target → favours UP
            log_delta = math.log(tick.price / price_to_beat)
            z_ptb = log_delta / vol
            prob_ptb = ArbitrageEngine._normal_cdf(z_ptb)

            # Secondary: short-term momentum confirms direction
            z_mom = (price_change_pct * 0.5) / vol
            prob_mom = ArbitrageEngine._normal_cdf(z_mom)

            prob = 0.50 * prob_ptb + 0.30 * prob_mom + 0.20 * prob_book
        else:
            # Fallback: original model (price_to_beat not yet captured)
            vol_dur = annual_vol * math.sqrt(duration_mins / (365 * 24 * 60))
            z_mom = (price_change_pct * 0.5) / vol_dur if vol_dur > 0 else 0.0
            prob_mom = ArbitrageEngine._normal_cdf(z_mom)
            prob = momentum_weight * prob_mom + book_weight * prob_book

        return max(0.05, min(0.95, prob))

    @staticmethod
    def _confidence_score(
        price_change_pct: float,
        edge: float,
        book: OrderBook,
        duration_mins: int,
    ) -> float:
        """Composite confidence 0–1."""
        magnitude   = min(abs(price_change_pct) / 0.001, 1.0)
        edge_score  = min(abs(edge) / 0.20, 1.0)
        spread_score = max(0.0, 1.0 - book.spread / 0.10)
        duration_mult = 1.0 if duration_mins >= 15 else 0.92

        raw = (0.40 * magnitude + 0.35 * edge_score + 0.25 * spread_score) * duration_mult
        return max(0.0, min(1.0, raw))

    @staticmethod
    def _normal_cdf(z: float) -> float:
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

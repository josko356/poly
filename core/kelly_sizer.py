"""
core/kelly_sizer.py — Half-Kelly Criterion pozicioniranje velicine.

Za binarne Polymarket ugovore:
  - b   = neto omjer isplate (payout / stake - 1). YES token od $0.40 koji isplacuje $1 → b = (1/0.40) - 1 = 1.5
  - p   = procijenjena vjerojatnost pobjede
  - q   = 1 - p
  - Kelly frakcija f* = (p*b - q) / b
  - Koristimo half-Kelly: f = f* * 0.5

Rezultat je ogranicen na max_position_pct ukupnog portfelja.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    kelly_fraction: float   # sirova puna Kelly frakcija
    half_kelly: float       # half-Kelly frakcija
    position_pct: float     # konacni udio portfelja koji se riskira
    usdc_amount: float      # iznos u dolarima za potrositi
    shares: float           # broj ugovora za kupiti
    entry_price: float      # cijena po dionici
    expected_value: float   # EV transakcije u USDC


class KellySizer:
    def __init__(self, config):
        self.kelly_fraction = config.KELLY_FRACTION        # 0.5 = half-Kelly
        self.max_position_pct = config.MAX_POSITION_PCT    # 0.08 = 8%
        self.min_trade_usdc = config.MIN_TRADE_USDC        # minimalno $5

    def size(
        self,
        portfolio_balance: float,
        model_prob: float,
        entry_price: float,      # cijena po dionici (0–1)
    ) -> SizingResult:
        """
        Izracun optimalne velicine pozicije pomocu half-Kelly kriterija.

        Argumenti:
            portfolio_balance:  ukupni dostupni USDC
            model_prob:         procijenjena vjerojatnost pobjede
            entry_price:        cijena po dionici ugovora (npr. 0.42)
        """
        if entry_price <= 0:
            logger.warning("Kelly sizer pozvan s entry_price=%.4f — vraca nultu velicinu", entry_price)
            return SizingResult(0.0, 0.0, 0.0, 0.0, 0.0, entry_price, 0.0)

        p = max(0.001, min(0.999, model_prob))
        q = 1.0 - p

        # Isplata: $1 po dionici, placamo `entry_price`, neto dobitak = 1 - entry_price
        b = (1.0 / entry_price) - 1.0   # neto omjer isplate

        # Puni Kelly
        kelly = (p * b - q) / b if b > 0 else 0.0
        kelly = max(0.0, kelly)          # nikad negativno

        # Half-Kelly
        half_kelly = kelly * self.kelly_fraction

        # Ogranici na maksimalnu velicinu pozicije
        position_pct = min(half_kelly, self.max_position_pct)

        # Iznos u dolarima
        usdc_amount = portfolio_balance * position_pct
        usdc_amount = max(self.min_trade_usdc, usdc_amount)  # primijeni minimum
        usdc_amount = min(usdc_amount, portfolio_balance * self.max_position_pct)

        # Dionice
        shares = usdc_amount / entry_price if entry_price > 0 else 0.0

        # Ocekivana vrijednost
        ev = shares * (p * (1.0 - entry_price) - q * entry_price)

        logger.debug(
            "Kelly sizing: p=%.3f b=%.2f f*=%.3f half-f=%.3f "
            "size=%.1f%% USDC=%.2f EV=%.2f",
            p, b, kelly, half_kelly,
            position_pct * 100, usdc_amount, ev,
        )

        return SizingResult(
            kelly_fraction=kelly,
            half_kelly=half_kelly,
            position_pct=position_pct,
            usdc_amount=usdc_amount,
            shares=shares,
            entry_price=entry_price,
            expected_value=ev,
        )

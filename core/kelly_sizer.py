"""
core/kelly_sizer.py — Half-Kelly Criterion position sizing.

For binary Polymarket contracts:
  - b   = net odds (payout / stake - 1). A $0.40 YES token that pays $1 → b = (1/0.40) - 1 = 1.5
  - p   = estimated probability of winning
  - q   = 1 - p
  - Kelly fraction f* = (p*b - q) / b
  - We use half-Kelly: f = f* * 0.5

The result is capped at max_position_pct of the total portfolio.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    kelly_fraction: float   # raw full-Kelly fraction
    half_kelly: float       # half-Kelly fraction
    position_pct: float     # final fraction of portfolio to risk
    usdc_amount: float      # dollar amount to spend
    shares: float           # number of contracts to buy
    entry_price: float      # price per share
    expected_value: float   # EV of the trade in USDC


class KellySizer:
    def __init__(self, config):
        self.kelly_fraction = config.KELLY_FRACTION        # 0.5 = half-Kelly
        self.max_position_pct = config.MAX_POSITION_PCT    # 0.08 = 8%
        self.min_trade_usdc = config.MIN_TRADE_USDC        # $5 minimum

    def size(
        self,
        portfolio_balance: float,
        model_prob: float,
        entry_price: float,      # price per share (0–1)
    ) -> SizingResult:
        """
        Calculate optimal position size using half-Kelly Criterion.

        Args:
            portfolio_balance:  total USDC available
            model_prob:         estimated probability of winning
            entry_price:        price per contract share (e.g. 0.42)
        """
        if entry_price <= 0:
            logger.warning("Kelly sizer called with entry_price=%.4f — returning zero size", entry_price)
            return SizingResult(0.0, 0.0, 0.0, 0.0, 0.0, entry_price, 0.0)

        p = max(0.001, min(0.999, model_prob))
        q = 1.0 - p

        # Payout: $1 per share, we pay `entry_price`, net gain = 1 - entry_price
        b = (1.0 / entry_price) - 1.0   # net odds ratio

        # Full Kelly
        kelly = (p * b - q) / b if b > 0 else 0.0
        kelly = max(0.0, kelly)          # never negative

        # Half-Kelly
        half_kelly = kelly * self.kelly_fraction

        # Cap at max position size
        position_pct = min(half_kelly, self.max_position_pct)

        # Dollar amount
        usdc_amount = portfolio_balance * position_pct
        usdc_amount = max(self.min_trade_usdc, usdc_amount)  # enforce minimum
        usdc_amount = min(usdc_amount, portfolio_balance * self.max_position_pct)

        # Shares
        shares = usdc_amount / entry_price if entry_price > 0 else 0.0

        # Expected value
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

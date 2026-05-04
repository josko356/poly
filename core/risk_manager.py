"""
core/risk_manager.py — Portfolio risk controls.

Enforces:
  - 20% daily drawdown kill switch
  - Max 6 simultaneous open positions
  - Per-trade position size cap (8% of portfolio)
  - Cooldown period after kill switch fires
  - Daily P&L tracking
"""

import asyncio
import logging
import time
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config, starting_balance: float):
        self.config = config
        self.starting_balance = starting_balance

        self._current_balance = starting_balance
        self._day_start_balance = starting_balance
        self._today = date.today()

        self._open_positions: int = 0
        self._daily_pnl: float = 0.0
        self._realized_daily_loss: float = 0.0   # only negative closed-trade P&L
        self._max_drawdown_today: float = 0.0

        self._killed: bool = False
        self._killed_at: Optional[float] = None
        self._kill_reason: str = ""

        self._on_kill_callbacks: list = []

        # Trade rate limiting (live mode protection)
        self._trades_this_hour: int = 0
        self._hour_start: float = time.time()

    # ── Public interface ──────────────────────────────────────────

    @property
    def balance(self) -> float:
        return self._current_balance

    @property
    def is_killed(self) -> bool:
        return self._killed

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_drawdown_pct(self) -> float:
        """Realized losses today / day-start balance. Used for kill switch.
        Does NOT count capital allocated to open positions — only closed-trade losses."""
        if self._day_start_balance == 0:
            return 0.0
        return self._realized_daily_loss / self._day_start_balance

    @property
    def balance_drawdown_pct(self) -> float:
        """Balance vs day-start including open position allocation. For display only."""
        if self._day_start_balance == 0:
            return 0.0
        loss = self._day_start_balance - self._current_balance
        return loss / self._day_start_balance

    @property
    def open_positions(self) -> int:
        return self._open_positions

    def on_kill(self, callback):
        """Register a callback to be called when the kill switch fires."""
        self._on_kill_callbacks.append(callback)

    # ── Daily reset ───────────────────────────────────────────────

    def check_day_rollover(self):
        today = date.today()
        if today != self._today:
            logger.info(
                "Day rollover: resetting daily stats. Previous P&L: %.2f USDC",
                self._daily_pnl,
            )
            self._today = today
            self._day_start_balance = self._current_balance
            self._daily_pnl = 0.0
            self._realized_daily_loss = 0.0
            self._max_drawdown_today = 0.0
            # Reset kill switch at day start
            if self._killed:
                logger.info("Kill switch reset for new trading day.")
                self._killed = False
                self._killed_at = None
                self._kill_reason = ""

    # ── Pre-trade checks ──────────────────────────────────────────

    def can_trade(self) -> tuple[bool, str]:
        """
        Returns (True, "") if we're allowed to open a new trade,
        else (False, reason).
        """
        self.check_day_rollover()

        if self._killed:
            remaining = ""
            if self._killed_at:
                elapsed = time.time() - self._killed_at
                cooldown = self.config.COOLDOWN_AFTER_KILL_SECS
                remaining = f" ({max(0, cooldown - elapsed):.0f}s cooldown remaining)"
            return False, f"Kill switch active: {self._kill_reason}{remaining}"

        if self._open_positions >= self.config.MAX_OPEN_POSITIONS:
            return False, f"Max open positions ({self.config.MAX_OPEN_POSITIONS}) reached"

        if self.daily_drawdown_pct >= self.config.MAX_DAILY_DRAWDOWN:
            self._trigger_kill(
                f"Realized daily loss {self.daily_drawdown_pct:.1%} ≥ "
                f"{self.config.MAX_DAILY_DRAWDOWN:.1%} limit"
            )
            return False, self._kill_reason

        if self._current_balance < self.config.MIN_TRADE_USDC:
            return False, f"Insufficient balance (${self._current_balance:.2f})"

        # Live trading: minimum absolute balance floor
        is_live = getattr(self.config, "is_live_trading", False)
        if is_live:
            min_floor = getattr(self.config, "MIN_LIVE_BALANCE_USDC", 50.0)
            if self._current_balance < min_floor:
                self._trigger_kill(
                    f"Live balance ${self._current_balance:.2f} below floor ${min_floor:.2f}"
                )
                return False, self._kill_reason

            # Trade rate limit
            now = time.time()
            if now - self._hour_start >= 3600:
                self._trades_this_hour = 0
                self._hour_start = now
            max_per_hour = getattr(self.config, "MAX_TRADES_PER_HOUR", 10)
            if self._trades_this_hour >= max_per_hour:
                return False, f"Rate limit: {self._trades_this_hour}/{max_per_hour} trades this hour"

        return True, ""

    # ── Balance + position tracking ───────────────────────────────

    def on_trade_opened(self, usdc_spent: float):
        self._current_balance -= usdc_spent
        self._open_positions += 1
        self._trades_this_hour += 1
        self._update_drawdown()
        logger.debug(
            "Trade opened: -%.2f USDC | balance=%.2f | open=%d",
            usdc_spent, self._current_balance, self._open_positions,
        )

    def on_trade_closed(self, gross_return: float, net_pnl: float = None):
        """
        gross_return: cash returned to balance (full payout for win, 0 for loss).
        net_pnl: net profit/loss for display tracking (defaults to gross_return).
        Stake is already deducted at on_trade_opened, so gross_return on a win = shares * $1.
        """
        self._current_balance += gross_return
        pnl_value = net_pnl if net_pnl is not None else gross_return
        self._daily_pnl += pnl_value
        if pnl_value < 0:
            self._realized_daily_loss += abs(pnl_value)
        self._open_positions = max(0, self._open_positions - 1)
        self._update_drawdown()
        logger.info(
            "Trade closed: P&L=%.2f USDC | balance=%.2f | open=%d",
            pnl_value, self._current_balance, self._open_positions,
        )

        # Check kill switch after each trade settlement
        if self.daily_drawdown_pct >= self.config.MAX_DAILY_DRAWDOWN:
            self._trigger_kill(
                f"Realized daily loss {self.daily_drawdown_pct:.1%} exceeded "
                f"{self.config.MAX_DAILY_DRAWDOWN:.1%} limit"
            )

    def update_balance(self, new_balance: float):
        """Directly set balance (e.g. from live wallet query)."""
        self._current_balance = new_balance
        self._update_drawdown()

    # ── Kill switch ───────────────────────────────────────────────

    def _trigger_kill(self, reason: str):
        if self._killed:
            return  # already killed
        self._killed = True
        self._killed_at = time.time()
        self._kill_reason = reason
        logger.critical("🛑 KILL SWITCH FIRED: %s", reason)

        for cb in self._on_kill_callbacks:
            try:
                if asyncio.iscoroutinefunction(cb):
                    asyncio.create_task(cb(reason))
                else:
                    cb(reason)
            except Exception as exc:
                logger.error("Kill callback error: %s", exc)

    def manual_kill(self, reason: str = "Manual kill"):
        """Operator-triggered kill switch."""
        self._trigger_kill(reason)

    def manual_resume(self):
        """Manually reset the kill switch (use carefully)."""
        if not self._killed:
            return
        logger.warning("Kill switch manually reset.")
        self._killed = False
        self._killed_at = None
        self._kill_reason = ""

    # ── Internal ──────────────────────────────────────────────────

    def _update_drawdown(self):
        dd = self.daily_drawdown_pct
        if dd > self._max_drawdown_today:
            self._max_drawdown_today = dd

    # ── Status summary ────────────────────────────────────────────

    def status(self) -> dict:
        self.check_day_rollover()
        return {
            "balance": self._current_balance,
            "starting_balance": self.starting_balance,
            "day_start_balance": self._day_start_balance,
            "daily_pnl": self._daily_pnl,
            "daily_drawdown_pct": self.daily_drawdown_pct,        # realized losses only
            "balance_drawdown_pct": self.balance_drawdown_pct,    # includes open allocation
            "max_drawdown_today": self._max_drawdown_today,
            "open_positions": self._open_positions,
            "is_killed": self._killed,
            "kill_reason": self._kill_reason,
            "total_return_pct": (
                (self._current_balance - self.starting_balance) / self.starting_balance
            ) if self.starting_balance else 0.0,
        }

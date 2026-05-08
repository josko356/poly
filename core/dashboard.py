"""
core/dashboard.py — Live terminal dashboard (Textual TUI).

Layout:
  ┌─ STATUS ──────────────────────────────────────────────────────────────┐
  │  mode | balance | pnl | total return | drawdown | win rate | clock   │
  ├─ LIVE PRICES ────┬─ OPEN POSITIONS ──────────────────┬─ RISK ────────┤
  │  Coinbase WS     │  Active binary contracts           │  Limits       │
  ├──────────────────┴───────────────────────────────────┴───────────────┤
  │  TRADE HISTORY — last 10 closed positions this session               │
  ├───────────────────────────────────────────────────────────────────────┤
  │  [Q] Quit  [K] Kill  [R] Resume  [D] Daily summary                  │
  └───────────────────────────────────────────────────────────────────────┘
"""

import asyncio
import logging
import sys
import time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_CSS = """
Screen {
    background: #07111e;
    color: #c5d8f0;
    layout: vertical;
}

#status-bar {
    height: 5;
    background: #0c1e30;
    border: tall #1b3d5c;
    padding: 0 2;
    content-align: left top;
}

#status-bar.killed {
    border: tall #8b0000;
    background: #1a0505;
}

#middle-row {
    height: 16;
    layout: horizontal;
}

.mid-panel {
    background: #0c1e30;
    border: tall #1b3d5c;
    padding: 0 1;
}

#prices-panel  { width: 1fr; }
#positions-panel { width: 2fr; }
#risk-panel    { width: 1fr; }

#trades-panel {
    height: 16;
    background: #0c1e30;
    border: tall #1b3d5c;
    padding: 0 1;
}

#footer-bar {
    height: 3;
    background: #0a1828;
    border: tall #1b3d5c;
    padding: 0 2;
    content-align: left middle;
}
"""


# ── Widget helpers ────────────────────────────────────────────────────────────

def _clr(text: str, color: str) -> str:
    return f"[{color}]{text}[/]"

def _dim(text: str) -> str:
    return f"[dim]{text}[/]"

def _bold(text: str, color: str = "white") -> str:
    return f"[bold {color}]{text}[/]"


# ── Panel widgets ─────────────────────────────────────────────────────────────

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal
    from textual.widgets import Static
    _TEXTUAL_OK = True
except ImportError:
    _TEXTUAL_OK = False


if _TEXTUAL_OK:

    class _StatusBar(Static):
        def __init__(self, dash, **kw):
            super().__init__("", markup=True, **kw)
            self._d = dash

        def tick(self):
            try:
                self.update(self._build())
                rs = self._d.risk.status()
                if rs["is_killed"]:
                    self.add_class("killed")
                else:
                    self.remove_class("killed")
            except Exception:
                pass

        def _build(self) -> str:
            rs   = self._d.risk.status()
            eng  = self._d.engine
            poly = self._d.polymarket
            cfg  = self._d.config

            mode    = eng.mode.upper()
            killed  = rs["is_killed"]
            bal     = rs["balance"]
            pnl     = rs["daily_pnl"]
            dd      = rs["daily_drawdown_pct"]
            bal_dd  = rs["balance_drawdown_pct"]
            alloc   = sum(p.paper_usdc_spent for p in eng.open_positions)
            start   = rs["starting_balance"]
            total_r = (bal + alloc - start) / start if start else 0.0
            total   = self._d._stats_cache.get("total", 0)
            wr      = self._d._stats_cache.get("win_rate", 0.0)
            now     = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            n_c     = len(poly.get_contracts())
            n_open  = rs["open_positions"]

            if killed:
                line1 = (
                    _bold("  KILL SWITCH ACTIVE  ", "red") + "  " +
                    _clr(rs["kill_reason"], "red")
                )
            else:
                mc  = "green"  if mode == "LIVE" else "cyan"
                pc  = "green"  if pnl    >= 0 else "red"
                ps  = "+"      if pnl    >= 0 else ""
                rc  = "green"  if total_r >= 0 else "red"
                rs2 = "+"      if total_r >= 0 else ""
                dc  = "red"    if dd > 0.10 else "yellow" if dd > 0.05 else "green"
                alloc_note = _dim(f" (alloc {bal_dd:.1%})") if bal_dd > 0.01 else ""

                line1 = (
                    _bold(f"* {mode}", mc) + "  " +
                    _dim("|") + "  " +
                    _dim("Balance") + "  " + _bold(f"${bal:,.2f} USDC") + "  " +
                    _dim("|") + "  " +
                    _dim("P&L Today") + "  " + _clr(f"{ps}{pnl:.2f}", pc) + "  " +
                    _dim("|") + "  " +
                    _dim("Total Return") + "  " + _clr(f"{rs2}{total_r:.1%}", rc) + "  " +
                    _dim("|") + "  " +
                    _dim("Realized DD") + "  " + _clr(f"{dd:.1%}/20%", dc) +
                    alloc_note + "  " +
                    _dim("|") + "  " +
                    _clr(f"Win {wr:.1%}  ({total} trades)", "bold yellow")
                )

            line2 = (
                _dim(f"  Contracts monitored: {n_c}") + "  " +
                _dim("|") + "  " +
                _dim(f"Open positions: {n_open}") + "  " +
                _dim("|") + "  " +
                _dim(f"{now} UTC")
            )
            return line1 + "\n\n" + line2


    class _PricesPanel(Static):
        def __init__(self, dash, **kw):
            super().__init__("", markup=True, **kw)
            self._d = dash

        def tick(self):
            try:
                self.update(self._build())
            except Exception:
                pass

        def _build(self) -> str:
            cfg  = self._d.config
            feed = self._d.feed
            rows = [
                _bold("LIVE PRICES", "#4da6e8"),
                _dim("Coinbase WebSocket — oracle cross-reference"),
                "",
                _dim(f"{'Asset':<6}  {'Price':>12}  {'10s Chg':>8}  {'Live':>4}"),
                _dim(f"{'─'*6}  {'─'*12}  {'─'*8}  {'─'*4}"),
            ]
            for asset in [a.split("-")[0] for a in cfg.ASSETS]:
                tick = feed.latest(asset)
                if tick:
                    chg = feed.price_change_pct(asset, 10) * 100
                    s   = "+" if chg >= 0 else ""
                    cc  = "green" if chg > 0 else "red" if chg < 0 else "white"
                    fr  = feed.is_fresh(asset, 3.0)
                    fc  = "green" if fr else "red"
                    rows.append(
                        f"[cyan]{asset:<6}[/]  [white]${tick.price:>11,.2f}[/]  "
                        f"[{cc}]{s}{chg:>6.3f}%[/]  [{fc}]{'✓' if fr else '✗':>4}[/]"
                    )
                else:
                    rows.append(f"[cyan]{asset:<6}[/]  [dim]{'–':>12}  {'–':>8}  [red]✗[/][/]")
            return "\n".join(rows)


    class _PositionsPanel(Static):
        def __init__(self, dash, **kw):
            super().__init__("", markup=True, **kw)
            self._d = dash

        def tick(self):
            try:
                self.update(self._build())
            except Exception:
                pass

        def _build(self) -> str:
            rows = [
                _bold("OPEN POSITIONS", "#4da6e8"),
                _dim("Active binary contracts — waiting for oracle resolution"),
                "",
                _dim(f"{'Asset':<5}  {'Dir':<6}  {'Min':>3}  {'Entry':>6}  {'Edge':>6}  {'Conf':>6}  {'$Size':>7}  {'Expires':>8}"),
                _dim(f"{'─'*5}  {'─'*6}  {'─'*3}  {'─'*6}  {'─'*6}  {'─'*6}  {'─'*7}  {'─'*8}"),
            ]
            positions = self._d.engine.open_positions
            for pos in positions:
                opp = pos.opportunity
                rem = max(0, pos.expected_expiry - time.time())
                dc  = "green" if opp.direction == "UP" else "red"
                dir_str = opp.direction if not opp.is_bundle else "BUNDLE"
                rows.append(
                    f"[cyan]{opp.contract.asset:<5}[/]  "
                    f"[{dc}]{dir_str:<6}[/]  "
                    f"{opp.contract.duration_mins:>3}  "
                    f"{pos.paper_entry_price:>6.3f}  "
                    f"[yellow]{opp.edge:>6.1%}[/]  "
                    f"[white]{opp.confidence:>6.1%}[/]  "
                    f"[bold]${pos.paper_usdc_spent:>6.1f}[/]  "
                    f"[dim]{rem:>7.0f}s[/]"
                )
            if not positions:
                rows.append(_dim("  — No open positions —"))
            return "\n".join(rows)


    class _RiskPanel(Static):
        def __init__(self, dash, **kw):
            super().__init__("", markup=True, **kw)
            self._d = dash

        def tick(self):
            try:
                self.update(self._build())
            except Exception:
                pass

        def _build(self) -> str:
            rs   = self._d.risk.status()
            cfg  = self._d.config
            live = getattr(cfg, "is_live_trading", False)
            dd   = rs["daily_drawdown_pct"]
            bal  = rs["balance_drawdown_pct"]
            lim  = cfg.MAX_DAILY_DRAWDOWN
            bar_len = 12
            filled  = min(int((dd / lim) * bar_len), bar_len) if lim > 0 else 0
            dc = "red" if dd > lim * 0.7 else "yellow" if dd > lim * 0.4 else "green"
            bar = f"[{dc}]{'█' * filled}[/][dim]{'░' * (bar_len - filled)}[/]"
            alloc = f"\n  {_dim(f'balance alloc: {bal:.1%}')}" if bal > 0.01 else ""

            if live:
                max_trade_str = f"[white]${getattr(cfg, 'MAX_LIVE_TRADE_USDC', 0):.2f} ({getattr(cfg, 'MAX_LIVE_TRADE_PCT', 0):.0%})[/]"
                floor_str     = f"[white]${getattr(cfg, 'MIN_LIVE_BALANCE_USDC', 0):.2f} ({getattr(cfg, 'MIN_LIVE_BALANCE_PCT', 0):.0%})[/]"
            else:
                max_trade_str = f"[white]{cfg.MAX_POSITION_PCT:.0%} of portfolio[/]"
                floor_str     = _dim("n/a (paper mode)")

            rows = [
                _bold("RISK CONTROLS", "#4da6e8"),
                _dim(f"Portfolio protection — kill switch at {lim:.0%} realized loss"),
                "",
                _dim(f"Realized daily loss  (fires kill switch at {lim:.0%})"),
                f"  {bar} [{dc}]{dd:.1%}/{lim:.0%}[/]{alloc}",
                "",
                _dim("Open positions") + "    " + f"[white]{rs['open_positions']}/{cfg.MAX_OPEN_POSITIONS}[/]",
                _dim("Max trade size") + "    " + max_trade_str,
                _dim("Balance floor") + "     " + floor_str,
                _dim("Min signal edge") + "   " + f"[white]{cfg.MIN_EDGE:.0%}[/]",
                _dim("Min confidence") + "    " + f"[white]{cfg.MIN_CONFIDENCE:.0%}[/]",
                _dim("Kelly fraction") + "    " + f"[white]½  ({cfg.KELLY_FRACTION:.0%})[/]",
                _dim("Taker fee") + "         " + f"[white]{cfg.TAKER_FEE:.2%}[/]",
            ]
            return "\n".join(rows)


    class _TradesPanel(Static):
        def __init__(self, dash, **kw):
            super().__init__("", markup=True, **kw)
            self._d = dash

        def tick(self):
            try:
                self.update(self._build())
            except Exception:
                pass

        def _build(self) -> str:
            rows = [
                _bold("TRADE HISTORY", "#4da6e8"),
                _dim("Last 10 closed positions this session — resets on each bot restart"),
                "",
                _dim(f"{'#':>4}  {'Asset':<5}  {'Dir':<6}  {'Min':>3}  {'Entry':>6}  {'P&L':>9}  {'Result':>6}  {'Time':<16}"),
                _dim(f"{'─'*4}  {'─'*5}  {'─'*6}  {'─'*3}  {'─'*6}  {'─'*9}  {'─'*6}  {'─'*16}"),
            ]
            recent = self._d.engine.recent_trades
            for t in reversed(recent[-10:]):
                pnl    = t.get("pnl", 0) or 0
                pc     = "green" if pnl >= 0 else "red"
                ps     = "+" if pnl >= 0 else ""
                status = t.get("status", "")
                se     = "[green]  WON[/]" if status == "won" else "[red] LOST[/]"
                ts     = t.get("timestamp", "")[:16]
                dc     = "green" if t.get("direction") == "UP" else "red"
                dir_s  = str(t.get("direction", ""))
                rows.append(
                    f"[dim]{str(t.get('id', '')):>4}[/]  "
                    f"[cyan]{str(t.get('asset', '')):>5}[/]  "
                    f"[{dc}]{dir_s:<6}[/]  "
                    f"{str(t.get('duration', '')):>3}  "
                    f"{t.get('entry_price', 0):>6.3f}  "
                    f"[{pc}]{ps}{pnl:>8.2f}[/]  "
                    f"{se}  "
                    f"[dim]{ts:<16}[/]"
                )
            if not recent:
                rows.append(_dim("  — No trades this session —"))
            return "\n".join(rows)


    class _FooterBar(Static):
        def __init__(self, cfg, **kw):
            super().__init__("", markup=True, **kw)
            self._cfg = cfg

        def on_mount(self):
            mode_note = (
                _bold("  ●  PAPER TRADING — safe to test", "cyan")
                if not self._cfg.is_live_trading
                else _bold("  ●  LIVE TRADING ACTIVE", "green")
            )
            self.update(
                _dim("[Q]") + " Quit   " +
                _dim("[K]") + " Kill Switch   " +
                _dim("[R]") + " Resume Trading   " +
                _dim("[D]") + " Daily Summary" +
                mode_note
            )


    class _BotApp(App):
        CSS = _CSS

        BINDINGS = [
            Binding("q", "quit", "Quit", show=False),
            Binding("k", "kill_switch", "Kill", show=False),
            Binding("r", "resume", "Resume", show=False),
            Binding("d", "daily_summary", "Daily", show=False),
        ]

        def __init__(self, dash_ref):
            super().__init__()
            self._d = dash_ref

        def compose(self) -> ComposeResult:
            yield _StatusBar(self._d, id="status-bar")
            with Horizontal(id="middle-row"):
                yield _PricesPanel(self._d, id="prices-panel",    classes="mid-panel")
                yield _PositionsPanel(self._d, id="positions-panel", classes="mid-panel")
                yield _RiskPanel(self._d, id="risk-panel",       classes="mid-panel")
            yield _TradesPanel(self._d, id="trades-panel")
            yield _FooterBar(self._d.config, id="footer-bar")

        def on_mount(self) -> None:
            self.set_interval(1.0, self._tick)

        def _tick(self) -> None:
            self.query_one(_StatusBar).tick()
            self.query_one(_PricesPanel).tick()
            self.query_one(_PositionsPanel).tick()
            self.query_one(_RiskPanel).tick()
            self.query_one(_TradesPanel).tick()

        def action_kill_switch(self) -> None:
            self._d.risk.manual_kill("Operator kill via dashboard")

        def action_resume(self) -> None:
            self._d.risk.manual_resume()

        async def action_daily_summary(self) -> None:
            rs = self._d.risk.status()
            sc = self._d._stats_cache
            logger.info(
                "Daily summary | balance=%.2f pnl=%.2f win=%.1f%% trades=%d",
                rs["balance"], rs["daily_pnl"],
                sc.get("win_rate", 0) * 100, sc.get("total", 0),
            )


# ── Public Dashboard class (same interface as the Rich version) ───────────────

class Dashboard:
    """
    Textual-based live dashboard.
    Falls back to a no-op stub if Textual is not installed.
    """

    def __init__(self, config, feed, polymarket, risk, engine, db):
        self.config     = config
        self.feed       = feed
        self.polymarket = polymarket
        self.risk       = risk
        self.engine     = engine
        self.db         = db
        self._stats_cache: dict = {}
        self._last_stats_update = 0.0
        self._task: Optional[asyncio.Task] = None

        if _TEXTUAL_OK:
            self._app: Optional[_BotApp] = _BotApp(self)
        else:
            self._app = None
            logger.warning(
                "Textual not installed — dashboard disabled.  "
                "Run: pip install textual>=0.60"
            )

    async def start(self):
        if self._app is None:
            return
        # Suppress stdout logging — Textual owns the terminal
        _mute_console_logging()
        self._task = asyncio.create_task(self._app.run_async())

    async def stop(self):
        if self._app is not None:
            try:
                self._app.exit()
            except Exception:
                pass
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def refresh_stats(self):
        try:
            trades = self.engine.session_trades
            closed = [t for t in trades if t.get("status") in ("won", "lost", "exited")]
            wins   = [t for t in closed if t.get("status") == "won"]
            self._stats_cache = {
                "total":     len(closed),
                "wins":      len(wins),
                "losses":    len(closed) - len(wins),
                "win_rate":  len(wins) / len(closed) if closed else 0.0,
                "total_pnl": sum(t.get("pnl") or 0 for t in closed),
            }
        except Exception:
            pass


def _mute_console_logging():
    """Remove StreamHandlers from the root logger so Textual isn't corrupted by log writes."""
    import sys
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and h.stream in (sys.stdout, sys.stderr):
            root.removeHandler(h)
            logger.debug("Console log handler removed (Textual dashboard active)")

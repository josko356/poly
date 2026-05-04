"""
main.py — Polymarket Latency Arbitrage Bot (Windows)
=====================================================

Usage:
    python main.py              # Paper trading (default, safe)
    python main.py --stats      # Show trade history and exit
    python main.py --check      # Check active contracts and exit

To enable LIVE trading, set all three flags in your .env:
    LIVE_TRADING_ENABLED=true
    LIVE_TRADING_CONFIRMED=true
    LIVE_TRADING_RISK_ACKNOWLEDGED=true
"""

import asyncio
import logging
import sys
import os
import subprocess
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import aiohttp

# ── Windows asyncio fix ───────────────────────────────────────────────────────
# Potrebno na Windowsu da se izbjegne "RuntimeError: Event loop is closed"
# s websockets i aiohttp. Mora se postaviti PRIJE bilo koje asyncio upotrebe.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from config import Config
from core.chainlink_feed import ChainlinkFeed
from core.coinbase_feed import CoinbaseFeed
from core.database import Database
from core.arbitrage_engine import ArbitrageEngine
from core.polymarket_client import PolymarketClient
from core.risk_manager import RiskManager
from core.trading_engine import TradingEngine
from core.telegram_alerts import TelegramBot
from core.dashboard import Dashboard

# ── Logiranje ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(
            "bot.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

_PID_FILE = Path(__file__).parent / "bot.pid"

# Native USDC na Polygonu (koristi Polymarket CLOB)
_USDC_CONTRACT  = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
_USDC_DECIMALS  = 1_000_000  # 6 decimalnih mjesta
_POLYGON_RPCS   = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://rpc-mainnet.matic.quiknode.pro",
]


async def _fetch_usdc_balance(address: str) -> Optional[float]:
    """
    Dohvati stvarni USDC balans na Polygonu putem javnog RPC-a.
    Poziva ERC-20 balanceOf(address) na native USDC ugovoru.
    Vraca None ako svi RPC-ovi zataje.
    """
    if not address or not address.startswith("0x"):
        return None
    padded = address[2:].lower().zfill(64)
    data = f"0x70a08231{padded}"   # balanceOf(address) selektor
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": _USDC_CONTRACT, "data": data}, "latest"],
        "id": 1,
    }
    timeout = aiohttp.ClientTimeout(total=5)
    for rpc in _POLYGON_RPCS:
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(rpc, json=payload) as resp:
                    if resp.status != 200:
                        continue
                    result = (await resp.json(content_type=None)).get("result", "")
                    if result and result != "0x":
                        return int(result, 16) / _USDC_DECIMALS
        except Exception:
            continue
    return None


# ── Zakljucavanje jedne instance ─────────────────────────────────────────────

def _pid_running(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=3,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _acquire_pid_lock():
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            if _pid_running(old_pid):
                print(f"\nERROR: Bot already running (PID {old_pid}).")
                print(f"       Stop it first: taskkill /PID {old_pid} /F\n")
                sys.exit(1)
        except Exception:
            pass
    _PID_FILE.write_text(str(os.getpid()))


def _release_pid_lock():
    try:
        _PID_FILE.unlink()
    except Exception:
        pass


# ── Live pre-flight provjera ──────────────────────────────────────────────────

async def _live_preflight(config, bot) -> bool:
    """Provjeri sve sustave prije nego sto pravi novac moze biti tradean."""
    print("\n" + "=" * 60)
    print("  LIVE TRADING PRE-FLIGHT CHECK")
    print("=" * 60)
    ok = True

    # 1. py-clob-client instaliran?
    try:
        import py_clob_client as _clob
        ver = getattr(_clob, "__version__", "unknown")
        print(f"  [OK] py-clob-client {ver}")
        if ver != "unknown":
            from packaging.version import Version
            if Version(ver) < Version("0.34.6"):
                print(f"  [WARN] v0.34.6+ recommended (V2 CLOB migration fix)")
    except ImportError:
        print("  [FAIL] py-clob-client not installed → pip install py-clob-client")
        ok = False

    # 2. Ugovori ucitani?
    contracts = bot.polymarket.get_contracts()
    if contracts:
        print(f"  [OK] {len(contracts)} contracts loaded from Polymarket")
    else:
        print("  [FAIL] No contracts loaded — check network/API")
        ok = False

    # 3. Order bookovi se pune?
    with_books = sum(1 for c in contracts if bot.polymarket._order_books.get(c.token_id))
    if with_books:
        print(f"  [OK] {with_books}/{len(contracts)} order books populated")
    else:
        print("  [WARN] Order books not yet populated (WS may still be connecting)")

    # 4. Nema sumnjivih order bookova po cijeni $0.01 (poznati bug)?
    bogus = [
        c for c in contracts
        if (book := bot.polymarket._order_books.get(c.token_id))
        and (book.best_ask < config.MIN_MARKET_PRICE or book.best_ask > 1 - config.MIN_MARKET_PRICE)
    ]
    if bogus:
        print(f"  [WARN] {len(bogus)} contracts with out-of-range prices — will be filtered by MIN_MARKET_PRICE")
    else:
        print(f"  [OK] All order book prices within valid range")

    # 5. Chainlink oracle aktivan?
    btc_oracle = bot.chainlink.get_price("BTC")
    if btc_oracle:
        print(f"  [OK] Chainlink BTC oracle: ${btc_oracle:,.2f}")
    else:
        print("  [WARN] Chainlink oracle not yet responding (non-critical, uses Coinbase fallback)")

    # 6. Coinbase feed aktivan?
    btc_tick = bot.feed.latest("BTC")
    if btc_tick:
        print(f"  [OK] Coinbase feed live: BTC=${btc_tick.price:,.2f}")
    else:
        print("  [FAIL] Coinbase price feed not active")
        ok = False

    # 7. Stvarni on-chain USDC balans + izracun dinamickih limita
    real_balance = await _fetch_usdc_balance(config.POLYGON_ADDRESS)
    if real_balance is not None:
        if real_balance == 0:
            print(f"  [FAIL] Polygon USDC balance: $0.00 — deposit USDC before trading")
            ok = False
        else:
            print(f"  [OK] Polygon USDC balance: ${real_balance:.2f} USDC")
            # Izvedi USDC hard limite iz stvarnog balansa
            config.MAX_LIVE_TRADE_USDC  = round(real_balance * config.MAX_LIVE_TRADE_PCT, 2)
            config.MIN_LIVE_BALANCE_USDC = round(real_balance * config.MIN_LIVE_BALANCE_PCT, 2)
        # Sinkroniziraj risk manager da prati stvarni novcanik, ne paper zadanu vrijednost
        bot.risk.update_balance(real_balance)
    else:
        print("  [WARN] Could not read on-chain USDC balance (RPC unreachable) — proceeding with caution")

    # 8. Sazetak sigurnosnih limita
    print(f"\n  Safety limits active:")
    if real_balance:
        print(f"    Max per-trade:    {config.MAX_LIVE_TRADE_PCT:.0%} of ${real_balance:.2f} = ${config.MAX_LIVE_TRADE_USDC:.2f} USDC")
        print(f"    Balance floor:    {config.MIN_LIVE_BALANCE_PCT:.0%} of ${real_balance:.2f} = ${config.MIN_LIVE_BALANCE_USDC:.2f} USDC")
    else:
        print(f"    Max per-trade:    {config.MAX_LIVE_TRADE_PCT:.0%} of balance")
        print(f"    Balance floor:    {config.MIN_LIVE_BALANCE_PCT:.0%} of balance")
    print(f"    Trades/hour cap:  {config.MAX_TRADES_PER_HOUR}")
    print(f"    Daily loss limit: {config.MAX_DAILY_DRAWDOWN:.0%}")
    print(f"    Max slippage:     {config.MAX_LIVE_SLIPPAGE_PCT:.1%}")

    if not ok:
        print("\n  [ABORT] Pre-flight FAILED — fix the above before trading.\n")
        return False

    delay = config.LIVE_STARTUP_DELAY_SECS
    print(f"\n  [PASS] All checks passed. Starting in {delay}s — Ctrl+C to abort.")
    print("=" * 60)
    for i in range(delay, 0, -1):
        print(f"  {i}s...", end="\r", flush=True)
        await asyncio.sleep(1)
    print()
    return True


# ── Glavni bot ───────────────────────────────────────────────────────────────

class PolymarketBot:
    def __init__(self, config: Config):
        self.config = config
        self._running = False
        self._stop_event = asyncio.Event()

        self.db = Database()
        self.feed = CoinbaseFeed(config=config, on_tick=self._on_price_tick)
        self.chainlink = ChainlinkFeed(config.ASSETS)
        self.polymarket = PolymarketClient(
            config=config,
            refresh_interval=config.CONTRACT_REFRESH_INTERVAL,
        )
        self.risk = RiskManager(config, config.PAPER_STARTING_BALANCE)
        self.telegram = TelegramBot(
            token=config.TELEGRAM_BOT_TOKEN,
            chat_id=config.TELEGRAM_CHAT_ID,
        )
        self.engine = TradingEngine(
            config=config,
            polymarket=self.polymarket,
            risk=self.risk,
            db=self.db,
            on_trade_open=self._on_trade_open,
            on_trade_close=self._on_trade_close,
        )
        self.arb = ArbitrageEngine(
            config=config,
            feed=self.feed,
            polymarket=self.polymarket,
            chainlink=self.chainlink,
        )
        self.dashboard = Dashboard(
            config=config,
            feed=self.feed,
            polymarket=self.polymarket,
            risk=self.risk,
            engine=self.engine,
            db=self.db,
        )

        self.risk.on_kill(self._on_kill_switch)
        self._scan_queue: asyncio.Queue = asyncio.Queue(maxsize=20)

        # Povezi Telegram remote naredbe
        self.telegram.on_kill_command   = self._on_telegram_kill
        self.telegram.on_resume_command = self._on_telegram_resume
        self.telegram.on_status_command = self._on_telegram_status

    # ── Zivotni ciklus ────────────────────────────────────────────

    async def start(self):
        self._running = True
        logger.info("=" * 60)
        logger.info("  POLYMARKET LATENCY ARBITRAGE BOT  [Windows]")
        logger.info("  Mode:    %s", "LIVE" if self.config.is_live_trading else "PAPER")
        logger.info("  Auth:    Coinbase %s", "JWT API key" if self.config.coinbase_auth_enabled else "public (no key)")
        logger.info("  Started: %s UTC", datetime.utcnow().isoformat())
        logger.info("=" * 60)

        await self.db.init()
        await self.polymarket.start()
        await self.chainlink.start()
        await self.telegram.start()
        await self.engine.start()
        await self.feed.start()

        # Live pre-flight: provjeri sve sustave prije pravog novca
        if self.config.is_live_trading:
            await asyncio.sleep(5.0)  # kratko cekanje da se WS i order bookovi napune
            passed = await _live_preflight(self.config, self)
            if not passed:
                self.request_stop()
                return

        await self.dashboard.start()

        tasks = [
            asyncio.create_task(self._scan_worker(), name="scan_worker"),
            asyncio.create_task(self._bundle_scanner(), name="bundle_scanner"),
            asyncio.create_task(self._stats_refresher(), name="stats_refresher"),
            asyncio.create_task(self._daily_summary_sender(), name="daily_summary"),
            asyncio.create_task(self._stop_event.wait(), name="stop_watcher"),
        ]
        if self.config.is_live_trading:
            tasks.append(asyncio.create_task(self._live_balance_syncer(), name="balance_syncer"))

        logger.info("Bot running. Press Ctrl+C to stop.")
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def shutdown(self):
        logger.info("Shutting down...")
        self._running = False
        _release_pid_lock()
        await self.dashboard.stop()
        await self.feed.stop()
        await self.chainlink.stop()
        await self.engine.stop()
        await self.polymarket.stop()
        await self.telegram.send("🔴 Bot stopped.")
        await self.telegram.stop()
        logger.info("Stopped cleanly.")

    def request_stop(self):
        self._stop_event.set()

    # ── Obrada price ticka ────────────────────────────────────────

    async def _on_price_tick(self, tick):
        try:
            self._scan_queue.put_nowait(tick.asset)
        except asyncio.QueueFull:
            pass

    # ── Radnik skeniranja ─────────────────────────────────────────

    async def _scan_worker(self):
        while self._running:
            try:
                asset = await asyncio.wait_for(self._scan_queue.get(), timeout=1.0)
                if self.risk.is_killed:
                    continue

                opportunities = await self.arb.scan(asset)
                for opp in opportunities:
                    await self.engine.execute_opportunity(opp)

                # Cross-asset oracle lag: Polymarketov ~2.7s oracle delay vrijedi za SVE
                # assete istovremeno. Kad bilo koji asset okine potvrdjeni signal, odmah
                # skeniraj ostale — njihovi Polymarket ugovori takodje kasne.
                chg = self.feed.price_change_pct(asset, self.config.PRICE_WINDOW_SECONDS)
                if abs(chg) >= self.config.LAG_THRESHOLD_PCT:
                    correlated = [
                        a.split("-")[0] for a in self.config.ASSETS
                        if a.split("-")[0] != asset
                    ]
                    cross_results = await asyncio.gather(
                        *[self.arb.scan(other) for other in correlated],
                        return_exceptions=True,
                    )
                    for result in cross_results:
                        if isinstance(result, list):
                            for opp in result:
                                await self.engine.execute_opportunity(opp)

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Scan worker error: %s", exc, exc_info=True)
                await asyncio.sleep(1)

    # ── Povratni pozivi ───────────────────────────────────────────

    async def _on_trade_open(self, pos):
        await self.telegram.send_trade_opened(pos)

    async def _on_trade_close(self, pos, pnl: float, status: str):
        await self.telegram.send_trade_closed(pos, pnl, status)

    async def _on_kill_switch(self, reason: str):
        await self.telegram.send_kill_switch(reason)

    async def _on_telegram_kill(self):
        self.risk.manual_kill("Remote kill via Telegram")

    async def _on_telegram_resume(self):
        self.risk.manual_resume()

    async def _on_telegram_status(self) -> str:
        rs = self.risk.status()
        sc = self.dashboard._stats_cache
        mode = "LIVE" if self.config.is_live_trading else "PAPER"
        pnl = rs["daily_pnl"]
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        killed = "🛑 KILL SWITCH ACTIVE\n" if rs["is_killed"] else ""
        return (
            f"{killed}"
            f"📊 <b>Status [{mode}]</b>\n\n"
            f"💰 Balance: <code>${rs['balance']:.2f}</code>\n"
            f"📈 P&amp;L today: <code>{pnl_str}</code>\n"
            f"📉 Drawdown: <code>{rs['daily_drawdown_pct']:.1%}</code>\n"
            f"📋 Open positions: <code>{rs['open_positions']}</code>\n"
            f"🏆 Win rate: <code>{sc.get('win_rate', 0):.1%}</code> "
            f"({sc.get('total', 0)} trades)"
        )

    # ── Pozadinski zadaci ─────────────────────────────────────────

    async def _bundle_scanner(self):
        """
        Provjerava bundle arbitrazne prilike svakih 30s — nije potreban price signal.
        Bundle arb (UP+DOWN < $0.97) je zagarantirana zarada bez obzira na smjer cijene.
        """
        await asyncio.sleep(10.0)  # pricekaj da se order bookovi napune
        while self._running:
            if not self.risk.is_killed:
                try:
                    for asset in [a.split("-")[0] for a in self.config.ASSETS]:
                        bundle_opps = self.arb._scan_bundles(asset)
                        for opp in bundle_opps:
                            await self.engine.execute_opportunity(opp)
                except Exception as exc:
                    logger.error("Bundle scanner error: %s", exc)
            await asyncio.sleep(30)

    async def _stats_refresher(self):
        while self._running:
            await self.dashboard.refresh_stats()
            await asyncio.sleep(30)

    async def _live_balance_syncer(self):
        """
        Svakih 60s dohvaca stvarni on-chain USDC balans i sinkronizira risk manager.
        Sprecava unutarnji tracker balansa da zaostane za stvarnoscu (npr. ako se nalog
        namiri on-chain bez cistog callbacka, ili ako se sredstva povuku).
        """
        while self._running:
            await asyncio.sleep(60)
            balance = await _fetch_usdc_balance(self.config.POLYGON_ADDRESS)
            if balance is not None:
                self.risk.update_balance(balance)
                logger.info("[LIVE] On-chain balance synced: $%.2f USDC", balance)
            else:
                logger.warning("[LIVE] Balance sync failed — all Polygon RPCs unreachable")

    async def _daily_summary_sender(self):
        while self._running:
            await asyncio.sleep(3600)
            now = datetime.utcnow()
            if now.hour == 0 and now.minute < 5:
                try:
                    stats = await self.db.get_today_stats()
                    rs = self.risk.status()
                    await self.telegram.send_daily_summary(stats, rs)
                    await self.db.snapshot_balance(rs["balance"], self.engine.mode)
                except Exception as exc:
                    logger.error("Daily summary error: %s", exc)


# ── Ulazna tocka ─────────────────────────────────────────────────────────────

async def run_stats():
    db = Database()
    await db.init()
    stats = await db.get_all_time_stats()
    today = await db.get_today_stats()
    print("\n=== ALL-TIME STATS ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    print("\n=== TODAY ===")
    for k, v in today.items():
        print(f"  {k}: {v}")


async def run_check():
    """Brza provjera ugovora bez pokretanja cijelog bota."""
    sys.path.insert(0, os.path.dirname(__file__))
    from scripts.check_contracts import fetch_contracts, print_results
    import aiohttp
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await fetch_contracts(session)
    print_results(results)


async def main():
    config = Config()

    if "--stats" in sys.argv:
        await run_stats()
        return

    if "--check" in sys.argv:
        await run_check()
        return

    # Potvrda live tradinga
    if config.is_live_trading:
        print("\n" + "=" * 60)
        print("  ⚠️  LIVE TRADING MODE ACTIVE")
        print("  You are about to trade REAL USDC on Polygon.")
        print("  Wallet:", config.POLYGON_ADDRESS[:10] + "...")
        confirm = input("  Type 'CONFIRM' to proceed: ").strip()
        if confirm != "CONFIRM":
            print("Cancelled.")
            return
        print("=" * 60 + "\n")
    else:
        print("\n[PAPER] Starting in PAPER TRADING mode.")
        print(f"   Starting balance: ${config.PAPER_STARTING_BALANCE:.2f} USDC (virtual)")
        print(f"   Coinbase auth:    {'JWT API key' if config.coinbase_auth_enabled else 'public (no key set)'}")
        print("   No real money at risk.\n")

    _acquire_pid_lock()
    bot = PolymarketBot(config)

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — stopping.")
        bot.request_stop()
    finally:
        _release_pid_lock()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")

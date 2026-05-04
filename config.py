"""
config.py — Konfiguracija Polymarket Arbitrage Bota (Windows).
"""

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── Coinbase CDP API ─────────────────────────────────────────
    # API Key ID  = UUID s Developer Platforma (npr. b2c9d6c1-5338-...)
    # API Secret  = base64 Secret s Developer Platforme
    COINBASE_WS_URL: str = "wss://advanced-trade-api.coinbase.com/ws/user"
    COINBASE_API_KEY: str = field(
        default_factory=lambda: os.getenv("COINBASE_API_KEY", "")
    )
    COINBASE_API_SECRET: str = field(
        default_factory=lambda: os.getenv("COINBASE_API_SECRET", "")
    )
    ASSETS: list = field(default_factory=lambda: ["BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD"])

    # ── Polymarket APIs ──────────────────────────────────────────
    POLYMARKET_CLOB_URL: str = "https://clob.polymarket.com"
    POLYMARKET_GAMMA_URL: str = "https://gamma-api.polymarket.com"

    # ── Arbitrage parametri ──────────────────────────────────────
    LAG_THRESHOLD_PCT: float = 0.003  # 0.3% in 10s — bigger moves have residual edge after MM repricing
    MIN_EDGE: float = 0.02
    MIN_CONFIDENCE: float = 0.40
    PRICE_WINDOW_SECONDS: int = 10
    MOMENTUM_WEIGHT: float = 0.60
    BOOK_WEIGHT: float = 0.40

    # ── Position sizing (half-Kelly) ─────────────────────────────
    KELLY_FRACTION: float = 0.50
    MAX_POSITION_PCT: float = 0.08
    MIN_TRADE_USDC: float = 1.0
    MIN_MARKET_PRICE: float = 0.15   # reject deep OTM contracts — model unreliable below 15¢
    TAKER_FEE: float = 0.02          # Polymarket taker fee per leg (~2%, actual BTC/ETH = 1.80%)
    BUNDLE_MIN_PROFIT: float = 0.03  # minimum guaranteed profit for bundle arb (after fees)

    # ── Markets to monitor ───────────────────────────────────────
    UPDOWN_DURATIONS: list = field(default_factory=lambda: [5, 15])
    # 15-min: wider spreads + less HFT competition; fees same as 5-min (1.80% BTC/ETH)

    # ── Risk management ──────────────────────────────────────────
    MAX_DAILY_DRAWDOWN: float = 0.20
    MAX_OPEN_POSITIONS: int = 6
    COOLDOWN_AFTER_KILL_SECS: int = 3600
    EARLY_EXIT_THRESHOLD: float = 0.35  # exit if token mid drops below 35% of entry price
    EARLY_WIN_THRESHOLD: float = 0.82   # close early and lock profit if token mid reaches 82%+
    MIN_WINDOW_SECS_REMAINING: int = 60  # don't enter in last 60s of contract window

    # ── Live trading safety limits ───────────────────────────────
    # Percentage-based limits — computed from actual wallet balance at startup.
    # This way the same config works whether you deposit $20 or $2000.
    MAX_LIVE_TRADE_PCT: float = 0.15      # max 15% of balance per single trade (hard cap above Kelly's 8%)
    MIN_LIVE_BALANCE_PCT: float = 0.10    # kill switch floor: stop if balance drops below 10% of start

    # Filled automatically from live balance × pct above — do not set manually.
    MAX_LIVE_TRADE_USDC: float = 0.0
    MIN_LIVE_BALANCE_USDC: float = 0.0

    MAX_TRADES_PER_HOUR: int = 10         # rate limit: refuse if already placed N trades this hour
    LIVE_STARTUP_DELAY_SECS: int = 30     # wait before first live trade (let WS/books stabilize)
    MAX_LIVE_SLIPPAGE_PCT: float = 0.015  # abort live fill if fill_price > ask + 1.5%

    # ── Market refresh ───────────────────────────────────────────
    CONTRACT_REFRESH_INTERVAL: int = 60
    PRICE_STALENESS_LIMIT: float = 5.0

    # ── Paper trading ────────────────────────────────────────────
    PAPER_STARTING_BALANCE: float = float(os.getenv("PAPER_STARTING_BALANCE", "1000.0"))
    PAPER_FILL_SLIPPAGE: float = 0.002

    # ── Dashboard ────────────────────────────────────────────────
    DASHBOARD_REFRESH_RATE: float = 1.0

    # ── Polygon wallet (live trading) ────────────────────────────
    POLYGON_PRIVATE_KEY: str = field(default_factory=lambda: os.getenv("POLYGON_PRIVATE_KEY", ""))
    POLYGON_ADDRESS: str = field(default_factory=lambda: os.getenv("POLYGON_ADDRESS", ""))

    # ── Telegram ─────────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))

    # ── Live trading flagovi (SVI 3 moraju biti true) ────────────
    LIVE_TRADING_ENABLED: bool = field(
        default_factory=lambda: os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
    )
    LIVE_TRADING_CONFIRMED: bool = field(
        default_factory=lambda: os.getenv("LIVE_TRADING_CONFIRMED", "false").lower() == "true"
    )
    LIVE_TRADING_RISK_ACKNOWLEDGED: bool = field(
        default_factory=lambda: os.getenv("LIVE_TRADING_RISK_ACKNOWLEDGED", "false").lower() == "true"
    )

    @property
    def is_live_trading(self) -> bool:
        return all([
            self.LIVE_TRADING_ENABLED,
            self.LIVE_TRADING_CONFIRMED,
            self.LIVE_TRADING_RISK_ACKNOWLEDGED,
            bool(self.POLYGON_PRIVATE_KEY),
            bool(self.POLYGON_ADDRESS),
        ])

    @property
    def coinbase_auth_enabled(self) -> bool:
        return bool(self.COINBASE_API_KEY) and bool(self.COINBASE_API_SECRET)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.TELEGRAM_BOT_TOKEN) and bool(self.TELEGRAM_CHAT_ID)

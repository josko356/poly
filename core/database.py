"""
core/database.py — Async SQLite layer for trade history and performance tracking.
"""

import aiosqlite
import asyncio
import logging
from datetime import datetime, date
from typing import Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

DB_PATH = "trades.db"

# ── Data models ──────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    id: Optional[int]
    timestamp: str
    mode: str              # "paper" | "live"
    asset: str             # "BTC" | "ETH"
    contract_id: str
    contract_question: str
    direction: str         # "UP" | "DOWN"
    duration_mins: int     # 5 | 15
    entry_price: float     # price paid per share (0–1)
    shares: float
    usdc_spent: float
    edge: float
    confidence: float
    kelly_size: float
    polymarket_prob: float
    model_prob: float
    coinbase_price: float
    status: str            # "open" | "won" | "lost" | "cancelled"
    pnl: Optional[float]
    exit_timestamp: Optional[str]


@dataclass
class DailyStats:
    date: str
    starting_balance: float
    ending_balance: float
    trades: int
    wins: int
    losses: int
    pnl: float
    max_drawdown: float


# ── Database class ───────────────────────────────────────────────────────────

class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._lock = asyncio.Lock()

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    contract_id TEXT NOT NULL,
                    contract_question TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    duration_mins INTEGER NOT NULL,
                    entry_price REAL NOT NULL,
                    shares REAL NOT NULL,
                    usdc_spent REAL NOT NULL,
                    edge REAL NOT NULL,
                    confidence REAL NOT NULL,
                    kelly_size REAL NOT NULL,
                    polymarket_prob REAL NOT NULL,
                    model_prob REAL NOT NULL,
                    coinbase_price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    pnl REAL,
                    exit_timestamp TEXT
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS daily_stats (
                    date TEXT PRIMARY KEY,
                    starting_balance REAL,
                    ending_balance REAL,
                    trades INTEGER DEFAULT 0,
                    wins INTEGER DEFAULT 0,
                    losses INTEGER DEFAULT 0,
                    pnl REAL DEFAULT 0,
                    max_drawdown REAL DEFAULT 0
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS balance_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    balance REAL NOT NULL,
                    mode TEXT NOT NULL
                )
            """)
            await db.commit()
        logger.info("Database initialised at %s", self.path)

    async def insert_trade(self, trade: TradeRecord) -> int:
        async with self._lock:
            async with aiosqlite.connect(self.path) as db:
                cursor = await db.execute("""
                    INSERT INTO trades (
                        timestamp, mode, asset, contract_id, contract_question,
                        direction, duration_mins, entry_price, shares, usdc_spent,
                        edge, confidence, kelly_size, polymarket_prob, model_prob,
                        coinbase_price, status, pnl, exit_timestamp
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    trade.timestamp, trade.mode, trade.asset, trade.contract_id,
                    trade.contract_question, trade.direction, trade.duration_mins,
                    trade.entry_price, trade.shares, trade.usdc_spent,
                    trade.edge, trade.confidence, trade.kelly_size,
                    trade.polymarket_prob, trade.model_prob, trade.coinbase_price,
                    trade.status, trade.pnl, trade.exit_timestamp,
                ))
                await db.commit()
                return cursor.lastrowid

    async def update_trade_result(self, trade_id: int, status: str, pnl: float):
        async with self._lock:
            async with aiosqlite.connect(self.path) as db:
                await db.execute("""
                    UPDATE trades SET status=?, pnl=?, exit_timestamp=?
                    WHERE id=?
                """, (status, pnl, datetime.utcnow().isoformat(), trade_id))
                await db.commit()

    async def get_recent_trades(self, limit: int = 10) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_open_trades(self) -> list[dict]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM trades WHERE status='open' ORDER BY timestamp DESC"
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_today_stats(self) -> dict:
        today = date.today().isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM trades WHERE timestamp LIKE ? AND status != 'open'",
                (f"{today}%",)
            )
            rows = await cursor.fetchall()
            trades = [dict(r) for r in rows]

        wins = [t for t in trades if t["status"] == "won"]
        losses = [t for t in trades if t["status"] == "lost"]
        total_pnl = sum(t["pnl"] or 0 for t in trades)
        win_rate = len(wins) / len(trades) if trades else 0.0

        return {
            "date": today,
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": win_rate,
            "total_pnl": total_pnl,
        }

    async def get_all_time_stats(self) -> dict:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM trades WHERE status != 'open'"
            )
            rows = await cursor.fetchall()
            trades = [dict(r) for r in rows]

        if not trades:
            return {"total": 0, "win_rate": 0.0, "total_pnl": 0.0}

        wins = [t for t in trades if t["status"] == "won"]
        total_pnl = sum(t["pnl"] or 0 for t in trades)
        return {
            "total": len(trades),
            "wins": len(wins),
            "losses": len(trades) - len(wins),
            "win_rate": len(wins) / len(trades),
            "total_pnl": total_pnl,
            "avg_pnl": total_pnl / len(trades),
        }

    async def snapshot_balance(self, balance: float, mode: str):
        async with self._lock:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT INTO balance_snapshots (timestamp, balance, mode) VALUES (?,?,?)",
                    (datetime.utcnow().isoformat(), balance, mode)
                )
                await db.commit()

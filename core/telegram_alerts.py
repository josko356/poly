"""
core/telegram_alerts.py — Telegram notification system.

Sends alerts for:
  - Every trade opened and closed
  - Kill switch activation
  - Daily summary
  - Startup / shutdown messages
  - System errors

Uses the Bot API directly via aiohttp (no external library needed).
"""

import asyncio
import logging
import time
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT = aiohttp.ClientTimeout(total=8)
POLL_TIMEOUT = aiohttp.ClientTimeout(total=35)   # long-poll timeout
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0

# Telegram bots CANNOT message users first.
# The user MUST send /start to the bot in Telegram before any message will arrive.


class TelegramBot:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.enabled = bool(token) and bool(chat_id)
        self._session: Optional[aiohttp.ClientSession] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._update_offset: int = 0

        # Set these after construction to wire up remote commands
        self.on_kill_command:   Optional[Callable] = None
        self.on_resume_command: Optional[Callable] = None
        self.on_status_command: Optional[Callable] = None  # must return str

    async def start(self):
        if not self.enabled:
            logger.info("Telegram disabled (no token/chat_id).")
            return
        self._session = aiohttp.ClientSession(timeout=SEND_TIMEOUT)
        self._worker_task = asyncio.create_task(self._worker())

        # Verify token and connectivity before queuing startup message
        ok = await self._verify_token()
        if not ok:
            logger.error(
                "Telegram: token verification failed. "
                "Check TELEGRAM_BOT_TOKEN in .env."
            )
            return

        self._listener_task = asyncio.create_task(self._command_listener())
        logger.info("Telegram bot started. Sending startup message...")
        await self.send(
            "🤖 <b>Polymarket Arbitrage Bot</b> started.\n\n"
            "Monitoring BTC &amp; ETH contracts...\n\n"
            "Commands:\n"
            "/kill — halt all trading\n"
            "/resume — resume after kill\n"
            "/status — balance &amp; P&amp;L"
        )

    async def _verify_token(self) -> bool:
        """Call getMe to verify the token is valid. Logs a clear error on failure."""
        url = f"{TELEGRAM_API}/bot{self.token}/getMe"
        try:
            async with self._session.get(url) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("ok"):
                    bot_name = data["result"].get("username", "?")
                    logger.info("Telegram: connected as @%s", bot_name)
                    logger.info(
                        "Telegram: make sure you have sent /start to @%s "
                        "— bots cannot message users who have never written first.",
                        bot_name,
                    )
                    return True
                logger.error(
                    "Telegram: getMe returned %d — %s",
                    resp.status, data.get("description", data),
                )
                return False
        except Exception as exc:
            logger.error("Telegram: connectivity check failed: %s", exc)
            return False

    async def stop(self):
        if self._worker_task:
            self._worker_task.cancel()
        if self._listener_task:
            self._listener_task.cancel()
        if self._session:
            await self._session.close()

    # ── Public send methods ───────────────────────────────────────

    async def send(self, text: str):
        """Queue a message for sending (non-blocking)."""
        if not self.enabled:
            return
        await self._queue.put(text)

    async def send_trade_opened(self, pos):
        opp = pos.opportunity
        mode_emoji = "📋" if pos.mode == "paper" else "💰"
        direction_emoji = "📈" if opp.direction == "UP" else "📉"
        msg = (
            f"{mode_emoji} <b>Trade Opened</b> [{pos.mode.upper()}]\n\n"
            f"{direction_emoji} <b>{opp.contract.asset} {opp.direction}</b> "
            f"({opp.contract.duration_mins}min)\n"
            f"💵 Entry: <code>{opp.polymarket_price:.3f}</code>\n"
            f"🎯 Model: <code>{opp.model_prob:.3f}</code>\n"
            f"📊 Edge: <code>{opp.edge:.1%}</code>\n"
            f"🔮 Confidence: <code>{opp.confidence:.1%}</code>\n"
            f"💲 Size: <code>${pos.paper_usdc_spent:.2f}</code>"
        )
        await self.send(msg)

    async def send_trade_closed(self, pos, pnl: float, status: str):
        opp = pos.opportunity
        result_emoji = "✅" if status == "won" else "❌"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        msg = (
            f"{result_emoji} <b>Trade Closed</b> [{pos.mode.upper()}]\n\n"
            f"{'📈' if opp.direction == 'UP' else '📉'} "
            f"<b>{opp.contract.asset} {opp.direction}</b> "
            f"({opp.contract.duration_mins}min)\n"
            f"💰 P&amp;L: <code>{pnl_str}</code>\n"
            f"📋 Status: <b>{status.upper()}</b>"
        )
        await self.send(msg)

    async def send_kill_switch(self, reason: str):
        msg = (
            f"🛑 <b>KILL SWITCH ACTIVATED</b>\n\n"
            f"⚠️ <i>{reason}</i>\n\n"
            f"All trading halted. Check the dashboard."
        )
        await self.send(msg)

    async def send_daily_summary(self, stats: dict, risk_status: dict):
        win_rate = stats.get("win_rate", 0)
        msg = (
            f"📊 <b>Daily Summary</b>\n\n"
            f"💰 Balance: <code>${risk_status['balance']:.2f}</code>\n"
            f"📈 P&amp;L: <code>${risk_status['daily_pnl']:+.2f}</code>\n"
            f"🏆 Win rate: <code>{win_rate:.1%}</code>\n"
            f"📋 Trades: <code>{stats['total_trades']}</code> "
            f"(W:{stats['wins']} / L:{stats['losses']})\n"
            f"📉 Max drawdown: <code>{risk_status['max_drawdown_today']:.1%}</code>"
        )
        await self.send(msg)

    async def send_error(self, error: str):
        await self.send(f"⚠️ <b>Error</b>: <code>{error}</code>")

    # ── Worker ────────────────────────────────────────────────────

    async def _worker(self):
        try:
            while True:
                text = await self._queue.get()
                await self._send_with_retry(text)
                await asyncio.sleep(0.3)  # rate limit: ~3 messages/sec
        except asyncio.CancelledError:
            pass

    # ── Command listener ──────────────────────────────────────────

    async def _command_listener(self):
        """
        Long-polls Telegram for incoming messages and handles commands.
        Only accepts messages from the authorised chat_id (ignores everyone else).
        Supported: /kill  /resume  /status
        """
        url = f"{TELEGRAM_API}/bot{self.token}/getUpdates"
        while True:
            try:
                params = {"timeout": 30, "offset": self._update_offset, "allowed_updates": ["message"]}
                async with self._session.get(url, params=params, timeout=POLL_TIMEOUT) as resp:
                    if resp.status != 200:
                        await asyncio.sleep(5)
                        continue
                    data = await resp.json()
                    for update in data.get("result", []):
                        self._update_offset = update["update_id"] + 1
                        await self._handle_update(update)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.debug("Telegram command listener error: %s", exc)
                await asyncio.sleep(5)

    async def _handle_update(self, update: dict):
        msg = update.get("message", {})
        if not msg:
            return

        # Security: only accept from authorised chat
        sender_id = str(msg.get("chat", {}).get("id", ""))
        if sender_id != str(self.chat_id):
            logger.warning("Telegram: message from unauthorised chat %s — ignored", sender_id)
            return

        text = msg.get("text", "").strip().lower().split()[0] if msg.get("text") else ""

        if text == "/kill":
            logger.info("Telegram remote kill command received.")
            if self.on_kill_command:
                await self._safe_call(self.on_kill_command)
            await self.send("🛑 <b>Kill switch activated remotely.</b>\nAll trading halted.")

        elif text == "/resume":
            logger.info("Telegram remote resume command received.")
            if self.on_resume_command:
                await self._safe_call(self.on_resume_command)
            await self.send("✅ <b>Trading resumed.</b>")

        elif text == "/status":
            if self.on_status_command:
                reply = await self._safe_call(self.on_status_command, return_value=True)
                await self.send(reply or "Status unavailable.")
            else:
                await self.send("Status callback not configured.")

        elif text in ("/start", "/help"):
            await self.send(
                "Commands:\n"
                "/kill — halt all trading immediately\n"
                "/resume — resume after kill switch\n"
                "/status — current balance &amp; P&amp;L"
            )

    async def _safe_call(self, fn: Callable, return_value: bool = False):
        try:
            import inspect
            if inspect.iscoroutinefunction(fn):
                result = await fn()
            else:
                result = fn()
            return result if return_value else None
        except Exception as exc:
            logger.error("Telegram command callback error: %s", exc)
            return None

    async def _send_with_retry(self, text: str):
        url = f"{TELEGRAM_API}/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with self._session.post(url, json=payload) as resp:
                    if resp.status == 200:
                        return
                    data = await resp.json()
                    if resp.status == 403:
                        logger.warning(
                            "Telegram 403 Forbidden — did you send /start to the bot? "
                            "Detail: %s", data.get("description", data)
                        )
                        return  # no point retrying
                    if resp.status in (400, 401):
                        logger.warning(
                            "Telegram permanent error %d: %s — check TELEGRAM_CHAT_ID",
                            resp.status, data.get("description", data),
                        )
                        return
                    logger.warning("Telegram API error %d: %s", resp.status, data)
            except Exception as exc:
                logger.warning("Telegram send attempt %d/%d failed: %s", attempt + 1, RETRY_ATTEMPTS, exc)
            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        logger.error("Telegram: message dropped after %d retries", RETRY_ATTEMPTS)

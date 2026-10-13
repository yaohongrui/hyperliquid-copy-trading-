import asyncio
from html import escape
import json
import logging

import aiohttp


log = logging.getLogger(__name__)


class TelegramManager:
    """Small Telegram Bot API long-polling client for the control commands."""

    def __init__(self, engine, token, chat_id, polling_timeout=25,
                 fetch_account_positions=None, fetch_leader_positions=None):
        self.engine = engine
        self.token = token
        self.chat_id = str(chat_id) if chat_id is not None else None
        self.polling_timeout = max(1, int(polling_timeout))
        self._stop = asyncio.Event()
        self._api_url = f"https://api.telegram.org/bot{token}"
        self.fetch_account_positions = fetch_account_positions
        self.fetch_leader_positions = fetch_leader_positions
        self.session = None

    def stop(self):
        self._stop.set()

    async def handle(self, text):
        # Telegram sends /command@botname in group chats.
        cmd = text.strip().split()[0] if text.strip() else ""
        cmd = cmd.split("@", 1)[0]
        if cmd == "/pause":
            self.engine.paused = True
            return "New entries paused."
        if cmd == "/resume":
            self.engine.paused = False
            return "New entries resumed."
        if cmd == "/status":
            return "PAUSED" if self.engine.paused else "RUNNING"
        if cmd == "/positions":
            return await self.fetch_account_positions() if self.fetch_account_positions else "Account query unavailable."
        if cmd == "/leader_positions":
            return await self.fetch_leader_positions() if self.fetch_leader_positions else "Leader query unavailable."
        return "Unknown command."

    async def _request(self, session, method, **params):
        async with session.post(f"{self._api_url}/{method}", data=params) as response:
            response.raise_for_status()
            payload = await response.json()
            if not payload.get("ok"):
                raise RuntimeError(payload.get("description", "Telegram API request failed"))
            return payload.get("result")

    async def send(self, text):
        """Send a best-effort notification without affecting trading."""
        if not self.session or not self.chat_id:
            return False
        try:
            await self._request(self.session, "sendMessage", chat_id=self.chat_id,
                                text=text, parse_mode="HTML")
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Telegram notification failed (%s)", type(exc).__name__)
            return False

    @staticmethod
    def _value(value):
        """Keep notification values compact while preserving useful precision."""
        if isinstance(value, float):
            return f"{value:.8g}"
        return str(value)

    @classmethod
    def _event_message(cls, title, fields, reason=None):
        lines = [f"<b>{escape(title)}</b>", "<i>━━━━━━━━━━━━</i>"]
        for label, value in fields:
            lines.append(f"<b>{escape(label)}</b>: {escape(cls._value(value))}")
        if reason is not None:
            lines.extend(("", f"<b>Reason</b>: {escape(str(reason)[:300])}"))
        return "\n".join(lines)

    async def notify_filled(self, leader, signal, order):
        kind = {"open": "OPEN", "increase": "INCREASE", "decrease": "DECREASE", "close": "CLOSE"}.get(signal.kind, signal.kind.upper())
        price = getattr(order, "price", None)
        fields = [
            ("Leader", leader),
            ("Symbol", signal.symbol),
            ("Position", signal.side.upper()),
            ("Action", kind),
            ("Filled qty", abs(order.amount)),
        ]
        if price is not None:
            fields.append(("Avg price", price))
        await self.send(self._event_message("✅ ORDER FILLED", fields))

    async def notify_failed(self, leader, signal, exc):
        await self.send(self._event_message("❌ ORDER FAILED", [
            ("Leader", leader),
            ("Symbol", signal.symbol),
            ("Position", signal.side.upper()),
            ("Action", signal.kind.upper()),
        ], reason=exc))

    async def notify_skipped(self, leader, signal, reason):
        await self.send(self._event_message("⏭️ ORDER SKIPPED", [
            ("Leader", leader),
            ("Symbol", signal.symbol),
            ("Position", signal.side.upper()),
            ("Action", signal.kind.upper()),
        ], reason=reason))

    async def notify_accepted(self, leader, signal, order):
        """Notify when an exchange accepts an order that is not filled yet."""
        await self.send(self._event_message("🕒 ORDER ACCEPTED", [
            ("Leader", leader),
            ("Symbol", signal.symbol),
            ("Position", signal.side.upper()),
            ("Action", signal.kind.upper()),
            ("Order", order.id),
            ("Status", order.status.upper()),
        ]))

    async def run(self):
        if not self.token or not self.chat_id:
            raise ValueError("Telegram bot token and chat id are required")

        timeout = aiohttp.ClientTimeout(total=self.polling_timeout + 10)
        offset = None
        async with aiohttp.ClientSession(timeout=timeout) as session:
            self.session = session
            # Avoid replaying old commands every time the service restarts.
            while not self._stop.is_set() and offset is None:
                try:
                    updates = await self._request(session, "getUpdates", timeout=0, limit=100)
                    if updates:
                        offset = updates[-1]["update_id"] + 1
                    else:
                        offset = 0
                    log.info("Telegram control bot enabled for chat %s", self.chat_id)
                    await self._request(session, "setMyCommands", commands=json.dumps([
                        {"command": "status", "description": "Show system status"},
                        {"command": "positions", "description": "Show account positions"},
                        {"command": "leader_positions", "description": "Show leader positions"},
                        {"command": "pause", "description": "Pause new entries"},
                        {"command": "resume", "description": "Resume new entries"},
                    ]))
                    await self.send("<b>SYSTEM STARTED</b>\nTelegram control and notifications are online.")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "Telegram startup check failed (%s); retrying in 5 seconds",
                        type(exc).__name__,
                    )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
            while not self._stop.is_set():
                try:
                    updates = await self._request(
                        session,
                        "getUpdates",
                        offset=offset,
                        timeout=self.polling_timeout,
                        allowed_updates='["message"]',
                    )
                    for update in updates or []:
                        offset = update["update_id"] + 1
                        message = update.get("message") or {}
                        chat = message.get("chat") or {}
                        if str(chat.get("id")) != self.chat_id:
                            continue
                        text = message.get("text")
                        if not text:
                            continue
                        try:
                            reply = await self.handle(text)
                        except Exception:
                            log.exception("Telegram command failed")
                            reply = "Command failed."
                        await self._request(
                            session,
                            "sendMessage",
                            chat_id=self.chat_id,
                            text=reply,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning(
                        "Telegram polling failed (%s); retrying in 5 seconds",
                        type(exc).__name__,
                    )
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        pass
            await self.send("<b>SYSTEM STOPPED</b>")
            self.session = None

import asyncio
from html import escape
import json
import logging

import aiohttp

from .report_image import render_report


log = logging.getLogger(__name__)

# Notification sends must never stall the trading hot path.  The session keeps
# its longer timeout for getUpdates long polling, while a single send gets a
# short, independent deadline.
_SEND_TIMEOUT = aiohttp.ClientTimeout(total=5)


class TelegramManager:
    """Small Telegram Bot API long-polling client for the control commands."""

    def __init__(self, engine, token, chat_id, polling_timeout=25,
                 fetch_account_positions=None, fetch_leader_positions=None,
                 fetch_equity=None, account_name=None, account_config=None,
                 mode=None, report_format="image"):
        self.engine = engine
        self.token = token
        self.chat_id = str(chat_id) if chat_id is not None else None
        self.polling_timeout = max(1, int(polling_timeout))
        self._stop = asyncio.Event()
        self._api_url = f"https://api.telegram.org/bot{token}"
        self.fetch_account_positions = fetch_account_positions
        self.fetch_leader_positions = fetch_leader_positions
        self.fetch_equity = fetch_equity
        self.account_name = account_name or "-"
        self.account_config = account_config or {}
        self.mode = mode or "UNKNOWN"
        self.report_format = str(report_format or "image").lower()
        self.session = None

    def stop(self):
        self._stop.set()

    async def handle(self, text):
        # Telegram sends /command@botname in group chats.
        cmd = text.strip().split()[0] if text.strip() else ""
        cmd = cmd.split("@", 1)[0]
        if cmd == "/pause":
            self.engine.paused = True
            return await self._status_message("⏸ NEW ENTRIES PAUSED")
        if cmd == "/resume":
            self.engine.paused = False
            return await self._status_message("▶️ NEW ENTRIES RESUMED")
        if cmd in {"/unlock_hard_stop", "/unlock_hardstop"}:
            try:
                result = await self.engine.unlock_hard_stop()
            except Exception as exc:
                log.warning("Telegram hard stop unlock failed (%s)", type(exc).__name__)
                return self._event_message(
                    "❌ HARD STOP UNLOCK FAILED",
                    [("Account", self.account_name)],
                    reason=exc,
                )
            return (await self._status_message("🔓 HARD STOP UNLOCKED")) + (
                f"\n<b>Daily baseline</b>: {escape(self._value(result['baseline']))} USDT"
                f" (was {'LOCKED' if result['was_locked'] else 'unlocked'})"
            )
        if cmd == "/status":
            return await self._status_message()
        if cmd == "/positions":
            return await self._positions_message()
        if cmd in {"/leader_positions", "/leader_positons"}:
            return await self._leader_positions_message()
        return self._event_message("❔ UNKNOWN COMMAND", [
            ("Available", "/status, /positions, /leader_positions"),
            ("Controls", "/pause, /resume, /unlock_hard_stop"),
        ], reason="Please choose a command from the bot menu.")

    def _stop_loss_text(self):
        hard_stop = self.account_config.get("hard_stop") or {}
        limits = []
        if hard_stop.get("max_daily_loss_usdt") is not None:
            limits.append(f"daily loss ≤ {self._value(float(hard_stop['max_daily_loss_usdt']))} USDT")
        if hard_stop.get("max_daily_drawdown_pct") is not None:
            limits.append(f"daily drawdown ≤ {self._value(float(hard_stop['max_daily_drawdown_pct']))}%")
        return "; ".join(limits) if limits else "Not configured"

    async def _equity_text(self):
        if not self.fetch_equity:
            return "Unavailable"
        try:
            return f"{self._value(await self.fetch_equity())} USDT (incl. unrealized PnL)"
        except Exception as exc:
            log.warning("Telegram equity query failed (%s)", type(exc).__name__)
            return "Unavailable"

    async def _status_message(self, title="📊 SYSTEM STATUS"):
        return self._event_message(title, [
            ("Mode", self.mode),
            ("Account", self.account_name),
            ("New entries", "PAUSED" if self.engine.paused else "ENABLED"),
            ("Hard stop", "LOCKED" if self.engine.risk.locked else "OK"),
            ("Hard stop limit", self._stop_loss_text()),
            ("Account equity", await self._equity_text()),
        ])

    async def _status_report(self):
        equity = await self._equity_text()
        return "\n".join([
            f"Mode: {self.mode}",
            f"Account: {self.account_name}",
            f"New entries: {'PAUSED' if self.engine.paused else 'ENABLED'}",
            f"Hard stop: {'LOCKED' if self.engine.risk.locked else 'OK'}",
            f"Hard stop limit: {self._stop_loss_text()}",
            f"Account equity: {equity}",
        ])

    async def _positions_message(self):
        if not self.fetch_account_positions:
            body = "Account positions unavailable."
        else:
            try:
                body = await self.fetch_account_positions()
            except Exception as exc:
                log.warning("Telegram account positions query failed (%s)", type(exc).__name__)
                body = f"Account positions unavailable: {type(exc).__name__}"
        return (await self._status_message("📈 ACCOUNT POSITIONS")) + \
            f"\n\n<b>Open positions</b>\n<pre>{escape(body)}</pre>"

    async def _leader_positions_message(self):
        if not self.fetch_leader_positions:
            body = "Leader positions unavailable."
        else:
            try:
                body = await self.fetch_leader_positions()
            except Exception as exc:
                log.warning("Telegram leader positions query failed (%s)", type(exc).__name__)
                body = f"Leader positions unavailable: {type(exc).__name__}"
        return f"<b>👥 LEADER POSITIONS</b>\n<i>━━━━━━━━━━━━</i>\n<pre>{escape(body)}</pre>"

    async def _report_photo(self, command):
        if command == "/positions":
            if not self.fetch_account_positions:
                body = "Account positions unavailable."
            else:
                body = await self.fetch_account_positions()
            text = f"{await self._status_report()}\n\nOPEN POSITIONS\n{body}"
            return await self.send_photo("ACCOUNT POSITIONS", text)
        if command == "/leader_positions":
            if not self.fetch_leader_positions:
                body = "Leader positions unavailable."
            else:
                body = await self.fetch_leader_positions()
            return await self.send_photo("LEADER POSITIONS", body)
        return False

    async def _request(self, session, method, data=None, request_timeout=None, **params):
        request_data = data if data is not None else params
        async with session.post(
                f"{self._api_url}/{method}", data=request_data, timeout=request_timeout) as response:
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
                                text=text, parse_mode="HTML", request_timeout=_SEND_TIMEOUT)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Telegram notification failed (%s)", type(exc).__name__)
            return False

    async def send_photo(self, title, body):
        if not self.session or not self.chat_id:
            return False
        try:
            form = aiohttp.FormData()
            form.add_field("chat_id", self.chat_id)
            form.add_field("photo", render_report(title, body), filename="report.png", content_type="image/png")
            form.add_field("caption", title)
            await self._request(self.session, "sendPhoto", data=form, request_timeout=_SEND_TIMEOUT)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Telegram report image failed (%s)", type(exc).__name__)
            return False

    @staticmethod
    def _value(value):
        """Keep notification values compact while preserving useful precision."""
        if isinstance(value, float):
            return f"{value:.8g}"
        return str(value)

    @classmethod
    def _event_message(cls, title, fields, reason=None, reason_label="Reason"):
        lines = [f"<b>{escape(title)}</b>", "<i>━━━━━━━━━━━━</i>"]
        for label, value in fields:
            lines.append(f"<b>{escape(label)}</b>: {escape(cls._value(value))}")
        if reason is not None:
            lines.extend(("", f"<b>{escape(reason_label)}</b>: {escape(str(reason)[:300])}"))
        return "\n".join(lines)

    _ORDER_STATUS = {
        "open": "OPEN — resting, not filled",
        "closed": "FILLED",
        "canceled": "CANCELLED",
        "cancelled": "CANCELLED",
        "expired": "EXPIRED",
        "rejected": "REJECTED",
    }

    @classmethod
    def _order_status(cls, status):
        text = str(status or "").lower()
        return cls._ORDER_STATUS.get(text, text.upper() or "UNKNOWN")

    @staticmethod
    def _order_side(signal, order=None):
        """Return the actual execution direction, independent of position side."""
        side = getattr(order, "side", None) if order is not None else None
        if side:
            return str(side).upper()
        return "BUY" if signal.delta > 0 else "SELL"

    def _trade_fields(self, leader, signal, order=None):
        return [
            ("Account", self.account_name),
            ("Leader", leader),
            ("Symbol", signal.symbol),
            ("Leader position", signal.side.upper()),
            ("Order side", self._order_side(signal, order)),
            ("Signal", signal.kind.upper()),
        ]

    async def notify_filled(self, leader, signal, order, order_type=None):
        fields = self._trade_fields(leader, signal, order)
        if order_type:
            fields.append(("Order type", str(order_type).upper()))
        fields.append(("Filled qty", abs(order.amount)))
        price = getattr(order, "price", None)
        if price is not None:
            fields.append(("Avg fill price", price))
        return await self.send(self._event_message("✅ ORDER FILLED", fields))

    async def notify_failed(self, leader, signal, exc):
        return await self.send(self._event_message(
            "❌ ORDER FAILED",
            self._trade_fields(leader, signal),
            reason=exc,
            reason_label="Error",
        ))

    async def notify_skipped(self, leader, signal, reason):
        return await self.send(self._event_message(
            "⏭️ SIGNAL SKIPPED", self._trade_fields(leader, signal), reason=reason,
        ))

    async def notify_source_failed(self, leader, exc):
        return await self.send(self._event_message(
            "⚠️ LEADER SOURCE FAILED",
            [
                ("Leader", leader),
                ("Impact", "No new signals from this leader; existing positions are unchanged"),
            ],
            reason=exc,
            reason_label="Error",
        ))

    async def notify_hard_stop(self, equity, baseline, loss):
        return await self.send(self._event_message("🛑 HARD STOP TRIGGERED", [
            ("Account", self.account_name),
            ("Mode", self.mode),
            ("Account equity", f"{self._value(float(equity))} USDT"),
            ("Day baseline", f"{self._value(float(baseline))} USDT"),
            ("Daily loss", f"{self._value(float(loss))} USDT"),
            ("Limit", self._stop_loss_text()),
            ("Action", "New entries paused; all positions are being flattened"),
        ], reason="Send /unlock_hard_stop in the main control chat to reset the daily baseline and resume entries.",
           reason_label="Next step"))

    async def notify_accepted(self, leader, signal, order, order_type=None):
        """Notify when an exchange accepts an order that is not filled yet."""
        fields = self._trade_fields(leader, signal, order)
        if order_type:
            fields.append(("Order type", str(order_type).upper()))
        fields.extend([
            ("Order ID", order.id),
            ("Order status", self._order_status(order.status)),
        ])
        price = getattr(order, "price", None)
        if price is not None:
            price_label = "Limit price" if str(order_type or "").lower() == "limit" else "Order price"
            fields.append((price_label, price))
        if getattr(signal, "price", 0):
            fields.append(("Market at signal", signal.price))
        return await self.send(self._event_message(
            "🕒 ORDER RESTING (UNFILLED)",
            fields,
            reason=("Stays on the book until filled; the leader's close/reduce "
                    "or a new open signal cancels it."),
            reason_label="Note",
        ))

    async def notify_limit_filled(self, leader, symbol, entry, filled, price, remaining=0.0):
        """Notify that a resting limit entry filled (fully or partially)."""
        remaining = max(0.0, float(remaining or 0))
        full = remaining <= 1e-12
        title = "✅ LIMIT ORDER FILLED" if full else "🟡 LIMIT ORDER PARTIALLY FILLED"
        fields = [
            ("Account", self.account_name),
            ("Leader", leader),
            ("Symbol", symbol),
            ("Order side", str(entry.get("side") or "").upper()),
            ("Filled qty", abs(float(filled or 0))),
        ]
        if not full:
            fields.append(("Remaining qty", remaining))
        if price is not None:
            fields.append(("Avg fill price", price))
        if entry.get("price") is not None:
            fields.append(("Limit price", entry.get("price")))
        fields.append(("Order ID", entry.get("id")))
        return await self.send(self._event_message(
            title,
            fields,
            reason=None if full else "The rest of the order is still resting.",
            reason_label="Note",
        ))

    async def notify_cancelled(self, leader, symbol, entry, reason):
        """Notify that a resting entry was cancelled without filling."""
        order_qty = abs(float(entry.get("amount") or 0))
        filled = abs(float(entry.get("filled") or 0))
        title = ("🚫 ORDER CANCELLED (PARTIALLY FILLED)"
                 if filled > 0 else "🚫 UNFILLED ORDER CANCELLED")
        fields = [
            ("Account", self.account_name),
            ("Leader", leader),
            ("Symbol", symbol),
            ("Order side", str(entry.get("side") or "").upper()),
            ("Order qty", order_qty),
        ]
        if filled > 0:
            fields.extend([
                ("Filled qty", filled),
                ("Remaining qty", max(0.0, order_qty - filled)),
            ])
        fields.append(("Order ID", entry.get("id")))
        return await self.send(self._event_message(title, fields, reason=reason))

    async def notify_mirror_placed(self, leader, symbol, side, amount, price):
        """Notify that a leader limit order is now mirrored post-only."""
        return await self.send(self._event_message("🧷 LIMIT MIRROR PLACED", [
            ("Account", self.account_name),
            ("Leader", leader),
            ("Symbol", symbol),
            ("Order side", str(side).upper()),
            ("Quantity", abs(float(amount or 0))),
            ("Limit price", price),
            ("Order type", "POST-ONLY"),
        ], reason=("Mirrors the leader's resting limit order at the same price; "
                   "it stays until filled or the leader cancels."),
           reason_label="Note"))

    async def notify_mirror_cancelled(self, leader, symbol, entry, reason):
        return await self.send(self._event_message("🚫 LIMIT MIRROR CANCELLED", [
            ("Account", self.account_name),
            ("Leader", leader),
            ("Symbol", symbol),
            ("Order side", str(entry.get("side") or "").upper()),
            ("Order qty", abs(float(entry.get("amount") or 0))),
            ("Filled qty", abs(float(entry.get("filled") or 0))),
            ("Order ID", entry.get("order_id") or entry.get("id")),
        ], reason=reason))

    async def notify_mirror_filled(self, leader, symbol, entry, filled, price):
        amount = abs(float(entry.get("amount") or 0))
        filled = abs(float(filled or 0))
        full = amount <= 0 or filled >= amount - 1e-12
        title = "✅ LIMIT MIRROR FILLED" if full else "🟡 LIMIT MIRROR PARTIALLY FILLED"
        fields = [
            ("Account", self.account_name),
            ("Leader", leader),
            ("Symbol", symbol),
            ("Order side", str(entry.get("side") or "").upper()),
            ("Filled qty", filled),
        ]
        if not full:
            fields.append(("Remaining qty", max(0.0, amount - filled)))
        if price is not None:
            fields.append(("Avg fill price", price))
        if entry.get("price") is not None:
            fields.append(("Limit price", entry.get("price")))
        fields.append(("Order ID", entry.get("order_id") or entry.get("id")))
        return await self.send(self._event_message(title, fields))

    async def notify_breakeven(self, leader, symbol, profit, trigger):
        return await self.send(self._event_message("🛡️ AUTO BREAKEVEN", [
            ("Account", self.account_name),
            ("Leader", leader),
            ("Symbol", symbol),
            ("Unrealized profit", f"{self._value(float(profit))} USDT"),
            ("Stop moved to", self._value(float(trigger))),
        ], reason="Native stop moved to the position entry price.", reason_label="Note"))

    async def notify_hard_stop_error(self, equity, exc):
        return await self.send(self._event_message("❌ HARD STOP FLATTEN FAILED", [
            ("Account", self.account_name),
            ("Mode", self.mode),
            ("Account equity", f"{self._value(float(equity))} USDT"),
            ("Action", "Retrying every 5 seconds"),
        ], reason=exc, reason_label="Error"))

    async def notify_hard_stop_recovered(self, equity):
        return await self.send(self._event_message("✅ HARD STOP FLATTEN RECOVERED", [
            ("Account", self.account_name),
            ("Mode", self.mode),
            ("Account equity", f"{self._value(float(equity))} USDT"),
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
                        {"command": "unlock_hard_stop", "description": "Unlock hard stop and resume entries"},
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
                        command = text.strip().split()[0].split("@", 1)[0] if text.strip() else ""
                        if self.report_format != "text" and command in {"/positions", "/leader_positions", "/leader_positons"}:
                            try:
                                if await self._report_photo("/leader_positions" if command == "/leader_positons" else command):
                                    continue
                            except Exception:
                                log.exception("Telegram report image failed")
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
                            parse_mode="HTML",
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

    async def run_sender(self, probe_text=None):
        """Run an outbound-only bot for high-priority alerts.

        No ``getUpdates`` polling: the alert channel is write-only and must
        never compete with the control bot for commands.
        """
        if not self.token or not self.chat_id:
            raise ValueError("Telegram bot token and chat id are required")

        timeout = aiohttp.ClientTimeout(total=self.polling_timeout + 10)
        while not self._stop.is_set():
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    self.session = session
                    try:
                        if probe_text:
                            await self.send(probe_text)
                        await self._stop.wait()
                    finally:
                        self.session = None
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "Telegram alert sender failed (%s); retrying in 5 seconds",
                    type(exc).__name__,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass


class RoutingNotifier:
    """Split notifications between the trading chat and a critical-alert bot.

    Fills and skips are noise the operator can mute; hard stops and failures
    must reach the high-priority channel.  If the alert channel cannot be
    reached, critical messages fall back to the normal chat instead of being
    lost.
    """

    def __init__(self, normal=None, alerts=None):
        self.normal = normal
        self.alerts = alerts

    @staticmethod
    async def _call(target, method, args, kwargs):
        handler = getattr(target, method, None) if target is not None else None
        if handler is None:
            return False
        result = await handler(*args, **kwargs)
        # Existing notifiers return nothing on success; treat that as delivered.
        return True if result is None else bool(result)

    async def _normal_call(self, method, *args, **kwargs):
        return await self._call(self.normal, method, args, kwargs)

    async def _alert_call(self, method, *args, **kwargs):
        if await self._call(self.alerts, method, args, kwargs):
            return True
        return await self._call(self.normal, method, args, kwargs)

    async def notify_filled(self, *args, **kwargs):
        return await self._normal_call("notify_filled", *args, **kwargs)

    async def notify_accepted(self, *args, **kwargs):
        return await self._normal_call("notify_accepted", *args, **kwargs)

    async def notify_cancelled(self, *args, **kwargs):
        return await self._normal_call("notify_cancelled", *args, **kwargs)

    async def notify_limit_filled(self, *args, **kwargs):
        return await self._normal_call("notify_limit_filled", *args, **kwargs)

    async def notify_mirror_placed(self, *args, **kwargs):
        return await self._normal_call("notify_mirror_placed", *args, **kwargs)

    async def notify_mirror_cancelled(self, *args, **kwargs):
        return await self._normal_call("notify_mirror_cancelled", *args, **kwargs)

    async def notify_mirror_filled(self, *args, **kwargs):
        return await self._normal_call("notify_mirror_filled", *args, **kwargs)

    async def notify_skipped(self, *args, **kwargs):
        return await self._normal_call("notify_skipped", *args, **kwargs)

    async def notify_breakeven(self, *args, **kwargs):
        return await self._normal_call("notify_breakeven", *args, **kwargs)

    async def notify_failed(self, *args, **kwargs):
        return await self._alert_call("notify_failed", *args, **kwargs)

    async def notify_source_failed(self, *args, **kwargs):
        return await self._alert_call("notify_source_failed", *args, **kwargs)

    async def notify_hard_stop(self, *args, **kwargs):
        return await self._alert_call("notify_hard_stop", *args, **kwargs)

    async def notify_hard_stop_error(self, *args, **kwargs):
        return await self._alert_call("notify_hard_stop_error", *args, **kwargs)

    async def notify_hard_stop_recovered(self, *args, **kwargs):
        return await self._alert_call("notify_hard_stop_recovered", *args, **kwargs)

    def __getattr__(self, name):
        for target in (self.__dict__.get("normal"), self.__dict__.get("alerts")):
            if target is not None and hasattr(target, name):
                return getattr(target, name)
        raise AttributeError(name)

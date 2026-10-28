"""Bridge Binance leader snapshots to the trading engine.

The engine consumes ``dict[str, Position]`` keyed by a base-asset symbol.  The
Binance copy-trading source returns ``LeaderPosition`` rows keyed by
``(symbol, positionSide)`` with venue-style names such as ``BTCUSDT``.  This
module is the single place where those shapes are reconciled.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from decimal import Decimal

from core.models import Position
from .position_source import LeaderPosition

log = logging.getLogger(__name__)

_QUOTES = ("USDT", "USDC", "BUSD")


def base_symbol(symbol: str) -> str:
    """``BTCUSDT`` -> ``BTC``, ``1000PEPEUSDT`` -> ``1000PEPE``."""
    text = str(symbol or "").strip().upper()
    for quote in _QUOTES:
        if text.endswith(quote) and len(text) > len(quote):
            return text[: -len(quote)]
    return text


def signed_amount(position: LeaderPosition) -> Decimal:
    """Normalise Binance's hedge-mode sides into one signed quantity."""
    amount = Decimal(str(position.position_amount))
    side = str(position.position_side or "").strip().upper()
    if side == "SHORT":
        return -abs(amount)
    if side == "LONG":
        return abs(amount)
    return amount


def to_positions(items: list[LeaderPosition]) -> dict[str, Position]:
    """Convert source rows into the engine's net-position view.

    A symbol can appear twice in hedge mode.  The two sides are netted; prices
    and leverage come from the larger leg so the displayed entry/mark remain
    meaningful.
    """
    result: dict[str, Position] = {}
    for item in items or []:
        symbol = base_symbol(item.symbol)
        if not symbol:
            continue
        size = float(signed_amount(item))
        if abs(size) <= 1e-12:
            continue
        entry = float(item.entry_price or 0)
        mark = float(item.mark_price or 0)
        pnl = float(item.unrealised_pnl or 0)
        leverage = float(item.leverage or 1)
        current = result.get(symbol)
        if current is None:
            result[symbol] = Position(symbol, size, entry, mark, pnl, leverage)
            continue
        net = current.size + size
        if abs(size) > abs(current.size):
            result[symbol] = Position(symbol, net, entry, mark, current.unrealized_pnl + pnl, leverage)
        else:
            result[symbol] = Position(
                symbol, net, current.entry_price, current.mark_price,
                current.unrealized_pnl + pnl, current.leverage,
            )
    return result


def _fmt(value) -> str:
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def format_leader_positions(latest_raw: dict[str, list[LeaderPosition]], leaders: dict[str, dict]) -> str:
    """Text report used by the Telegram ``/leader_positions`` command."""
    lines: list[str] = []
    for name, cfg in leaders.items():
        lines.append(f"{name} ({cfg.get('portfolio_id', '')})")
        rows = latest_raw.get(name)
        if rows is None:
            lines.append("  Awaiting first snapshot")
            continue
        if not rows:
            lines.append("  No open positions")
            continue
        lines.append("Symbol | Direction | Leverage | Entry | Mark | Size")
        lines.append("--- | --- | --- | --- | --- | ---")
        for position in rows:
            amount = signed_amount(position)
            direction = "LONG" if amount > 0 else "SHORT"
            lines.append(
                f"{position.symbol} | {direction} | {int(position.leverage)}x | "
                f"{_fmt(position.entry_price)} | {_fmt(position.mark_price)} | {_fmt(abs(amount))}"
            )
    return "\n".join(lines) if lines else "No leaders configured."


class LeaderPoller:
    """Poll Binance leader positions and feed snapshots to the engine.

    Safety rules carried over from the previous lead runtime:

    * a source error is never treated as an empty snapshot;
    * a row missing from one snapshot is retained; two consecutive misses are
      required before the position is considered closed.
    """

    def __init__(self, engine, sources, leaders, polling=None, notifier=None):
        self.engine = engine
        self.sources = sources
        self.leaders = leaders
        polling = polling or {}
        self.interval = float(polling.get("interval_seconds", 2.5))
        self.jitter = max(0.0, float(polling.get("jitter_ms", 500)) / 1000.0)
        self.notifier = notifier
        self.latest: dict[str, dict[str, Position]] = {}
        self.latest_raw: dict[str, list[LeaderPosition]] = {}
        self.missing: dict[str, dict[str, int]] = {}
        self.errors: dict[str, int] = {}
        self.last_success: dict[str, float] = {}
        self.running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.running = True
        # The first pass runs inside the task so a slow/unavailable source
        # cannot delay the web console or Telegram startup.
        self._task = asyncio.create_task(self.run(), name="leader-poller")

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run(self) -> None:
        while self.running:
            for name in list(self.leaders):
                if not self.running:
                    break
                await self.poll_once(name)
            if self.running:
                await asyncio.sleep(self.interval + random.uniform(0, self.jitter))

    async def poll_once(self, name: str) -> None:
        cfg = self.leaders.get(name)
        if cfg is None:
            return
        if not cfg.get("enabled", True):
            self._forget(name)
            return
        source = self.sources.get(cfg.get("source"))
        portfolio_id = str(cfg.get("portfolio_id") or "").strip()
        if source is None or not portfolio_id:
            return
        try:
            raw = await source.fetch_positions(portfolio_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors[name] = self.errors.get(name, 0) + 1
            count = self.errors[name]
            if count == 1 or count == 3 or count % 20 == 0:
                log.warning(
                    "position source failed for %s (%d consecutive): %s",
                    name, count, exc,
                )
            if count == 3:
                await self._notify_source_failed(name, exc)
            return
        self.errors[name] = 0
        self.last_success[name] = time.time()
        positions = self._debounce(name, to_positions(raw))
        self.latest[name] = positions
        self.latest_raw[name] = self._merge_raw(name, raw, positions)
        try:
            await self.engine.process(name, positions)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("leader signal processing failed for %s", name)

    def _forget(self, name: str) -> None:
        """Drop all cached state for a leader that is now disabled."""
        for store in (self.latest, self.latest_raw, self.missing,
                      self.errors, self.last_success):
            store.pop(name, None)

    def _merge_raw(self, name: str, raw: list[LeaderPosition], positions: dict[str, Position]) -> list[LeaderPosition]:
        """Keep retained rows in the display snapshot during the one-miss window."""
        present = {base_symbol(item.symbol) for item in raw}
        merged = list(raw)
        if any(symbol not in present for symbol in positions):
            previous = self.latest_raw.get(name, [])
            for symbol in positions:
                if symbol in present:
                    continue
                retained = next((item for item in previous if base_symbol(item.symbol) == symbol), None)
                if retained is not None:
                    merged.append(retained)
        return merged

    def _debounce(self, name: str, current: dict[str, Position]) -> dict[str, Position]:
        previous = self.latest.get(name, {})
        streaks = self.missing.setdefault(name, {})
        merged = dict(current)
        for symbol, old_position in previous.items():
            if symbol in current:
                streaks.pop(symbol, None)
                continue
            streaks[symbol] = streaks.get(symbol, 0) + 1
            if streaks[symbol] < 2:
                merged[symbol] = old_position
            else:
                streaks.pop(symbol, None)
        return merged

    async def _notify_source_failed(self, name: str, exc: Exception) -> None:
        notify = getattr(self.notifier, "notify_source_failed", None) if self.notifier else None
        if notify is None:
            return
        try:
            await notify(name, exc)
        except Exception:
            log.warning("source failure notification failed for %s", name, exc_info=True)

    def positions(self, name: str) -> dict[str, Position] | None:
        return self.latest.get(name)

    def raw_positions(self, name: str) -> list[LeaderPosition] | None:
        return self.latest_raw.get(name)

    def status(self, name: str) -> dict:
        return {
            "last_success": self.last_success.get(name),
            "errors": self.errors.get(name, 0),
            "has_snapshot": name in self.latest,
        }

    def format_positions(self) -> str:
        return format_leader_positions(self.latest_raw, self.leaders)

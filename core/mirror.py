"""Mirror a leader's resting limit orders as post-only orders.

The entry path in ``core.engine`` is driven by position snapshots, so it can
only copy a fill after the fact and always as a taker.  This module adds the
proactive half for leaders that opt in with ``mirror_orders: true``:

* watch the leader's plain limit orders (never triggers),
* keep at most ``max_orders`` (default two: nearest above and nearest below
  the mark) of our own post-only orders at the exact leader prices,
* cancel ours when the leader cancels, keep ours resting when the leader
  fills (the user asked for "unfilled stays resting"),
* credit maker entries so the snapshot engine does not execute them a second
  time.

All placement/cancellation is serialized on ``engine._lock`` so a mirrored
order can never interleave with a snapshot-driven order for the same symbol.
"""

import asyncio
import logging
import time
from dataclasses import replace

import aiohttp

from exchanges.base import OrderQuantityTooSmall
from sources.leader_orders import (
    LeaderFillBook,
    LeaderRestingOrder,
    fetch_leader_orders,
    session_timeout,
)
from sources.position_queries import _perp_dexes

log = logging.getLogger(__name__)

_ORDER_GONE_TOKENS = (
    "ORDER_NOT_FOUND", "ORDER NOT FOUND", "NOT_FOUND", "NOT FOUND",
    "UNKNOWN_ORDER", "UNKNOWN ORDER", "ALREADY FILLED", "ALREADY CLOSED",
    "ALREADY CANCELED", "ALREADY CANCELLED", "NEVER PLACED",
    "NOT OPEN", "DOES NOT EXIST", "NO SUCH ORDER",
)


def _order_gone(exc):
    message = str(exc).upper()
    return any(token in message for token in _ORDER_GONE_TOKENS)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class OrderMirror:
    """Per-leader post-only mirror of the leader's resting limit orders."""

    def __init__(self, engine, config=None):
        self.engine = engine
        self.config = config if isinstance(config, dict) else {}
        self.enabled = bool(self.config.get("enabled", False))
        self.max_orders = max(1, int(self.config.get("max_orders", 2) or 2))
        self.post_only = bool(self.config.get("post_only", True))
        self.shadow = bool(self.config.get("shadow", False))
        self.refresh_seconds = max(1.0, _float(self.config.get("refresh_seconds"), 5.0))
        self.orders_seconds = max(2.0, _float(self.config.get("leader_orders_seconds"), 10.0))
        # Empty HIP-3 dexes are re-probed slowly; only dexes that actually hold
        # orders are polled every cycle.  A full scan of every dex each cycle
        # would exceed Hyperliquid's 1200 weight/min IP budget.
        self.probe_seconds = max(self.orders_seconds,
                                 _float(self.config.get("dex_probe_seconds"), 120.0))
        self.grace_ms = max(0, int(_float(self.config.get("credit_grace_ms"), 300)))
        self.credit_ttl = max(60.0, _float(self.config.get("credit_ttl_seconds"), 900.0))
        self.orphan_ttl = max(1.0, _float(self.config.get("orphan_ttl_seconds"), 300.0))
        raw_orphans = self.config.get("max_orphans", 2)
        self.max_orphans = max(0, int(2 if raw_orphans is None else raw_orphans))
        self.min_sync_seconds = max(0.25, _float(self.config.get("min_sync_seconds"), 1.0))
        self.failure_cooldown = max(1.0, _float(
            self.config.get("failure_cooldown_seconds"), 30.0))
        self._last_sync = 0.0

        # (leader, dex) -> [LeaderRestingOrder]; keyed per dex so a failed dex
        # never wipes the orders learnt from the others.
        self._books = {}
        self._book_at = {}
        self._dex_at = {}
        # f"{leader}:{leader_oid}" -> mirrored order record
        self.orders = {}
        # (leader, symbol) -> (signed leader size not yet consumed by a diff)
        self.credits = {}
        self.fills = LeaderFillBook()
        self._last_fill_at = 0.0
        # Fills older than the floor predate the current position baseline
        # (startup/resync) and must not be credited.
        self._fill_floor = {}
        self._sync_task = None
        self._force_orders = False
        self._stop = False
        self._failed_at = {}
        self._warned_late_entry = set()
        # leader order ids the venue reported as fully filled via orderUpdates,
        # used when the userFills push has not arrived yet.
        self._filled_oids = {}
        # (leader, oid) -> wall time we actually mirrored that leader order.
        # Kept after our record is popped on fill so a userFills event that
        # arrives later still credits the mirrored fill instead of placing a
        # duplicate market order.
        self._mirrored_oids = {}

    # -- configuration -----------------------------------------------------
    def enabled_for(self, leader):
        cfg = self.engine.leaders.get(leader) or {}
        return (self.enabled
                and bool(cfg.get("mirror_orders", False))
                and bool(cfg.get("enabled", True)))

    def leaders(self):
        return [name for name in self.engine.leaders if self.enabled_for(name)]

    # -- lifecycle ---------------------------------------------------------
    async def startup(self):
        # Restored mirrors may carry stale prices/sizes; drop them even when
        # the feature was disabled since the last run.
        await self._cancel_persisted()
        self.set_floor()
        if not self.enabled:
            return
        if self.leaders():
            await self.sync(force_orders=True)

    def set_floor(self, leaders=None):
        """Ignore fills that predate the position baseline being built now."""
        now = time.time()
        for leader in (leaders if leaders is not None else self.leaders()):
            self._fill_floor[leader] = now

    async def _cancel_persisted(self):
        """Drop mirrored orders restored from state; prices/sizes are stale."""
        persisted = self.engine.runtime_state.get("mirror_orders") or {}
        exchange = self.engine.exchange
        for item in list(persisted.values()):
            order_id = item.get("order_id")
            if not order_id or self.shadow:
                continue
            try:
                await exchange.cancel_order(order_id, item.get("symbol"))
            except Exception as exc:
                if not _order_gone(exc):
                    log.warning("Failed to cancel restored mirror order %s for %s",
                                order_id, item.get("symbol"), exc_info=True)
        self.orders = {}
        self.engine.runtime_state["mirror_orders"] = {}
        self.engine._save_state()

    def stop(self):
        self._stop = True
        task = self._sync_task
        if task is not None and not task.done():
            task.cancel()

    async def run(self):
        if not self.enabled:
            return
        while not self._stop:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Order mirror refresh failed")
            try:
                await asyncio.sleep(self.refresh_seconds)
            except asyncio.CancelledError:
                raise

    # -- feeds -------------------------------------------------------------
    def on_fills(self, leader, raw_fills):
        """Consume a ``userFills`` event (websocket or REST backfill)."""
        if not self.enabled_for(leader):
            return
        added = self.fills.add(leader, raw_fills)
        if not added:
            return
        # ``userFills`` sends up to 100 historical fills on subscribe.  Only
        # fills that happened after the current baseline started may be
        # credited.  Do not gate on ``previous_at``: the state snapshot and
        # the fill push race, and a fill is often timestamped just before the
        # snapshot that reacts to it.
        floor = self._fill_floor.get(leader, 0.0)
        for fill in added:
            if fill.time < floor:
                continue
            # The cached book is up to ``leader_orders_seconds`` stale; apply
            # the fill immediately so a rest/next-order decision is not made
            # against an already-executed order.
            self._apply_fill_to_book(leader, fill)
            if fill.crossed:
                # Taker fill: the snapshot engine copies it with a market order.
                continue
            if not self._mirrored_oid(leader, fill.oid):
                # We never had a mirror for this order (not selected, placed
                # too late, or cancelled).  Crediting it would make the
                # snapshot engine skip a market order it still has to place.
                continue
            self._add_credit(leader, fill.symbol, fill.signed_size)
        self._last_fill_at = time.monotonic()
        self.request_sync()

    def on_order_updates(self, leader, updates):
        """Apply low-latency ``orderUpdates`` pushes (single mirrored leader).

        The channel carries no user field, so the websocket only subscribes to
        it when exactly one leader is mirrored; attribution is then unambiguous.
        """
        if not self.enabled_for(leader):
            return
        if isinstance(updates, dict):
            updates = [updates]
        changed = False
        for update in updates or []:
            if not isinstance(update, dict):
                continue
            order = update.get("order") or {}
            status = str(update.get("status") or "").lower()
            oid = order.get("oid")
            if oid is None:
                continue
            oid = str(oid)
            if status == "open":
                coin = str(order.get("coin") or "").upper()
                price = _float(order.get("limitPx"))
                size = _float(order.get("sz"))
                if (not coin or price <= 0 or size <= 0
                        or order.get("isTrigger")
                        or str(order.get("orderType") or "Limit") != "Limit"):
                    self._remove_order(leader, oid)
                    changed = True
                    continue
                self._upsert_order(leader, LeaderRestingOrder(
                    leader=leader, dex=None, symbol=coin, oid=oid,
                    side="buy" if str(order.get("side")).upper() == "B" else "sell",
                    price=price, size=size, mark=self._mark_for(leader, coin),
                    reduce_only=bool(order.get("reduceOnly")),
                    timestamp=_float(order.get("timestamp")) / 1000.0,
                ))
                changed = True
            elif status == "filled":
                self._remove_order(leader, oid)
                self._filled_oids[(leader, oid)] = time.time()
                changed = True
            elif status:
                self._remove_order(leader, oid)
                self._filled_oids.pop((leader, oid), None)
                changed = True
        if changed:
            self.request_sync()

    def _mark_for(self, leader, symbol):
        position = self.engine.previous.get(leader, {}).get(symbol.upper())
        if position is not None and getattr(position, "mark_price", 0) > 0:
            return float(position.mark_price)
        for (name, _dex), rows in self._books.items():
            if name != leader:
                continue
            for order in rows:
                if order.symbol.upper() == symbol.upper() and order.mark > 0:
                    return order.mark
        return 0.0

    def _upsert_order(self, leader, order):
        for key, rows in self._books.items():
            if key[0] != leader:
                continue
            for index, existing in enumerate(rows):
                if existing.oid != order.oid:
                    continue
                updated = list(rows)
                updated[index] = order
                self._books[key] = updated
                return
        self._books.setdefault((leader, order.dex), []).append(order)

    def _remove_order(self, leader, oid):
        for key in list(self._books):
            if key[0] != leader:
                continue
            rows = self._books[key]
            filtered = [order for order in rows if order.oid != oid]
            if len(filtered) != len(rows):
                self._books[key] = filtered

    def _apply_fill_to_book(self, leader, fill):
        """Reduce or drop the leader order that just executed."""
        if not fill.oid:
            return
        for key in list(self._books):
            if key[0] != leader:
                continue
            rows = self._books[key]
            if not any(order.oid == fill.oid for order in rows):
                continue
            updated = []
            for order in rows:
                if order.oid != fill.oid:
                    updated.append(order)
                    continue
                remaining = order.size - fill.size
                if remaining > 1e-12:
                    updated.append(replace(order, size=remaining))
            self._books[key] = updated
            return

    def _mirrored_oid(self, leader, oid):
        if not oid:
            return False
        if any(item.get("leader") == leader and item.get("leader_oid") == oid
               for item in self.orders.values()):
            return True
        at = self._mirrored_oids.get((leader, str(oid)))
        if at is None:
            return False
        if time.time() - at > self.credit_ttl:
            self._mirrored_oids.pop((leader, str(oid)), None)
            return False
        return True

    def _remember_mirrored(self, leader, oid):
        if oid is not None:
            self._mirrored_oids[(leader, str(oid))] = time.time()

    def _forget_mirrored(self, leader, oid):
        if oid is not None:
            self._mirrored_oids.pop((leader, str(oid)), None)

    def _add_credit(self, leader, symbol, signed_size):
        if not signed_size:
            return
        key = (leader, symbol.upper())
        size, _ = self.credits.get(key, (0.0, 0.0))
        self.credits[key] = (size + signed_size, time.time())

    def consume_credit(self, leader, symbol, signed_want):
        """Consume a matching-sign credit; returns the leader-unit amount used."""
        key = (leader, symbol.upper())
        item = self.credits.get(key)
        if not item or not signed_want:
            return 0.0
        size, at = item
        if time.time() - at > self.credit_ttl:
            self.credits.pop(key, None)
            return 0.0
        if (size > 0) != (signed_want > 0):
            return 0.0
        used = min(abs(size), abs(signed_want))
        signed = used if size > 0 else -used
        remainder = size - signed
        if abs(remainder) <= 1e-12:
            self.credits.pop(key, None)
        else:
            self.credits[key] = (remainder, at)
        return signed

    def should_wait_for_fills(self, leader, symbol, signed_want):
        """True only when a live mirror could be filling right now.

        The leader order must still be in the cached book; once it is gone we
        already know whether it filled or was cancelled and waiting cannot
        help.  Resting orphans are excluded: the leader is done with theirs.
        """
        if self.grace_ms <= 0:
            return False
        side = "buy" if signed_want > 0 else "sell"
        book = self._book(leader)
        for item in self.orders.values():
            if (item.get("leader") == leader
                    and str(item.get("symbol", "")).upper() == symbol.upper()
                    and item.get("side") == side
                    and not item.get("orphan")
                    and float(item.get("filled") or 0) < float(item.get("amount") or 0)
                    and item.get("leader_oid") in book):
                return True
        return False

    async def wait_for_fills(self, milliseconds):
        if milliseconds <= 0:
            return
        deadline = time.monotonic() + milliseconds / 1000.0
        start = self._last_fill_at
        while self._last_fill_at <= start and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

    def clear_credits(self):
        self.credits.clear()
        self.set_floor()

    def forget_credit(self, leader, symbol):
        """Drop a stale credit once the snapshot path has acted on the delta."""
        self.credits.pop((leader, symbol.upper()), None)

    # -- refresh / reconcile ----------------------------------------------
    def request_sync(self, force_orders=False):
        if not self.enabled:
            return
        if force_orders:
            self._force_orders = True
        task = self._sync_task
        if task is not None and not task.done():
            return
        self._sync_task = asyncio.create_task(self._debounced_sync())

    async def _debounced_sync(self):
        try:
            await asyncio.sleep(0.25)
            await self.sync(force_orders=self._force_orders)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Order mirror sync failed")
        finally:
            self._force_orders = False

    async def sync(self, force_orders=False):
        if not self.enabled:
            return
        leaders = self.leaders()
        if not leaders:
            return
        now = time.monotonic()
        if not force_orders and now - self._last_sync < self.min_sync_seconds:
            return
        self._last_sync = now
        await self._refresh_books(leaders, force=force_orders)
        cutoff = time.time() - 3600.0
        for key in [key for key, at in self._failed_at.items() if at < cutoff]:
            self._failed_at.pop(key, None)
        for key in [key for key, at in self._filled_oids.items() if at < cutoff]:
            self._filled_oids.pop(key, None)
        mirrored_cutoff = time.time() - self.credit_ttl
        for key in [key for key, at in self._mirrored_oids.items()
                    if at < mirrored_cutoff]:
            self._mirrored_oids.pop(key, None)
        for leader in leaders:
            try:
                # Read-only network work happens before taking the lock so a
                # slow venue call does not delay every leader's market copy.
                states = await self._fetch_own_states(leader)
                ratio = (await self.engine.sizing(leader))[0]
            except Exception:
                log.warning("Order mirror prepare failed for %s", leader, exc_info=True)
                continue
            if ratio <= 0:
                continue
            try:
                async with self.engine._locked(f'mirror sync {leader}'):
                    await self._reconcile_leader(leader, states, ratio)
                    self.engine._save_state()
            except Exception:
                log.warning("Order mirror reconcile failed for %s", leader, exc_info=True)

    async def _refresh_books(self, leaders, force=False):
        now = time.monotonic()
        due = [name for name in leaders
               if force or now - self._book_at.get(name, 0.0) >= self.orders_seconds]
        if not due:
            return
        async with aiohttp.ClientSession(timeout=session_timeout()) as session:
            all_dexes = await _perp_dexes(session)
            for leader in due:
                cfg = self.engine.leaders.get(leader) or {}
                address = cfg.get("address")
                if isinstance(address, int):
                    address = hex(address)
                if not address:
                    continue
                dexes = [
                    dex for dex in all_dexes
                    if dex is None
                    or self._books.get((leader, dex))
                    or now - self._dex_at.get((leader, dex), 0.0) >= self.probe_seconds
                ]
                if not dexes:
                    continue
                try:
                    books = await fetch_leader_orders(
                        leader, str(address), session, dexes=dexes)
                except Exception:
                    log.warning("Leader order refresh failed for %s", leader, exc_info=True)
                    continue
                if books is None:
                    continue
                for dex, rows in books.items():
                    self._books[(leader, dex)] = rows
                    self._dex_at[(leader, dex)] = now
                self._book_at[leader] = now
                log.debug("MIRROR BOOK leader=%s orders=%d", leader,
                          sum(len(rows) for rows in books.values()))

    def _book(self, leader):
        merged = {}
        for (name, _dex), rows in self._books.items():
            if name != leader:
                continue
            for order in rows:
                merged[order.oid] = order
        return merged

    def _skip_reason(self, leader, order):
        if order.mark <= 0:
            return "no mark price"
        if self.engine.symbol_map.is_skipped(order.symbol):
            return "symbol is mapped to skip"
        cfg = self.engine.leaders.get(leader) or {}
        blacklist = cfg.get("symbol_blacklist") or []
        if order.symbol in blacklist or order.symbol + "USDT" in blacklist:
            return "symbol is blacklisted"
        if not order.reduce_only and f"{leader}:{order.symbol}" in self.engine.stopped:
            return "stopped out; waiting for the leader to close and reopen"
        owner_key = self.engine._owner_key(leader, order.symbol)
        owner = self.engine.owners.get(owner_key)
        if owner and owner != leader:
            return f"symbol is owned by leader {owner}"
        return None

    def _select(self, candidates):
        """Nearest order above and nearest below the mark.

        ``max_orders`` is a total across all symbols: 1 keeps only the single
        nearest order, 2 keeps one above plus one below.
        """
        if not candidates:
            return []
        above = [order for order in candidates if order.above]
        below = [order for order in candidates if not order.above]
        picks = []
        if below:
            picks.append(min(below, key=lambda order: order.distance))
        if above:
            picks.append(min(above, key=lambda order: order.distance))
        if self.max_orders == 1:
            return [min(candidates, key=lambda order: order.distance)]
        return picks[:self.max_orders]

    async def _reconcile_leader(self, leader, states, ratio):
        """Decide and place/cancel while holding ``engine._lock``.

        ``states`` and ``ratio`` were read outside the lock by ``sync``.
        """
        await self._poll_own_orders(leader, states)
        book = self._book(leader)
        if ratio <= 0:
            return
        paused = bool(self.engine.paused or self.engine.risk.locked)
        candidates = []
        for order in book.values():
            if self._skip_reason(leader, order):
                continue
            candidates.append(order)
        desired = [] if paused else self._select(candidates)
        await self._apply(leader, desired, book, ratio, paused)

    async def _apply(self, leader, desired, book, ratio, paused):
        desired_by_oid = {order.oid: order for order in desired}
        current = [item for item in self.orders.values() if item.get("leader") == leader]
        for item in list(current):
            key = self._key(item)
            if key not in self.orders:
                continue
            leader_oid = item.get("leader_oid")
            order = book.get(leader_oid)
            if order is None:
                fully = self._leader_order_filled(item)
                if fully:
                    if paused and not item.get("reduce_only"):
                        # Blocking new exposure must also drop a resting entry
                        # whose leader order has already filled.
                        await self._cancel(item, "new entries are paused")
                        continue
                    if item.get("orphan"):
                        reason = self._orphan_reason(leader, item)
                        if reason:
                            await self._cancel(item, reason)
                            continue
                        continue
                    # The leader filled; leave our mirror resting until it fills
                    # too ("没成交就一直挂着").
                    item["orphan"] = True
                    item["orphaned_at"] = time.time()
                    log.info("MIRROR KEEP leader=%s symbol=%s: leader filled, ours resting",
                             leader, item.get("symbol"))
                    continue
                await self._cancel(item, "leader cancelled the order")
                continue
            if paused:
                if order.reduce_only:
                    # Blocking new exposure must not cancel a de-risking order.
                    await self._sync_item(item, order, ratio)
                    continue
                await self._cancel(item, "new entries are paused")
                continue
            if leader_oid not in desired_by_oid:
                await self._cancel(item, "replaced by a closer order")
                continue
            await self._sync_item(item, order, ratio)

        orphans = [item for item in self.orders.values()
                   if item.get("leader") == leader and item.get("orphan")]
        if len(orphans) > self.max_orphans:
            orphans.sort(key=lambda item: float(
                item.get("orphaned_at") or item.get("created_at") or 0.0))
            for item in orphans[:len(orphans) - self.max_orphans]:
                await self._cancel(item, "too many resting orphans")

        live = [item for item in self.orders.values() if item.get("leader") == leader]
        # Orphans are orders the leader already filled; they must not eat the
        # slots that replenish the next nearest order on either side.
        active = [item for item in live if not item.get("orphan")]
        slots = max(0, self.max_orders - len(active))
        if slots <= 0:
            return
        now = time.time()
        for order in desired:
            if slots <= 0:
                break
            if any(item.get("leader_oid") == order.oid and not item.get("orphan")
                   for item in live):
                continue
            if now - self._failed_at.get((leader, order.oid), 0.0) < self.failure_cooldown:
                continue
            placed = await self._place(leader, order, ratio)
            if placed is not None:
                slots -= 1

    def _leader_order_filled(self, item):
        """True when the leader's order went away *because it filled*.

        An order that partially filled and then had its remainder cancelled
        must cancel our mirror too; only a complete fill keeps it resting.
        """
        leader = item.get("leader")
        oid = item.get("leader_oid")
        created = item.get("created_at", 0.0)
        size = float(item.get("leader_size") or 0.0)
        fills = self.fills.fills_for_oid(leader, oid, since=created)
        if any(fill.crossed for fill in fills):
            # The leader's order crossed the spread.  Our post-only order at
            # the same price is no longer a faithful copy; let it go.
            return False
        if (leader, oid) in self._filled_oids:
            return True
        filled = sum(fill.size for fill in fills)
        if size <= 0:
            # Older state had no leader size; prefer keeping our order over a
            # wrong cancellation.
            return bool(fills)
        return filled >= size - 1e-9

    def _orphan_reason(self, leader, item):
        """Reason to cancel a resting orphan, or ``None`` to keep it."""
        if item.get("reduce_only"):
            return None
        orphaned_at = float(item.get("orphaned_at") or item.get("created_at") or 0.0)
        if orphaned_at and time.time() - orphaned_at > self.orphan_ttl:
            return "orphan expired"
        # ``previous`` only tells us where the leader stood at its last
        # snapshot.  Right after an entry fill it still shows the pre-fill
        # position (often flat), so only trust it once a snapshot newer than
        # the orphan itself has been processed.
        seen_at = float(self.engine.previous_at.get(leader) or 0.0)
        if seen_at <= orphaned_at:
            return None
        previous = self.engine.previous.get(leader) or {}
        position = previous.get(str(item.get("symbol", "")).upper())
        if position is None or abs(float(getattr(position, "size", 0.0))) <= 1e-12:
            return "leader is flat"
        if (float(position.size) > 0) != (item.get("side") == "buy"):
            return "leader reversed"
        return None

    async def cancel_orphans(self, leader, symbol, reason):
        """Cancel this leader+symbol's non-reduce-only resting orphans.

        The caller must hold ``engine._lock`` (``Engine.process`` does).
        """
        for item in [item for item in self.orders.values()
                     if item.get("leader") == leader
                     and str(item.get("symbol", "")).upper() == symbol.upper()
                     and item.get("orphan")
                     and not item.get("reduce_only")]:
            await self._cancel(item, reason)

    async def _sync_item(self, item, order, ratio):
        target = abs(order.size) * ratio
        filled = float(item.get("filled") or 0.0)
        remaining = max(0.0, target - filled)
        current = float(item.get("amount") or 0.0)
        if remaining <= 0:
            await self._cancel(item, "no quantity left to mirror")
            return
        if abs(remaining - current) <= max(1e-12, target * 0.005):
            return
        if await self._cancel(item, "leader size changed"):
            await self._place(item.get("leader"), order, ratio, amount=remaining)

    async def _place(self, leader, order, ratio, amount=None):
        exchange = self.engine.exchange
        target = abs(order.size) * ratio if amount is None else abs(amount)
        if target <= 0:
            return None
        signed = 1 if order.side == "buy" else -1
        try:
            live = await exchange.position_size(order.symbol)
        except Exception:
            log.warning("MIRROR SKIP %s %s: position probe failed",
                        leader, order.symbol, exc_info=True)
            self._failed_at[(leader, order.oid)] = time.time()
            return None
        if order.reduce_only:
            if abs(live) <= 1e-12 or (live > 0) == (signed > 0):
                log.info("MIRROR SKIP leader=%s symbol=%s: reduce-only %s has no matching position",
                         leader, order.symbol, order.side)
                self._failed_at[(leader, order.oid)] = time.time()
                return None
            target = min(target, abs(live))
        elif live and (live > 0) != (signed > 0):
            log.info("MIRROR SKIP leader=%s symbol=%s: opposite position exists",
                     leader, order.symbol)
            self._failed_at[(leader, order.oid)] = time.time()
            return None
        if target <= 0:
            return None
        claimed = False
        if not order.reduce_only:
            ok, owner = await self.engine._claim_or_check(leader, order.symbol)
            if not ok:
                log.info("MIRROR SKIP leader=%s symbol=%s: owned by %s",
                         leader, order.symbol, owner)
                self._failed_at[(leader, order.oid)] = time.time()
                return None
            claimed = True
        if self.shadow:
            log.info("MIRROR SHADOW leader=%s symbol=%s side=%s qty=%.10g price=%g",
                     leader, order.symbol, order.side, target, order.price)
            if claimed:
                await self._release_unused(leader, order.symbol)
            return None
        try:
            placed = await exchange.create_order(
                order.symbol, order.side, target, "limit", order.price,
                bool(order.reduce_only), post_only=self.post_only,
            )
        except OrderQuantityTooSmall as exc:
            log.info("MIRROR SKIP leader=%s symbol=%s: quantity below minimum (%s)",
                     leader, order.symbol, exc)
            self._failed_at[(leader, order.oid)] = time.time()
            if claimed:
                await self._release_unused(leader, order.symbol)
            return None
        except Exception as exc:
            log.warning("MIRROR FAILED leader=%s symbol=%s price=%g: %s",
                        leader, order.symbol, order.price, exc)
            self._failed_at[(leader, order.oid)] = time.time()
            if claimed:
                await self._release_unused(leader, order.symbol)
            return None
        self._failed_at.pop((leader, order.oid), None)
        # Remember that this leader order really was mirrored so a maker fill
        # is credited even if our record is popped by the fill itself.
        self._remember_mirrored(leader, order.oid)
        key = self._key_for(leader, order.oid)
        if str(placed.status) != "open":
            item = {"leader": leader, "leader_oid": order.oid, "symbol": order.symbol,
                    "side": order.side, "price": order.price, "amount": target,
                    "reduce_only": bool(order.reduce_only), "order_id": str(placed.id),
                    "filled": 0.0}
            await self._book_fill(item, float(placed.amount or target),
                                  float(placed.price or order.price))
            await self.engine._notify(
                "notify_mirror_filled", leader, order.symbol, item,
                float(placed.amount or target), float(placed.price or order.price))
            return None
        item = {
            "leader": leader,
            "leader_oid": order.oid,
            "symbol": order.symbol,
            "side": order.side,
            "price": float(order.price),
            "amount": float(target),
            "leader_size": float(order.size),
            "order_id": str(placed.id),
            "reduce_only": bool(order.reduce_only),
            "filled": 0.0,
            "created_at": time.time(),
            "orphan": False,
        }
        self.orders[key] = item
        self.engine._save_state()
        log.info("MIRROR PLACED leader=%s symbol=%s side=%s qty=%.10g price=%g id=%s",
                 leader, order.symbol, order.side, target, order.price, placed.id)
        await self.engine._notify(
            "notify_mirror_placed", leader, order.symbol, order.side, target, order.price)
        return item

    async def _release_unused(self, leader, symbol):
        """Drop a claim made for a mirror order that was never placed."""
        try:
            await self.engine._release_if_flat(leader, symbol)
        except Exception:
            log.warning("MIRROR RELEASE failed for %s %s", leader, symbol, exc_info=True)

    async def _fetch_own_states(self, leader):
        """Read our mirror order states.  Network only, called outside the lock.

        A key that could not be read is simply absent; ``_poll_own_orders``
        then leaves that record untouched, exactly like a failed poll used to.
        """
        exchange = self.engine.exchange
        fetcher = getattr(exchange, "fetch_order", None)
        states = {}
        if fetcher is None:
            return states
        for key, item in list(self.orders.items()):
            if item.get("leader") != leader:
                continue
            order_id = item.get("order_id")
            symbol = item.get("symbol")
            if not order_id or not symbol:
                continue
            try:
                states[key] = await fetcher(order_id, symbol)
            except NotImplementedError:
                continue
            except Exception:
                log.warning("MIRROR POLL failed for %s %s", leader, symbol, exc_info=True)
                continue
        return states

    async def _poll_own_orders(self, leader, states):
        for key, item in list(self.orders.items()):
            if item.get("leader") != leader or key not in self.orders:
                continue
            order_id = item.get("order_id")
            symbol = item.get("symbol")
            if not order_id or not symbol:
                self.orders.pop(key, None)
                continue
            if key not in states:
                # Not fetched this cycle (unsupported or failed); keep it.
                continue
            state = states[key]
            if state is None:
                await self._book_missing(item)
                self.orders.pop(key, None)
                self.engine._save_state()
                continue
            status = str(state.get("status") or "").lower()
            filled = max(0.0, float(state.get("filled") or 0.0))
            previous = float(item.get("filled") or 0.0)
            if filled > previous + 1e-12:
                await self._book_fill(item, filled - previous,
                                      float(state.get("average") or item.get("price") or 0.0))
                item["filled"] = filled
            done = status in ("closed", "filled") or (
                filled > 0 and float(state.get("remaining") or 0.0) <= 1e-12)
            if done:
                self.orders.pop(key, None)
                self.engine._save_state()
                await self.engine._notify(
                    "notify_mirror_filled", leader, symbol, item, filled,
                    float(state.get("average") or item.get("price") or 0.0))
            elif status in ("canceled", "cancelled", "rejected", "expired"):
                self.orders.pop(key, None)
                self._forget_mirrored(leader, item.get("leader_oid"))
                self.engine._save_state()
                await self.engine._notify(
                    "notify_mirror_cancelled", leader, symbol, item, f"exchange reported {status}")

    async def _book_missing(self, item):
        """Handle an order the venue no longer knows about (filled or cancelled)."""
        leader = item.get("leader")
        symbol = item.get("symbol")
        reduce_only = bool(item.get("reduce_only"))
        if reduce_only:
            try:
                await self.engine._release_if_flat(leader, symbol)
            except Exception:
                log.warning("MIRROR RELEASE failed for %s %s", leader, symbol, exc_info=True)
            return
        try:
            live = await self.engine.exchange.position_size(symbol)
        except Exception:
            return
        if abs(live) <= 1e-12:
            return
        log.info("MIRROR RECOVERED leader=%s symbol=%s: order gone but position exists",
                 leader, symbol)
        await self._book_fill(item, abs(live), float(item.get("price") or 0.0))

    async def _book_fill(self, item, quantity, price):
        quantity = max(0.0, float(quantity or 0.0))
        if quantity <= 0:
            return
        engine = self.engine
        leader = item.get("leader")
        symbol = item.get("symbol")
        reduce_only = bool(item.get("reduce_only"))
        if not reduce_only:
            ok, owner = await engine._claim_or_check(leader, symbol)
            if not ok:
                log.info("MIRROR FILL %s %s ignored: owned by %s", leader, symbol, owner)
                return
            try:
                live = await engine.exchange.position_size(symbol)
            except Exception:
                live = None
            if live is not None and abs(live) > 1e-12:
                engine.entered[f"{leader}:{symbol}"] = True
                engine._save_state()
            try:
                await engine._sync_protection(leader, symbol)
            except Exception:
                log.warning("MIRROR PROTECTION failed for %s %s", leader, symbol, exc_info=True)
        else:
            try:
                await engine._release_if_flat(leader, symbol)
            except Exception:
                log.warning("MIRROR RELEASE failed for %s %s", leader, symbol, exc_info=True)
        engine.fill_events.append({
            "account": engine._account_key(leader),
            "leader": leader,
            "order_id": str(item.get("order_id") or ""),
            "symbol": symbol,
            "side": item.get("side"),
            "quantity": quantity,
            "price": float(price or 0.0),
            "reduce_only": reduce_only,
            "timestamp": time.time(),
        })
        log.info("MIRROR FILLED leader=%s symbol=%s qty=%.10g price=%g id=%s",
                 leader, symbol, quantity, price, item.get("order_id"))

    async def _book_gone_fill(self, item):
        """Book a fill the venue reports when a cancel finds the order gone.

        Returns True when it booked a positive quantity.  Deliberately unlike
        ``_book_missing``: the account may already hold a position, so only the
        order's own reported fill is booked here.
        """
        order_id = item.get("order_id")
        fetcher = getattr(self.engine.exchange, "fetch_order", None)
        if not order_id or fetcher is None:
            return False
        try:
            state = await fetcher(order_id, item.get("symbol"))
        except NotImplementedError:
            return False
        except Exception:
            return False
        if not state:
            return False
        filled = max(0.0, float(state.get("filled") or 0.0))
        previous = float(item.get("filled") or 0.0)
        if filled <= previous + 1e-12:
            return False
        await self._book_fill(
            item, filled - previous,
            float(state.get("average") or item.get("price") or 0.0))
        item["filled"] = filled
        return True

    async def _cancel(self, item, reason):
        key = self._key(item)
        order_id = item.get("order_id")
        if order_id and not self.shadow:
            try:
                await self.engine.exchange.cancel_order(order_id, item.get("symbol"))
                # A cancelled mirror did not fill, so a later leader fill for
                # this order must go through the normal market path.
                self._forget_mirrored(item.get("leader"), item.get("leader_oid"))
            except Exception as exc:
                if not _order_gone(exc):
                    log.warning("Failed to cancel mirror order %s for %s (%s)",
                                order_id, key, reason, exc_info=True)
                    return False
                # The order may have filled instead of being cancelled.  Ask
                # the venue once and book the fill before dropping the record.
                booked = await self._book_gone_fill(item)
                if not booked:
                    self._forget_mirrored(item.get("leader"), item.get("leader_oid"))
        self.orders.pop(key, None)
        if not item.get("reduce_only") and not float(item.get("filled") or 0.0):
            # A cancelled, never-filled entry must not keep the symbol claimed.
            await self._release_unused(item.get("leader"), item.get("symbol"))
        self.engine._save_state()
        log.info("MIRROR CANCELLED leader=%s symbol=%s id=%s (%s)",
                 item.get("leader"), item.get("symbol"), order_id, reason)
        await self.engine._notify(
            "notify_mirror_cancelled", item.get("leader"), item.get("symbol"), item, reason)
        return True

    async def drop_leader(self, leader):
        """Forget a leader's book and cancel its mirrored orders."""
        async with self.engine._lock:
            await self.drop_leader_locked(leader)

    async def drop_leader_locked(self, leader):
        """``drop_leader`` for callers that already hold ``engine._lock``."""
        for item in [entry for entry in self.orders.values()
                     if entry.get("leader") == leader]:
            await self._cancel(item, "leader disabled or address changed")
        for key in [key for key in self._books if key[0] == leader]:
            self._books.pop(key, None)
        for key in [key for key in self._dex_at if key[0] == leader]:
            self._dex_at.pop(key, None)
        self._book_at.pop(leader, None)
        for key in [key for key in self.credits if key[0] == leader]:
            self.credits.pop(key, None)
        for key in [key for key in self._filled_oids if key[0] == leader]:
            self._filled_oids.pop(key, None)
        for key in [key for key in self._mirrored_oids if key[0] == leader]:
            self._mirrored_oids.pop(key, None)
        self._fill_floor[leader] = time.time()
        self.engine._save_state()

    # -- persistence -------------------------------------------------------
    def snapshot(self):
        keys = ("leader", "leader_oid", "symbol", "side", "price", "amount",
                "leader_size", "order_id", "reduce_only", "filled", "created_at",
                "orphan", "orphaned_at")
        return {key: {field: item.get(field) for field in keys}
                for key, item in self.orders.items()}

    @staticmethod
    def _key(item):
        return f"{item.get('leader')}:{item.get('leader_oid')}"

    @staticmethod
    def _key_for(leader, oid):
        return f"{leader}:{oid}"

import asyncio
import logging
import time
from dataclasses import replace
from .detector import diff_positions
from .symbols import SymbolMap
from exchanges.base import OrderQuantityTooSmall
from risk.manager import RiskManager

log = logging.getLogger(__name__)

class Engine:
    def __init__(self, exchange, leaders, account_state=None, notifier=None, state_store=None,
                 symbol_map=None, auto_breakeven=None, stop_out_cooldown=600):
        self.exchange = exchange
        self.leaders = leaders
        self.state = account_state
        self.notifier = notifier
        self.previous = {}
        self.previous_at = {}
        self.paused = False
        self._lock = asyncio.Lock()
        self._protection_lock = asyncio.Lock()
        self.state_store = state_store
        self.symbol_map = symbol_map if isinstance(symbol_map, SymbolMap) else SymbolMap(symbol_map)
        self.config_manager = None
        self.runtime_state = state_store.load() if state_store else {}
        self.risk = RiskManager(self.exchange.config.get('hard_stop'), self.runtime_state.get('risk'))
        self._risk_state_saved = None
        self.owners = {}
        if state_store:
            self.owners = self.runtime_state.get('symbol_owners', {})
        # Symbols where the account was closed out (native stop, liquidation,
        # manual close) while the leader still held.  Re-entry is suppressed for
        # a bounded cooldown so an incremental add does not create a fresh
        # position right after a stop, while a leader that keeps holding the
        # same side cannot suppress the symbol forever.
        self.stopped = self.runtime_state.get('stopped', {})
        self.stopped_at = self.runtime_state.get('stopped_at', {})
        self.stop_out_cooldown = max(0.0, float(stop_out_cooldown or 0))
        # Symbols for which a local opening order actually filled.  A flat
        # account is only a stop-out if we had built a position first; an
        # entry that never filled must keep accumulating normally.
        self.entered = self.runtime_state.get('entered', {})
        self.small_order_carry = self.runtime_state.get('small_order_carry', {})
        # Late-entry limit orders that are still live on the exchange, keyed by
        # ``leader:symbol``.  Without this a fill can arrive after the leader
        # has already closed and leave an orphan position behind.
        self.pending_orders = self.runtime_state.get('pending_orders', {})
        # Owner keys whose native stop was moved to the entry price from the
        # console.  Persisted so a restart keeps the tighter stop instead of
        # silently restoring the configured loss-based one.
        self.breakeven = self.runtime_state.get('breakeven') or {}
        # Auto break-even defaults (top-level config); a leader may override
        # the same keys.  ``auto_breakeven_skip`` honours an explicit manual
        # cancel so the next five-second reconcile does not re-arm it.
        self.auto_breakeven = auto_breakeven if isinstance(auto_breakeven, dict) else {}
        self.auto_breakeven_skip = self.runtime_state.get('auto_breakeven_skip') or {}
        self._auto_be_retry_at = {}
        self._auto_be_warned = {}
        # Symbols whose first snapshot failed before an order was placed.  The
        # next event must still treat them as startup so the initial entry
        # price check is not lost with the failed signal.
        self.startup_pending = {}
        # Send the hard-stop flatten failure alert once per lock episode, not
        # every five-second retry.
        self._flatten_alerted = False
        # Console consumes these immutable fill events to attribute exchange
        # realized PnL back to the leader that generated the order.
        self.fill_events = []
        # Optional proactive limit-order mirror (core.mirror.OrderMirror).
        # Snapshots drive the market path; the mirror handles resting orders
        # and coordinates with this engine through ``consume_credit``.
        self.mirror = None
        if state_store:
            self._migrate_owners()
            self._drop_removed_leaders()

    def _migrate_owners(self):
        """Re-key ownership/protection after a leader's account label changes.

        Owner keys embed ``execution_account``.  Correcting that value in the
        config (for example a stale account label on a leader that actually
        trades on the configured execution account) would otherwise leave
        the old key in place and create a second claim for the same symbol.
        """
        changed = False
        for key in list(self.owners):
            leader = self.owners[key]
            if leader not in self.leaders:
                continue
            symbol = key.split(':', 1)[1]
            expected = self._owner_key(leader, symbol)
            if expected == key:
                continue
            self.owners.pop(key, None)
            changed = True
            if expected in self.owners:
                log.warning('Dropped stale ownership %s for %s; %s is owned by %s',
                            key, symbol, expected, self.owners[expected])
                continue
            self.owners[expected] = leader
            protection = self.runtime_state.get('protection') or {}
            if key in protection:
                protection[expected] = protection.pop(key)
            if key in self.breakeven:
                self.breakeven[expected] = self.breakeven.pop(key)
            if key in self.auto_breakeven_skip:
                self.auto_breakeven_skip[expected] = self.auto_breakeven_skip.pop(key)
        if changed:
            log.warning('Re-keyed symbol ownership to match configured execution accounts')
            self._save_state()

    def _drop_removed_leaders(self):
        """Forget runtime state owned by leaders removed from the config.

        ``symbol_owners`` is persisted, so deleting a leader from the YAML
        would otherwise leave owner keys whose leader lookup raises KeyError
        in the five-second protection reconciliation.  A leader that is no
        longer configured can never emit another signal, so its owner and
        protection records and its entry/stop bookkeeping are dead state.
        """
        known = set(self.leaders)
        stale_owners = {
            key for key, leader in self.owners.items() if leader not in known
        }
        removed = {self.owners[key] for key in stale_owners}
        stores = (
            ('stopped', self.stopped),
            ('entered', self.entered),
            ('small_order_carry', self.small_order_carry),
        )
        for _, store in stores:
            for key in store:
                leader = key.split(':', 1)[0]
                if leader not in known:
                    removed.add(leader)
        if not stale_owners and not removed:
            return
        for key in stale_owners:
            self.owners.pop(key, None)
            self.runtime_state.get('protection', {}).pop(key, None)
            self.breakeven.pop(key, None)
            self.auto_breakeven_skip.pop(key, None)
        for _, store in stores:
            for key in [k for k in store if k.split(':', 1)[0] in removed]:
                store.pop(key, None)
        for key in [k for k in self.stopped_at if k.split(':', 1)[0] in removed]:
            self.stopped_at.pop(key, None)
        log.warning('Dropped state for leaders removed from config: %s',
                    ', '.join(sorted(removed)))
        self._save_state()

    async def update_leader_config(self, leader, changes):
        """Apply validated console changes without replacing the leader map.

        Returns True when the address changed, so the caller can resubscribe
        the WebSocket and seed the new address from REST.
        """
        async with self._lock:
            if leader not in self.leaders:
                raise KeyError(leader)
            cfg = self.leaders[leader]
            was_enabled = bool(cfg.get('enabled', True))
            old_address = str(cfg.get('address') or '').lower()
            new_address = str(changes.get('address') or '').lower()
            address_changed = 'address' in changes and new_address != old_address
            cfg.update(changes)
            enabled = bool(cfg.get('enabled', True))
            if 'enabled' in changes and enabled != was_enabled:
                # A disabled leader must stop seeing signals.  Dropping the
                # diff baseline makes a later re-enable start from a fresh
                # startup snapshot instead of closing positions the leader
                # changed while it was not being followed.
                self.previous.pop(leader, None)
                self.previous_at.pop(leader, None)
                if not enabled:
                    await self.cancel_pending_orders(leader=leader, reason='leader disabled')
                    if self.mirror is not None:
                        await self.mirror.drop_leader_locked(leader)
                elif self.mirror is not None:
                    self.mirror.request_sync(force_orders=True)
            if not address_changed:
                return False
            # A new address is a new leader episode.  Diffing the first event
            # against the old address's positions would turn every old coin
            # into a false close, so drop the baseline and per-episode state.
            self.previous.pop(leader, None)
            self.previous_at.pop(leader, None)
            prefix = f'{leader}:'
            for store in (self.stopped, self.entered, self.small_order_carry):
                for key in [k for k in store if k.startswith(prefix)]:
                    store.pop(key, None)
            for key in [k for k in self.stopped_at if k.startswith(prefix)]:
                self.stopped_at.pop(key, None)
            # Breakeven is keyed by owner (account:symbol), not by leader.
            for key in [k for k, owner in self.owners.items() if owner == leader]:
                self.breakeven.pop(key, None)
                self.auto_breakeven_skip.pop(key, None)
                self._auto_be_retry_at.pop(key, None)
                self._auto_be_warned.pop(key, None)
            await self.cancel_pending_orders(leader=leader, reason='leader address changed')
            if self.mirror is not None:
                await self.mirror.drop_leader_locked(leader)
                self.mirror.request_sync(force_orders=True)
            self._save_state()
            return True

    def _account_key(self, leader):
        return str(self.leaders[leader].get('execution_account') or '__default__')

    def _owner_key(self, leader, symbol):
        return f'{self._account_key(leader)}:{symbol.upper()}'

    def _save_state(self):
        if self.state_store:
            self.runtime_state['symbol_owners'] = self.owners
            self.runtime_state['stopped'] = self.stopped
            self.runtime_state['stopped_at'] = self.stopped_at
            self.runtime_state['entered'] = self.entered
            self.runtime_state['small_order_carry'] = self.small_order_carry
            self.runtime_state['pending_orders'] = self.pending_orders
            self.runtime_state['breakeven'] = self.breakeven
            self.runtime_state['auto_breakeven_skip'] = self.auto_breakeven_skip
            self.runtime_state['risk'] = self.risk.state()
            if self.mirror is not None:
                self.runtime_state['mirror_orders'] = self.mirror.snapshot()
            self.state_store.save(self.runtime_state)

    async def _notify(self, method, *args, **kwargs):
        """Best-effort notification that must never abort order processing."""
        handler = getattr(self.notifier, method, None) if self.notifier else None
        if handler is None:
            return False
        try:
            await handler(*args, **kwargs)
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning('Notification %s failed', method, exc_info=True)
            return False

    def _mark_stopped(self, leader, symbol, side=None):
        key = f'{leader}:{symbol}'
        # Remember the leader's side and when the stop happened.  A high
        # frequency leader may cross zero between two snapshots, so "leader is
        # flat" is not always observable; the timestamp bounds the suppression
        # so the symbol can never stay blocked for as long as the leader holds.
        side = side if side in ('long', 'short') else True
        self.stopped[key] = side
        self.stopped_at[key] = time.time()
        self._save_state()

    def _clear_stopped(self, key):
        self.stopped_at.pop(key, None)
        return self.stopped.pop(key, None) is not None

    @staticmethod
    def _pending_key(leader, symbol):
        return f'{leader}:{symbol}'

    @staticmethod
    def _order_gone(exc):
        message = str(exc).upper()
        return any(token in message for token in (
            'ORDER_NOT_FOUND', 'ORDER NOT FOUND', 'NOT_FOUND', 'NOT FOUND',
            'UNKNOWN_ORDER', 'UNKNOWN ORDER', 'ALREADY FILLED', 'ALREADY CLOSED',
            'NOT OPEN', 'DOES NOT EXIST', 'NO SUCH ORDER',
            'INVALID ALGO', 'BEEN COMPLETED',
        ))

    def _track_pending(self, leader, symbol, order):
        self.pending_orders[self._pending_key(leader, symbol)] = {
            'id': str(order.id), 'leader': leader, 'symbol': symbol,
            'side': order.side, 'amount': float(order.amount or 0),
            'price': order.price, 'filled': 0.0,
            'created_at': time.time(),
        }
        self._save_state()

    async def _cancel_pending(self, leader, symbol, reason, notify=True):
        """Cancel one tracked limit entry; return True once it is off the book."""
        key = self._pending_key(leader, symbol)
        item = self.pending_orders.get(key)
        if not item:
            return False
        order_id = item.get('id')
        cancelled = False
        try:
            await self.exchange.cancel_order(order_id, symbol)
        except Exception as exc:
            if not self._order_gone(exc):
                # The order is still live.  Remember why it has to go so the
                # five-second reconciliation retries the cancellation instead
                # of leaving an entry that can fill after the leader left.
                item['cancel_reason'] = reason
                self._save_state()
                log.warning('Failed to cancel pending order %s for %s (%s)', order_id, key, reason, exc_info=True)
                return False
            # The order is no longer on the book, but it may have filled
            # instead of being cancelled.  Ask the venue once before dropping
            # the record so a fill that raced with our cancel is still
            # reported instead of disappearing silently.
            log.info('Pending order %s for %s is already gone (%s)', order_id, key, reason)
            if notify:
                state = await self._pending_order_state(item)
                if state is not None:
                    filled, remaining, full = self._state_fill(state, item)
                    if filled > float(item.get('filled') or 0) + 1e-12:
                        await self._notify_pending_fill(item, filled, remaining, state)
                        item['filled'] = filled
                    if full:
                        self.entered[f'{leader}:{symbol}'] = True
                    self._save_state()
        else:
            cancelled = True
            log.info('Cancelled pending order %s for %s (%s)', order_id, key, reason)
        self.pending_orders.pop(key, None)
        self._save_state()
        if notify and cancelled:
            await self._notify('notify_cancelled', leader, symbol, item, reason)
        return True

    async def cancel_pending_orders(self, leader=None, reason='cancelled', notify=True):
        for item in list(self.pending_orders.values()):
            if leader is not None and item.get('leader') != leader:
                continue
            await self._cancel_pending(
                item.get('leader'), item.get('symbol'), reason, notify=notify,
            )

    @staticmethod
    def _assume_pending_open(item):
        """Fallback when an adapter cannot report individual order status."""
        filled = max(0.0, float(item.get('filled') or 0))
        amount = abs(float(item.get('amount') or 0))
        return {
            'status': 'open', 'filled': filled,
            'remaining': max(0.0, amount - filled),
            'average': None, 'price': item.get('price'),
        }

    async def _pending_order_state(self, item):
        """Return the venue's latest view of one resting order.

        ``None`` means the query failed this cycle and the order must stay
        tracked.  A ``gone`` status means the venue no longer knows it, which
        happens for cancelled unfilled orders.
        """
        fetcher = getattr(self.exchange, 'fetch_order', None)
        order_id = item.get('id')
        symbol = item.get('symbol')
        if fetcher is None or not order_id or not symbol:
            return self._assume_pending_open(item)
        try:
            order = await fetcher(order_id, symbol)
        except NotImplementedError:
            return self._assume_pending_open(item)
        except Exception:
            log.warning('Pending order %s status query failed for %s',
                        order_id, symbol, exc_info=True)
            return None
        if order is not None:
            return order
        return {'status': 'gone', 'filled': 0.0, 'remaining': 0.0,
                'average': None, 'price': None}

    @staticmethod
    def _state_fill(state, item):
        """Normalize an order snapshot into (filled, remaining, full)."""
        status = str(state.get('status') or '').lower()
        filled = max(0.0, float(state.get('filled') or 0))
        remaining = max(0.0, float(state.get('remaining') or 0))
        full = status in ('closed', 'filled') or (filled > 0 and remaining <= 1e-12)
        if full:
            filled = filled or abs(float(item.get('amount') or 0))
            remaining = 0.0
        return filled, remaining, full

    async def _notify_pending_fill(self, item, filled, remaining, state):
        price = state.get('average') or item.get('price')
        await self._notify(
            'notify_limit_filled',
            item.get('leader'), item.get('symbol'), item, filled, price, remaining,
        )

    def _append_fill_event(self, leader, symbol, item, quantity, price):
        """Record a resting-order fill for console attribution.

        Resting entries are discovered by polling rather than returned from
        ``create_order``, so the order path's fill event never fires for them.
        Partial fills are appended as they appear; the console's ``local_fills``
        table is keyed by order id, so replaying is harmless.
        """
        try:
            quantity = float(quantity)
        except (TypeError, ValueError):
            return
        if quantity <= 0:
            return
        self.fill_events.append({
            'account': self._account_key(leader), 'leader': leader,
            'order_id': str(item.get('id') or ''), 'symbol': symbol,
            'side': str(item.get('side') or '').lower(), 'quantity': quantity,
            'price': float(price or 0), 'reduce_only': bool(item.get('reduce_only')),
            'timestamp': time.time(),
        })

    async def reconcile_pending_orders(self):
        """Keep resting entries aligned with the leader and report fills.

        There is no time-based expiry.  A late-entry limit stays on the book
        until it fills or its premise disappears: the leader goes flat or
        reverses, the leader is disabled/removed, or a later signal replaces
        it.  Failed cancellations are retried on every cycle.
        """
        async with self._lock:
            for key, item in list(self.pending_orders.items()):
                leader = item.get('leader')
                symbol = item.get('symbol')
                if not leader or not symbol:
                    self.pending_orders.pop(key, None)
                    self._save_state()
                    continue

                state = await self._pending_order_state(item)
                if state is None:
                    # The venue did not answer; keep tracking and retry.
                    continue
                status = str(state.get('status') or '').lower()
                filled, remaining, full = self._state_fill(state, item)
                previous_filled = float(item.get('filled') or 0)
                fill_price = state.get('average') or item.get('price')
                if full:
                    await self._notify_pending_fill(item, filled, 0.0, state)
                    self._append_fill_event(
                        leader, symbol, item, filled - previous_filled, fill_price)
                    self.entered[f'{leader}:{symbol}'] = True
                    self.pending_orders.pop(key, None)
                    self._save_state()
                    continue
                if filled > previous_filled + 1e-12:
                    # Partial fill: report the increase but keep watching.
                    await self._notify_pending_fill(item, filled, remaining, state)
                    self._append_fill_event(
                        leader, symbol, item, filled - previous_filled, fill_price)
                    item['filled'] = filled
                    self._save_state()
                if status in ('canceled', 'cancelled', 'expired', 'rejected'):
                    self.pending_orders.pop(key, None)
                    self._save_state()
                    await self._notify(
                        'notify_cancelled', leader, symbol, item,
                        f'exchange reported {status}',
                    )
                    continue
                if status == 'gone':
                    self.pending_orders.pop(key, None)
                    self._save_state()
                    log.info('Pending order %s for %s disappeared from the exchange',
                             item.get('id'), key)
                    continue
                # The order is still open (possibly partially filled).
                cfg = self.leaders.get(leader)
                if cfg is None or not cfg.get('enabled', True):
                    await self._cancel_pending(leader, symbol, 'leader is disabled')
                    continue
                if item.get('cancel_reason'):
                    await self._cancel_pending(leader, symbol, item['cancel_reason'])
                    continue
                positions = self.previous.get(leader)
                if positions is None:
                    continue
                position = positions.get(symbol)
                expected_long = str(item.get('side') or '').lower() == 'buy'
                if position is None or abs(position.size) <= 1e-12:
                    await self._cancel_pending(leader, symbol, 'leader is flat')
                elif (position.size > 0) != expected_long:
                    await self._cancel_pending(leader, symbol, 'leader reversed direction')

    async def check_hard_stop(self):
        """Check daily account drawdown and flatten exposure once per lock."""
        if not self.risk.config:
            return False
        equity = await self.exchange.equity()
        locked_before = self.risk.locked
        hit = self.risk.evaluate(equity)
        if self.state_store and (self._risk_state_saved != self.risk.state()):
            self._save_state()
            self._risk_state_saved = self.risk.state()
        if not hit:
            self._flatten_alerted = False
            return False
        self.paused = True
        first_trigger = not locked_before
        flatten_error = None
        flatten_recovered = False
        try:
            # Serialize flattening with signal processing so an in-flight open
            # order cannot land between cancellation and the close.
            async with self._lock:
                if self.risk.flattened:
                    checker = getattr(self.exchange, 'has_open_positions', None)
                    if not checker or not await checker():
                        return True
                    # A late fill or manual trade slipped through after
                    # flattening; flatten again rather than leaving exposure.
                    self.risk.flattened = False
                    self._save_state()
                log.critical('HARD STOP triggered equity=%g baseline=%g', equity, self.risk.baseline)
                try:
                    # Pending entry orders must be gone before flattening,
                    # otherwise a fill can reopen exposure while locked.
                    await self.cancel_pending_orders(reason='hard stop', notify=False)
                    cancel_all = getattr(self.exchange, 'cancel_all_orders', None)
                    if cancel_all:
                        await cancel_all()
                except Exception:
                    log.exception('HARD STOP order cancellation incomplete; flattening anyway')
                try:
                    await self.exchange.close_all_positions()
                except Exception as exc:
                    # Keep ``flattened`` false so the next cycle retries.
                    self.risk.flattened = False
                    self._save_state()
                    log.exception('HARD STOP flatten incomplete; retrying next cycle')
                    if not self._flatten_alerted:
                        self._flatten_alerted = True
                        flatten_error = exc
                else:
                    self.risk.flattened = True
                    self._save_state()
                    log.critical('HARD STOP flatten complete equity=%g', equity)
                    if self._flatten_alerted:
                        self._flatten_alerted = False
                        flatten_recovered = True
        finally:
            if first_trigger:
                await self._notify(
                    'notify_hard_stop',
                    equity, self.risk.baseline,
                    float(self.risk.baseline) - float(equity),
                )
            if flatten_error is not None:
                handler = getattr(self.notifier, 'notify_hard_stop_error', None) if self.notifier else None
                if handler:
                    try:
                        await handler(equity, flatten_error)
                    except Exception:
                        log.warning('HARD STOP failure notification failed', exc_info=True)
            elif flatten_recovered:
                handler = getattr(self.notifier, 'notify_hard_stop_recovered', None) if self.notifier else None
                if handler:
                    try:
                        await handler(equity)
                    except Exception:
                        log.warning('HARD STOP recovery notification failed', exc_info=True)
        return True

    async def unlock_hard_stop(self):
        """Manually clear the hard-stop lock and rebaseline to current equity."""
        equity = await self.exchange.equity()
        was_locked = self.risk.locked
        self.risk.unlock(equity)
        self.paused = False
        self._flatten_alerted = False
        if self.state_store:
            self._save_state()
            self._risk_state_saved = self.risk.state()
        log.warning('HARD STOP manually unlocked equity=%g baseline=%g', equity, self.risk.baseline)
        return {'was_locked': was_locked, 'equity': equity, 'baseline': self.risk.baseline}

    def _set_carry(self, leader, symbol, side=None, amount=0):
        key = f'{leader}:{symbol}'
        if side and amount > 1e-12:
            self.small_order_carry[key] = {'side': side, 'amount': amount}
        else:
            self.small_order_carry.pop(key, None)
        self._save_state()

    async def _claim_or_check(self, leader, symbol):
        key = self._owner_key(leader, symbol)
        owner = self.owners.get(key)
        if owner and owner != leader:
            return False, owner
        if not owner:
            self.owners[key] = leader
            self._save_state()
        return True, leader

    async def _release_if_flat(self, leader, symbol, live=None):
        key = self._owner_key(leader, symbol)
        if self.owners.get(key) != leader:
            return
        if live is None:
            live = await self.exchange.position_size(symbol)
        if abs(live) <= 1e-12:
            await self._cancel_pending(leader, symbol, 'account position is flat')
            old = self.runtime_state.get('protection', {}).pop(key, None)
            if old:
                try:
                    await self.exchange.cancel_protection(self._protection_id(old), symbol)
                except Exception as exc:
                    # The venue may remove a conditional order automatically
                    # when the position reaches zero or its trigger fires, then
                    # answer an order-not-found error to a delete.  Treat both
                    # flavors of "order is gone" as an already-completed
                    # cancellation instead of aborting the release with the
                    # owner intact.
                    message = str(exc).upper()
                    if type(exc).__name__ != 'OrderNotFound' and 'ORDER_NOT_FOUND' not in message:
                        log.warning('Failed to cancel protection order %s for %s', old, symbol, exc_info=True)
                if hasattr(self.exchange, 'cancel_protection_orders'):
                    try:
                        await self.exchange.cancel_protection_orders(symbol)
                    except Exception:
                        log.warning('Failed to clean residual protection orders for %s', symbol, exc_info=True)
            self.owners.pop(key, None)
            self.entered.pop(f'{leader}:{symbol}', None)
            self.breakeven.pop(key, None)
            self.auto_breakeven_skip.pop(key, None)
            self._auto_be_retry_at.pop(key, None)
            self._auto_be_warned.pop(key, None)
            self._save_state()

    def _leader_holds_same_side(self, leader, symbol, live):
        """True when the leader still holds ``symbol`` in the same direction."""
        position = self.previous.get(leader, {}).get(symbol)
        return (position is not None and abs(position.size) > 1e-12
                and position.size * live > 0)

    @staticmethod
    def _protection_id(value):
        # State files written by older versions contain the id directly.
        return value.get('id') if isinstance(value, dict) else value

    async def set_breakeven(self, leader, symbol):
        """Move this position's native stop to its entry price.

        Returns the trigger price.  A position that is not in profit is
        rejected: the stop would sit on the wrong side of the mark and fire
        immediately, turning "breakeven" into a market close at a loss.
        """
        key = self._owner_key(leader, symbol)
        result = await self._set_breakeven_detail(leader, symbol, ignore_skip=True)
        if result is None:
            raise ValueError(f'could not place a breakeven stop for {symbol}')
        # An explicit manual action overrides an earlier manual cancel.
        self.auto_breakeven_skip.pop(key, None)
        self._auto_be_retry_at.pop(key, None)
        self._auto_be_warned.pop(key, None)
        self._save_state()
        return result['trigger']

    async def clear_breakeven(self, leader, symbol):
        """Revert a breakeven stop to the configured stop loss, if any."""
        key = self._owner_key(leader, symbol)
        if self.owners.get(key) != leader:
            raise KeyError(f'{symbol} is not owned by {leader}')
        cfg = self.leaders[leader].get('position_stop_loss') or {}
        async with self._protection_lock:
            if not self.breakeven.pop(key, None):
                return False
            # Honour the cancel during later reconciles instead of re-arming
            # the stop from the next profit check.
            self.auto_breakeven_skip[key] = True
            self._auto_be_retry_at.pop(key, None)
            self._save_state()
            if cfg.get('enabled') and cfg.get('max_loss_usdt') is not None:
                await self._sync_protection_locked(leader, symbol, cfg, None)
                return True
            old = self.runtime_state.get('protection', {}).pop(key, None)
            if old:
                try:
                    await self.exchange.cancel_protection(self._protection_id(old), symbol)
                except Exception:
                    log.info('Breakeven protection %s for %s is already gone', self._protection_id(old), symbol)
                self._save_state()
        return True

    async def _set_breakeven_detail(self, leader, symbol, ignore_skip=False):
        """Arm a break-even stop, re-reading the position under the lock.

        Returns ``{'trigger', 'profit'}`` on success and ``None`` when no
        protection order could be placed.  A stale reconciliation snapshot is
        never used for the actual order: entry, mark and size are re-fetched.
        """
        key = self._owner_key(leader, symbol)
        if self.owners.get(key) != leader:
            raise KeyError(f'{symbol} is not owned by {leader}')
        cfg = self.leaders[leader].get('position_stop_loss') or {}
        async with self._protection_lock:
            if not ignore_skip and self.auto_breakeven_skip.get(key):
                return None
            detail = await self.exchange.fetch_position_detail(symbol)
            if not detail:
                raise ValueError(f'no open position for {symbol}')
            trigger = self._breakeven_trigger(detail)
            profit = self._position_profit(detail)
            self.breakeven[key] = True
            self._save_state()
            try:
                status = await self._sync_protection_locked(leader, symbol, cfg, detail)
            except Exception:
                self.breakeven.pop(key, None)
                self._save_state()
                raise
            if status == 'none':
                # Never leave a persisted flag that claims a stop exists.
                self.breakeven.pop(key, None)
                self._save_state()
                return None
            return {'trigger': trigger, 'profit': profit if profit is not None else 0.0}

    def _auto_breakeven_config(self, leader):
        base = self.auto_breakeven if isinstance(self.auto_breakeven, dict) else {}
        override = (self.leaders.get(leader) or {}).get('auto_breakeven') or {}
        if not isinstance(override, dict):
            override = {}
        return {**base, **override}

    @staticmethod
    def _position_profit(detail):
        """Mark-based unrealized profit, matching the breakeven trigger basis."""
        if not detail:
            return None
        try:
            entry = float(detail.get('entryPrice') or 0)
            mark = float(detail.get('markPrice') or detail.get('markPx') or 0)
            contracts = abs(float(detail.get('contracts') or 0))
            contract_size = abs(float(detail.get('contractSize') or 1))
        except (TypeError, ValueError):
            return None
        side = str(detail.get('side') or '').lower()
        if side not in ('long', 'short'):
            return None
        if entry <= 0 or mark <= 0 or contracts <= 0:
            return None
        amount = contracts * contract_size
        return (mark - entry) * amount if side == 'long' else (entry - mark) * amount

    async def _maybe_auto_breakeven(self, leader, symbol, detail):
        """Arm breakeven once this position's profit reaches the configured amount."""
        key = self._owner_key(leader, symbol)
        if self.breakeven.get(key) or self.auto_breakeven_skip.get(key):
            return None
        cfg = self._auto_breakeven_config(leader)
        if not cfg.get('enabled'):
            return None
        try:
            trigger_usdt = float(cfg.get('trigger_usdt') or 0)
        except (TypeError, ValueError):
            return None
        if trigger_usdt <= 0:
            return None
        now = time.time()
        if self._auto_be_retry_at.get(key, 0) > now:
            return None
        profit = self._position_profit(detail)
        if profit is None or profit < trigger_usdt:
            return None
        try:
            result = await self._set_breakeven_detail(leader, symbol)
        except Exception:
            self._auto_be_retry_at[key] = now + 60
            log.warning('AUTO BREAKEVEN failed for %s %s', leader, symbol, exc_info=True)
            return None
        if result is None:
            self._auto_be_retry_at[key] = now + 60
            log.warning('AUTO BREAKEVEN could not place a stop for %s %s', leader, symbol)
            return None
        log.warning('AUTO BREAKEVEN armed leader=%s symbol=%s profit=%.2f trigger=%g',
                    leader, symbol, result['profit'], result['trigger'])
        return result

    @staticmethod
    def _breakeven_trigger(detail):
        entry = float(detail.get('entryPrice') or 0)
        mark = float(detail.get('markPrice') or detail.get('markPx') or 0)
        side = str(detail.get('side') or '').lower()
        if entry <= 0:
            raise ValueError('entry price unavailable')
        if mark > 0 and ((side == 'long' and entry >= mark) or (side == 'short' and entry <= mark)):
            raise ValueError('position is not in profit; a breakeven stop would trigger immediately')
        return entry

    async def _sync_protection(self, leader, symbol, detail=None):
        cfg = self.leaders[leader].get('position_stop_loss') or {}
        key = self._owner_key(leader, symbol)
        if not self.breakeven.get(key) and (not cfg.get('enabled') or cfg.get('max_loss_usdt') is None):
            return
        async with self._protection_lock:
            return await self._sync_protection_locked(leader, symbol, cfg, detail)

    async def _sync_protection_locked(self, leader, symbol, cfg, detail):
        if detail is None:
            detail = await self.exchange.fetch_position_detail(symbol)
        if not detail:
            return 'none'
        contracts = abs(float(detail.get('contracts') or 0))
        contract_size = abs(float(detail.get('contractSize') or 1))
        position_amount = contracts * contract_size
        entry = float(detail.get('entryPrice') or 0)
        if not position_amount or entry <= 0:
            return 'none'
        side = str(detail.get('side') or '').lower()
        key = self._owner_key(leader, symbol)
        if self.breakeven.get(key):
            trigger = entry
        else:
            loss = float(cfg['max_loss_usdt'])
            # Venues differ in how they report position size; convert to
            # base-asset quantity before translating a dollar loss into a
            # price distance, otherwise non-unit contract sizes trigger early.
            trigger = entry - loss / position_amount if side == 'long' else entry + loss / position_amount
        mark = float(detail.get('markPrice') or detail.get('markPx') or 0)
        if trigger <= 0 or (mark > 0 and ((side == 'long' and trigger >= mark) or (side == 'short' and trigger <= mark))):
            old = self.runtime_state.get('protection', {}).get(key)
            old_id = self._protection_id(old)
            old_contracts = float(old.get('contracts') or 0) if isinstance(old, dict) else 0
            old_trigger = float(old.get('trigger') or 0) if isinstance(old, dict) else 0
            old_valid = (old_id and old_trigger > 0 and mark > 0 and (
                (side == 'long' and old_trigger < mark)
                or (side == 'short' and old_trigger > mark)))
            # After a leader add, a breakeven trigger (the new average entry)
            # can fall on the wrong side of the mark.  The previous stop is
            # still protective; cancelling it would leave the position naked.
            if self.breakeven.get(key) and old_valid:
                if old_contracts and abs(old_contracts - contracts) > 1e-12:
                    # Protection orders carry a size, so a larger position
                    # needs a replacement at the old trigger for full cover.
                    if hasattr(self.exchange, 'cancel_protection_orders'):
                        await self.exchange.cancel_protection_orders(symbol, old_id)
                    if hasattr(self.exchange, 'cancel_protection'):
                        try:
                            await self.exchange.cancel_protection(old_id, symbol)
                        except Exception:
                            log.info('Protection %s for %s is already gone; replacing it', old_id, symbol)
                    oid = await self.exchange.set_protection(
                        symbol, 'sell' if side == 'long' else 'buy', contracts, old_trigger)
                    self.runtime_state.setdefault('protection', {})[key] = {
                        'id': oid, 'contracts': contracts, 'trigger': old_trigger,
                    }
                    self._save_state()
                    return 'placed'
                if not self._auto_be_warned.get(key):
                    self._auto_be_warned[key] = True
                    log.warning(
                        'Keeping existing protection for %s: new trigger %g is on the wrong side of mark %g',
                        symbol, trigger, mark)
                return 'kept'
            log.warning('Protection price invalid for %s: side=%s entry=%g mark=%g trigger=%g; no order created', symbol, side, entry, mark, trigger)
            # The tracked stop no longer exists or was never accepted; drop the
            # record so a later reconciliation can place a valid one instead
            # of trusting a dead id.
            self.runtime_state.get('protection', {}).pop(key, None)
            self._save_state()
            if hasattr(self.exchange, 'cancel_protection_orders'):
                await self.exchange.cancel_protection_orders(symbol)
            return 'none'
        # Native stop orders are intentionally owned by the exchange; this demo
        # records the latest id for later replacement/cancellation.
        old = self.runtime_state.get('protection', {}).get(key)
        old_id = self._protection_id(old)
        old_contracts = float(old.get('contracts') or 0) if isinstance(old, dict) else 0
        old_trigger = float(old.get('trigger') or 0) if isinstance(old, dict) else 0
        # Do not churn orders on every reconciliation tick.  Recreate only
        # when the live position or its trigger has changed.
        if old_id and old_contracts and old_trigger and abs(old_contracts - contracts) <= 1e-12 and abs(old_trigger - trigger) <= max(abs(trigger), 1) * 1e-10:
            return 'kept'
        # Cleaning duplicates costs a full conditional-order listing, so only
        # do it when we are about to replace the protection order.
        if hasattr(self.exchange, 'cancel_protection_orders'):
            await self.exchange.cancel_protection_orders(symbol, old_id)
        if old_id and hasattr(self.exchange, 'cancel_protection'):
            try:
                await self.exchange.cancel_protection(old_id, symbol)
            except Exception:
                # The stop may already have fired or been canceled manually.
                # Treat an unknown old id as already removed and continue.
                log.info('Protection %s for %s is already gone; replacing it', old_id, symbol)
        oid = await self.exchange.set_protection(symbol, 'sell' if side == 'long' else 'buy', contracts, trigger)
        self.runtime_state.setdefault('protection', {})[key] = {
            'id': oid, 'contracts': contracts, 'trigger': trigger,
        }
        self._save_state()
        self._auto_be_warned.pop(key, None)
        return 'placed'

    @staticmethod
    def _detail_size(detail):
        if not detail:
            return 0.0
        contracts = abs(float(detail.get('contracts') or 0))
        size = contracts * abs(float(detail.get('contractSize') or 1))
        return -size if str(detail.get('side') or '').lower() == 'short' else size

    async def _position_probe(self, symbol):
        """Return (signed size, detail) with one REST call for open positions."""
        fetcher = getattr(self.exchange, 'fetch_position_detail', None)
        detail = await fetcher(symbol) if fetcher else None
        if detail is not None:
            return self._detail_size(detail), detail
        return await self.exchange.position_size(symbol), None

    async def reconcile_protections(self):
        """Reconcile exchange positions with locally tracked protection orders.

        This catches manual closes and partial closes, which do not produce a
        leader signal and therefore never pass through ``process``.
        """
        notifications = []
        async with self._lock:
            items = []
            seen = set()
            for key, leader in list(self.owners.items()):
                symbol = key.split(':', 1)[1]
                if self.symbol_map.is_skipped(symbol):
                    continue
                items.append((key, leader, symbol))
                seen.add(key)
            # A stop-out suppression only makes sense while the account is
            # flat.  Probe suppressed symbols even when the owner record was
            # already released: the position may have been reopened manually,
            # and later leader adds must then be followed instead of skipped.
            for stopped_key in list(self.stopped):
                leader, _, symbol = stopped_key.partition(':')
                if not leader or not symbol or leader not in self.leaders:
                    continue
                if self.symbol_map.is_skipped(symbol):
                    continue
                owner_key = self._owner_key(leader, symbol)
                if owner_key in seen:
                    continue
                items.append((owner_key, leader, symbol))
                seen.add(owner_key)
            # Probe concurrently so reconciliation time does not grow with the
            # number of owned positions while the signal lock is held.
            probes = await asyncio.gather(
                *(self._position_probe(symbol) for _, _, symbol in items),
                return_exceptions=True,
            )
            for (key, leader, symbol), probe in zip(items, probes):
                try:
                    if isinstance(probe, Exception):
                        message = str(probe)
                        if isinstance(probe, ValueError) and (
                                message.startswith('No USDT perpetual market')
                                or 'No Hyperliquid market' in message):
                            self.runtime_state.get('protection', {}).pop(key, None)
                            self.owners.pop(key, None)
                            self._save_state()
                            log.warning('Dropped protection state for unsupported market %s', symbol)
                        else:
                            log.warning('Protection reconciliation failed for %s: %s', key, probe)
                        continue
                    live, detail = probe
                    holder = self.owners.get(key)
                    if holder not in (None, leader):
                        # Another configured leader already owns this account
                        # symbol; its own reconciliation entry handles it.
                        continue
                    if abs(live) <= 1e-12:
                        if self.pending_orders.get(self._pending_key(leader, symbol)):
                            # A late-entry limit is still working; a flat
                            # account is expected and the owner must stay.
                            continue
                        baseline = self.previous.get(leader, {}).get(symbol)
                        if (baseline is not None and abs(baseline.size) > 1e-12
                                and self.entered.get(f'{leader}:{symbol}')):
                            # The account is flat but the leader still holds:
                            # our side was stopped or closed manually.
                            self._mark_stopped(leader, symbol,
                                               'long' if baseline.size > 0 else 'short')
                        await self._release_if_flat(leader, symbol, live=live)
                    else:
                        if holder is None:
                            claimed, owner = await self._claim_or_check(leader, symbol)
                            if not claimed:
                                continue
                        if self._leader_holds_same_side(leader, symbol, live):
                            if self._clear_stopped(f'{leader}:{symbol}'):
                                self._save_state()
                                log.info('STOP-OUT CLEARED leader=%s symbol=%s: account holds a position again',
                                         leader, symbol)
                        if not self.entered.get(f'{leader}:{symbol}'):
                            self.entered[f'{leader}:{symbol}'] = True
                            self._save_state()
                        armed = await self._maybe_auto_breakeven(leader, symbol, detail)
                        if armed:
                            notifications.append(
                                (leader, symbol, armed['profit'], armed['trigger']))
                        else:
                            await self._sync_protection(leader, symbol, detail=detail)
                except Exception:
                    log.warning('Protection reconciliation failed for %s', key, exc_info=True)
        # Notifications do network I/O; send them after releasing the signal lock.
        handler = getattr(getattr(self, 'notifier', None), 'notify_breakeven', None)
        if handler:
            for leader, symbol, profit, trigger in notifications:
                try:
                    await handler(leader, symbol, profit, trigger)
                except Exception:
                    log.warning('AUTO BREAKEVEN notification failed for %s %s',
                                leader, symbol, exc_info=True)

    async def prepare_startup(self):
        # Failure must propagate; unknown account state is never zero exposure.
        await self.exchange.fetch_positions()

    async def _pending_matches(self, item, signal):
        """Return True when a resting entry still fits the startup snapshot.

        A pending order survives a restart only when it points the same way as
        the leader and is not larger than the remaining target gap.  Keeping an
        oversized order would over-expose the account after a leader reduction
        that happened while the process was down.
        """
        expected_long = str(item.get('side') or '').lower() == 'buy'
        if (float(signal.target_size or 0) > 0) != expected_long:
            return False
        # A stale record (cancelled or filled while the process was down) must
        # not keep the startup entry from being evaluated.  If the venue
        # cannot answer, keep the order rather than cancelling it blindly.
        state = await self._pending_order_state(item)
        if state is None:
            return True
        status = str(state.get('status') or '').lower()
        if status not in ('open', ''):
            return False
        live = await self.exchange.position_size(signal.symbol)
        if live and (live > 0) != expected_long:
            return False
        gap = max(0.0, abs(float(signal.target_size or 0)) - abs(live))
        remaining = max(
            0.0,
            abs(float(item.get('amount') or 0)) - abs(float(item.get('filled') or 0)),
        )
        return remaining <= gap + 1e-12

    async def _skip(self, leader, signal, reason):
        log.info('ORDER SKIPPED leader=%s symbol=%s reason=%s', leader, signal.symbol, reason)
        await self._notify('notify_skipped', leader, signal, reason)

    async def _unfavorable_entry(self, signal, target, positions):
        """Return a description when the market is worse than the leader entry."""
        current = await self.exchange.entry_quote(signal.symbol, 'buy' if target > 0 else 'sell')
        position = positions.get(signal.symbol)
        entry = position.entry_price if position else 0
        if entry <= 0 or current <= 0:
            raise ValueError('entry price unavailable')
        if (target > 0 and current > entry) or (target < 0 and current < entry):
            return f'market={current:g}, leader entry={entry:g}, target={target:g}'
        return None

    async def sizing(self, leader):
        """Return ``(ratio, budget, equity, denominator)`` for a leader.

        Shared with ``core.mirror`` so a mirrored order and a snapshot-driven
        order scale the leader's size identically.
        """
        cfg = self.leaders[leader]
        denominator = float(cfg['total_margin'])
        if denominator <= 0:
            raise ValueError('total_margin must be positive')
        budget = self.exchange.config.get('fixed_balance_usdt')
        equity = None
        if budget is None:
            equity = await self.exchange.equity()
            budget = equity
        else:
            budget = float(budget)
        ratio = budget / denominator * float(cfg.get('coefficient', 1))
        return ratio, float(budget), equity, denominator

    async def _credit_bookkeeping(self, leader, signal, reduce=False):
        """Keep ownership/entry/protection in sync for a mirrored fill.

        The mirror filled (or the leader did) without the snapshot path placing
        an order, so the normal bookkeeping below the order call never runs.
        """
        symbol = signal.symbol
        try:
            if not reduce and f'{leader}:{symbol}' in self.stopped:
                log.info('MIRROR CREDIT leader=%s symbol=%s ignored: stopped out', leader, symbol)
                return
            if not reduce:
                ok, owner = await self._claim_or_check(leader, symbol)
                if not ok:
                    log.info('MIRROR CREDIT leader=%s symbol=%s ignored: owned by %s',
                             leader, symbol, owner)
                    return
            live = await self.exchange.position_size(symbol)
            if reduce:
                await self._release_if_flat(leader, symbol, live=live)
                return
            if abs(live) > 1e-12:
                self.entered[f'{leader}:{symbol}'] = True
                self._save_state()
                await self._sync_protection(leader, symbol)
        except Exception:
            log.warning('MIRROR CREDIT bookkeeping failed for %s %s',
                        leader, symbol, exc_info=True)

    async def _consume_mirror_credit(self, leader, signal, ratio, startup, startup_symbols):
        """Apply a mirrored-fill credit to a snapshot signal.

        Returns ``None`` when the mirror already covers the whole delta (the
        caller must not place an order) or a possibly-resized signal.
        """
        mirror = self.mirror
        if mirror is None or ratio <= 0:
            return signal
        if startup or signal.symbol in startup_symbols:
            return signal
        want = signal.delta / ratio
        credit = mirror.consume_credit(leader, signal.symbol, want)
        if not credit and mirror.has_live_mirror(leader, signal.symbol, signal.delta):
            # The position snapshot can arrive before the userFills event that
            # explains it.  Wait briefly instead of placing a duplicate order.
            await mirror.wait_for_fills(mirror.grace_ms)
            credit = mirror.consume_credit(leader, signal.symbol, want)
        if not credit:
            return signal
        remaining = want - credit
        if abs(remaining) <= 1e-12:
            if signal.kind in ('open', 'increase', 'close_reverse'):
                # A resting late-entry limit would now be an orphan on top of
                # the mirrored fill; a stale carry would over-size the next add.
                await self._cancel_pending(
                    leader, signal.symbol, 'mirrored fill covers this entry')
                self._set_carry(leader, signal.symbol)
            await self._credit_bookkeeping(
                leader, signal, reduce=signal.kind in ('decrease', 'close'))
            return None
        if signal.kind in ('open', 'increase'):
            return replace(signal, delta=remaining * ratio)
        # Closes/reversals size from the live position; a partial credit leaves
        # the normal path to finish whatever is left.
        return signal

    async def process(self, leader, positions, resync=False):
        protect = []
        async with self._lock:
            cfg = self.leaders[leader]
            if not cfg.get('enabled', True):
                return []
            ratio, budget, equity, denominator = await self.sizing(leader)
            startup = leader not in self.previous
            startup_symbols = self.startup_pending.get(leader, set())
            previous = dict(self.previous.get(leader, {}))
            now = time.time()
            failed = set()
            # A stop-out suppression never lasts forever.  It ends when the
            # leader goes flat, reverses direction (a close and reopen even if
            # the flat state fell between two snapshots), or the cooldown
            # expires while the leader keeps holding.  On expiry a flat account
            # is re-aligned to the leader's current target on this same diff
            # instead of waiting for enough sub-minimum adds to accumulate.
            for key in [k for k in self.stopped if k.startswith(f'{leader}:')]:
                symbol = key[len(leader) + 1:]
                position = positions.get(symbol)
                if position is None or abs(position.size) <= 1e-12:
                    self._clear_stopped(key)
                    self._save_state()
                    continue
                stopped_side = self.stopped.get(key)
                current_side = 'long' if position.size > 0 else 'short'
                if stopped_side in ('long', 'short') and stopped_side != current_side:
                    self._clear_stopped(key)
                    self._save_state()
                    log.info('STOP-OUT CLEARED leader=%s symbol=%s: leader reversed %s->%s',
                             leader, symbol, stopped_side, current_side)
                    continue
                at = float(self.stopped_at.get(key) or 0)
                if at > 0 and now - at < self.stop_out_cooldown:
                    continue
                self._clear_stopped(key)
                self._save_state()
                log.info('STOP-OUT COOLDOWN EXPIRED leader=%s symbol=%s: resuming after %.0fs',
                         leader, symbol, max(0.0, now - at))
                try:
                    live = await self.exchange.position_size(symbol)
                except Exception:
                    live = 0.0
                if abs(live) <= 1e-12 and previous.get(symbol) is not None:
                    # Flat account, leader still holds: turn the next diff into
                    # an open so we re-enter the current target immediately.
                    previous.pop(symbol, None)
            signals = diff_positions(leader, previous, positions, ratio)
            self.previous_at[leader] = now
            if startup:
                for key in list(self.small_order_carry):
                    if key.startswith(f'{leader}:') and key[len(leader) + 1:] not in positions:
                        self.small_order_carry.pop(key)
                        self._save_state()
                log.info('STARTUP leader=%s equity=%s sizing_balance=%g total_margin=%g ratio=%g positions=%d', leader, 'fixed' if equity is None else f'{equity:g}', budget, denominator, ratio, len(positions))
            for s in signals:
                ordered = False
                symbol_startup = startup or s.symbol in startup_symbols
                try:
                    if self.symbol_map.is_skipped(s.symbol):
                        if f'{leader}:{s.symbol}' in self.small_order_carry:
                            self._set_carry(leader, s.symbol)
                        await self._skip(leader, s, 'symbol is mapped to skip')
                        continue
                    if s.symbol in cfg.get('symbol_blacklist', []) or s.symbol + 'USDT' in cfg.get('symbol_blacklist', []):
                        await self._skip(leader, s, 'symbol is blacklisted')
                        continue
                    if s.kind in ('decrease', 'close', 'close_reverse'):
                        # The leader is leaving; an unfilled late-entry limit
                        # would otherwise fill later as an orphan position.
                        await self._cancel_pending(leader, s.symbol, f'{s.kind} signal from leader')
                    covered = await self._consume_mirror_credit(
                        leader, s, ratio, startup, startup_symbols)
                    if covered is None:
                        # A mirrored maker fill already accounts for this delta.
                        continue
                    s = covered
                    if s.kind in ('open', 'increase', 'close_reverse') and (self.paused or self.risk.locked):
                        reason = 'new exposure is paused' if self.paused else 'account is hard-stop locked'
                        await self._skip(leader, s, reason)
                        continue
                    stopped_key = f'{leader}:{s.symbol}'
                    if stopped_key in self.stopped and s.kind in ('open', 'increase'):
                        live_now = await self.exchange.position_size(s.symbol)
                        leader_position = positions.get(s.symbol)
                        if (abs(live_now) > 1e-12 and leader_position is not None
                                and live_now * leader_position.size > 0):
                            # The account holds this symbol again (manual
                            # reopen, or a residual left after the stop).  The
                            # suppression only protects a flat account, so
                            # resume following instead of skipping forever.
                            self._clear_stopped(stopped_key)
                            self._save_state()
                            log.info('STOP-OUT CLEARED leader=%s symbol=%s: account holds a position again',
                                     leader, s.symbol)
                        else:
                            await self._skip(leader, s, 'stopped out; waiting for the leader to close and reopen')
                            continue
                    account_key = self._owner_key(leader, s.symbol)
                    had_owner = self.owners.get(account_key) == leader
                    if s.kind in ('open', 'increase', 'close_reverse'):
                        ok, owner = await self._claim_or_check(leader, s.symbol)
                        if not ok:
                            await self._skip(leader, s, f'symbol is owned by leader {owner}')
                            continue
                    elif s.kind in ('decrease', 'close') and self.owners.get(account_key) not in (None, leader):
                        await self._skip(leader, s, f'symbol is owned by leader {self.owners[account_key]}')
                        continue
                    pending = self.pending_orders.get(self._pending_key(leader, s.symbol))
                    if s.kind == 'open' and pending:
                        if startup and await self._pending_matches(pending, s):
                            # Keep a still-valid resting entry across a restart
                            # instead of replacing it with a worse market order
                            # and then skipping that entry on price.
                            log.info('STARTUP keeping resting limit %s for %s',
                                     pending.get('id'), s.symbol)
                            continue
                        # A stale limit from an earlier episode is still live;
                        # never place a second entry on top of it.
                        if not await self._cancel_pending(leader, s.symbol, 'replaced by a new open signal'):
                            await self._skip(leader, s, 'previous limit entry could not be cancelled')
                            continue
                    live = await self.exchange.position_size(s.symbol)
                    if (s.kind in ('open', 'increase') and abs(live) > 1e-12
                            and not self.entered.get(stopped_key)):
                        # A limit entry may fill after create_order returns
                        # "open"; seeing the exposure here marks it too.
                        self.entered[stopped_key] = True
                        self._save_state()
                    if (had_owner and s.kind in ('open', 'increase')
                            and self.entered.get(stopped_key)
                            and abs(live) <= 1e-12
                            and not self.small_order_carry.get(stopped_key)):
                        # We held this symbol but the account is now flat: a
                        # native stop or manual close removed it while the
                        # leader still holds.  Copying the add would create a
                        # fresh position right after the stop, so suppress
                        # re-entry until the leader goes flat and reopens.
                        self._mark_stopped(leader, s.symbol,
                                           'long' if s.target_size > 0 else 'short')
                        await self._release_if_flat(leader, s.symbol)
                        await self._skip(leader, s, 'account is flat (native close?); waiting for the leader to close and reopen')
                        continue
                    if symbol_startup:
                        target = s.target_size
                        if live * target < 0:
                            await self._skip(leader, s, 'opposite account position exists')
                            continue
                        gap = max(0, abs(target) - abs(live))
                        carry = self.small_order_carry.get(f'{leader}:{s.symbol}', {})
                        if carry and (gap <= 1e-12 or carry.get('side') != ('buy' if target > 0 else 'sell')):
                            self._set_carry(leader, s.symbol)
                        elif carry and float(carry.get('amount') or 0) > gap:
                            self._set_carry(leader, s.symbol, carry['side'], gap)
                        if not gap:
                            await self._skip(leader, s, 'account position already meets target')
                            continue
                        s = replace(s, delta=gap if target > 0 else -gap)
                        reason = await self._unfavorable_entry(s, target, positions)
                        if reason:
                            await self._skip(leader, s, f'startup price unfavorable: {reason}, current position={live:g}')
                            continue
                    elif resync and s.kind in ('open', 'increase'):
                        # A position discovered by the reconnect snapshot was
                        # not seen as a live signal, so the market may already
                        # be far past the leader's entry.  Apply the same
                        # "no worse than entry" rule as the startup sync.
                        reason = await self._unfavorable_entry(s, s.target_size, positions)
                        if reason:
                            await self._skip(leader, s, f'resync price unfavorable: {reason}')
                            continue
                    if s.kind == 'close_reverse':
                        # The leader closed and reopened on the other side.
                        # Mirror it as one net order from our live position to
                        # the leader's new scaled target.  Skipping this used to
                        # leave the account sitting on the side the leader had
                        # just abandoned.  A reversal is also a fresh episode,
                        # so drop any stale stop-out suppression (including
                        # legacy boolean markers) and old-side carry.
                        target = s.target_size
                        if abs(target) <= 1e-12:
                            await self._skip(leader, s, 'reversal target is flat')
                            continue
                        delta = target - live
                        if abs(delta) <= 1e-12:
                            await self._skip(leader, s, 'account position already matches the reversed target')
                            continue
                        if self._clear_stopped(stopped_key):
                            self._save_state()
                            log.info('STOP-OUT CLEARED leader=%s symbol=%s: leader reversed',
                                     leader, s.symbol)
                        self._set_carry(leader, s.symbol)
                        s = replace(s, delta=delta, side='long' if target > 0 else 'short')
                        # Fall through to the shared entry path below.  No
                        # "worse than leader entry" veto here: staying on the
                        # abandoned side is worse than a slightly late fill.
                    reduce = s.kind in ('decrease', 'close')
                    carry = self.small_order_carry.get(f'{leader}:{s.symbol}', {})
                    carry_amount = float(carry.get('amount') or 0)
                    if reduce:
                        carry_reducing = (carry.get('side') == 'buy' and s.delta < 0) or (carry.get('side') == 'sell' and s.delta > 0)
                        carry_consumed = 0.0
                        if carry_amount and s.kind == 'decrease' and carry_reducing:
                            remaining = max(0, carry_amount - abs(s.delta))
                            self._set_carry(leader, s.symbol, carry.get('side'), remaining)
                            if abs(s.delta) <= carry_amount:
                                await self._skip(leader, s, 'reduction absorbed by unfilled small increases')
                                continue
                            carry_consumed = carry_amount
                        elif carry_amount:
                            self._set_carry(leader, s.symbol)
                        if not live or live * s.delta >= 0:
                            # The account has nothing left to reduce (a native
                            # stop fired, or the position was closed manually)
                            # while the leader still holds it.  Release the
                            # stale claim and remember the stop-out so a later
                            # leader add does not reopen the position.
                            if not live:
                                if s.kind == 'decrease' and self.entered.get(stopped_key):
                                    self._mark_stopped(leader, s.symbol,
                                                       'long' if s.target_size > 0 else 'short')
                                await self._release_if_flat(leader, s.symbol)
                            await self._skip(leader, s, 'no matching account position to reduce')
                            continue
                        if s.kind == 'close':
                            amount = abs(live)
                        else:
                            # Reduce the same fraction of our live position
                            # that the leader closed.  A ratio-scaled absolute
                            # delta drifts whenever equity (and therefore
                            # ratio) moved since the position was opened.
                            old_position = previous.get(s.symbol)
                            new_position = positions.get(s.symbol)
                            old_size = abs(old_position.size) if old_position else 0.0
                            new_size = abs(new_position.size) if new_position else 0.0
                            if old_size > 1e-12:
                                amount = abs(live) * max(0.0, old_size - new_size) / old_size
                            else:
                                amount = abs(s.delta)
                            amount = min(abs(live), max(0.0, amount - carry_consumed))
                    else:
                        amount = abs(s.delta)
                    side = 'buy' if s.delta > 0 else 'sell'
                    if not reduce and not symbol_startup and carry.get('side') == side:
                        amount += carry_amount
                    elif not reduce and carry_amount and carry.get('side') != side:
                        self._set_carry(leader, s.symbol)
                    typ = 'limit' if not symbol_startup and cfg.get('late_entry_enabled') and s.kind == 'open' else 'market'
                    if typ == 'limit' and self.mirror is not None and self.mirror.enabled_for(leader):
                        # The whole point of mirroring is to follow the
                        # leader's order type.  A late-entry offset limit would
                        # turn a leader market order into a resting limit.
                        if leader not in self.mirror._warned_late_entry:
                            self.mirror._warned_late_entry.add(leader)
                            log.warning(
                                'Ignoring late_entry_enabled for mirror leader=%s; '
                                'order type follows the leader', leader)
                        typ = 'market'
                    price = None
                    if typ == 'limit':
                        anchor = positions[s.symbol].entry_price
                        price = anchor * (1 + (-1 if side == 'buy' else 1) * cfg.get('late_entry_offset_pct', 0) / 100)
                    try:
                        order = await self.exchange.create_order(s.symbol, side, amount, typ, price, reduce)
                    except OrderQuantityTooSmall as exc:
                        if not reduce:
                            self._set_carry(leader, s.symbol, side, amount)
                        reason = 'quantity below exchange minimum; waiting to accumulate' if not reduce else 'reduction below exchange minimum'
                        await self._skip(leader, s, f'{reason} ({exc})')
                        continue
                    ordered = True
                    if self.mirror is not None:
                        # The snapshot path already acted on this delta; a
                        # leftover credit must not suppress a future signal.
                        self.mirror.forget_credit(leader, s.symbol)
                    if not reduce:
                        self._set_carry(leader, s.symbol)
                    log.info('ORDER RESULT leader=%s symbol=%s id=%s status=%s amount=%s', leader, s.symbol, order.id, order.status, order.amount)
                    if typ == 'limit' and not reduce and order.status == 'open':
                        self._track_pending(leader, s.symbol, order)
                    if order.status == 'closed' and order.amount:
                        # The amount we actually filled may be rounded down to
                        # the exchange step.  Near the minimum a reversal can
                        # shrink to exactly our old size and only close the
                        # position; recording that as a fresh entry would make
                        # reconciliation mistake the flat account for a native
                        # stop and suppress the symbol.
                        filled = float(order.amount)
                        resulting = live + (filled if side == 'buy' else -filled)
                        if not reduce:
                            if abs(resulting) > 1e-12:
                                self.entered[stopped_key] = True
                                self._save_state()
                            else:
                                await self._release_if_flat(leader, s.symbol, live=resulting)
                        self.fill_events.append({'account': self._account_key(leader), 'leader': leader,
                                                 'order_id': str(order.id), 'symbol': s.symbol,
                                                 'side': side, 'quantity': float(order.amount),
                                                 'price': float(order.price or s.price or 0),
                                                 'reduce_only': reduce, 'timestamp': time.time()})
                    if order.status == 'closed':
                        await self._notify(
                            'notify_filled', leader,
                            replace(s, delta=order.amount if side == 'buy' else -order.amount),
                            order, order_type=typ,
                        )
                    else:
                        await self._notify(
                            'notify_accepted', leader, s, order, order_type=typ,
                        )
                    if reduce:
                        await self._release_if_flat(leader, s.symbol)
                    elif order.status == 'closed':
                        protect.append((leader, s.symbol))
                except Exception as exc:
                    log.exception('Order processing failed: %s %s', leader, s.symbol)
                    if not ordered:
                        failed.add(s.symbol)
                    await self._notify('notify_failed', leader, s, exc)
            # Commit the baseline per symbol.  A symbol whose order failed
            # keeps its previous value, so the next event (or the REST
            # fallback) regenerates the signal instead of silently dropping it.
            committed = dict(positions)
            for symbol in failed:
                if symbol in previous:
                    committed[symbol] = previous[symbol]
                else:
                    committed.pop(symbol, None)
            self.previous[leader] = committed
            if startup:
                self.startup_pending[leader] = set(failed)
            elif startup_symbols:
                remaining = startup_symbols & failed
                if remaining:
                    self.startup_pending[leader] = remaining
                else:
                    self.startup_pending.pop(leader, None)
        # Protection setup is several REST calls; run it after releasing the
        # signal lock so the next leader event is not queued behind it.
        for protect_leader, protect_symbol in protect:
            try:
                await self._sync_protection(protect_leader, protect_symbol)
            except Exception:
                log.warning('Protection setup failed for %s %s', protect_leader, protect_symbol, exc_info=True)
        return signals

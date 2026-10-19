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
    def __init__(self, exchange, leaders, account_state=None, notifier=None, state_store=None, symbol_map=None):
        self.exchange = exchange
        self.leaders = leaders
        self.state = account_state
        self.notifier = notifier
        self.previous = {}
        self.previous_at = {}
        self.paused = False
        self._lock = asyncio.Lock()
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
        # manual close) while the leader still held.  Re-entry is suppressed
        # until the leader goes flat, so an incremental add does not create a
        # fresh position right after a stop.
        self.stopped = self.runtime_state.get('stopped', {})
        # Symbols for which a local opening order actually filled.  A flat
        # account is only a stop-out if we had built a position first; an
        # entry that never filled must keep accumulating normally.
        self.entered = self.runtime_state.get('entered', {})
        self.small_order_carry = self.runtime_state.get('small_order_carry', {})
        # Late-entry limit orders that are still live on the exchange, keyed by
        # ``leader:symbol``.  Without this a fill can arrive after the leader
        # has already closed and leave an orphan position behind.
        self.pending_orders = self.runtime_state.get('pending_orders', {})
        # Console consumes these immutable fill events to attribute exchange
        # realized PnL back to the leader that generated the order.
        self.fill_events = []

    async def update_leader_config(self, leader, changes):
        """Apply validated console changes without replacing the leader map.

        Returns True when the address changed, so the caller can resubscribe
        the WebSocket and seed the new address from REST.
        """
        async with self._lock:
            if leader not in self.leaders:
                raise KeyError(leader)
            old_address = str(self.leaders[leader].get('address') or '').lower()
            new_address = str(changes.get('address') or '').lower()
            address_changed = 'address' in changes and new_address != old_address
            self.leaders[leader].update(changes)
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
            await self.cancel_pending_orders(leader=leader, reason='leader address changed')
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
            self.runtime_state['entered'] = self.entered
            self.runtime_state['small_order_carry'] = self.small_order_carry
            self.runtime_state['pending_orders'] = self.pending_orders
            self.runtime_state['risk'] = self.risk.state()
            self.state_store.save(self.runtime_state)

    def _mark_stopped(self, leader, symbol):
        key = f'{leader}:{symbol}'
        if not self.stopped.get(key):
            self.stopped[key] = True
            self._save_state()

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
        ))

    def _track_pending(self, leader, symbol, order):
        self.pending_orders[self._pending_key(leader, symbol)] = {
            'id': str(order.id), 'leader': leader, 'symbol': symbol,
            'side': order.side, 'amount': float(order.amount or 0),
            'created_at': time.time(),
        }
        self._save_state()

    async def _cancel_pending(self, leader, symbol, reason):
        """Cancel one tracked limit entry; return True once it is off the book."""
        key = self._pending_key(leader, symbol)
        item = self.pending_orders.get(key)
        if not item:
            return False
        order_id = item.get('id')
        try:
            await self.exchange.cancel_order(order_id, symbol)
        except Exception as exc:
            if not self._order_gone(exc):
                log.warning('Failed to cancel pending order %s for %s (%s)', order_id, key, reason, exc_info=True)
                return False
            log.info('Pending order %s for %s is already gone (%s)', order_id, key, reason)
        else:
            log.info('Cancelled pending order %s for %s (%s)', order_id, key, reason)
        self.pending_orders.pop(key, None)
        self._save_state()
        return True

    async def cancel_pending_orders(self, leader=None, reason='cancelled'):
        for item in list(self.pending_orders.values()):
            if leader is not None and item.get('leader') != leader:
                continue
            await self._cancel_pending(item.get('leader'), item.get('symbol'), reason)

    async def expire_pending_orders(self, now=None):
        """Cancel late-entry limits that have stayed unfilled past their TTL."""
        now = time.time() if now is None else now
        async with self._lock:
            for item in list(self.pending_orders.values()):
                leader = item.get('leader')
                symbol = item.get('symbol')
                if not leader or not symbol:
                    continue
                cfg = self.leaders.get(leader) or {}
                ttl = float(cfg.get('late_entry_ttl_seconds', 60) or 60)
                age = now - float(item.get('created_at') or now)
                if age >= ttl:
                    await self._cancel_pending(
                        leader, symbol, f'limit entry unfilled after {age:.0f}s (ttl {ttl:g}s)',
                    )

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
            return False
        self.paused = True
        first_trigger = not locked_before
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
                    await self.cancel_pending_orders(reason='hard stop')
                    cancel_all = getattr(self.exchange, 'cancel_all_orders', None)
                    if cancel_all:
                        await cancel_all()
                except Exception:
                    log.exception('HARD STOP order cancellation incomplete; flattening anyway')
                try:
                    await self.exchange.close_all_positions()
                except Exception:
                    # Keep ``flattened`` false so the next cycle retries.
                    self.risk.flattened = False
                    self._save_state()
                    log.exception('HARD STOP flatten incomplete; retrying next cycle')
                else:
                    self.risk.flattened = True
                    self._save_state()
                    log.critical('HARD STOP flatten complete equity=%g', equity)
        finally:
            if first_trigger and self.notifier:
                await self.notifier.notify_hard_stop(
                    equity, self.risk.baseline,
                    float(self.risk.baseline) - float(equity),
                )
        return True

    async def unlock_hard_stop(self):
        """Manually clear the hard-stop lock and rebaseline to current equity."""
        equity = await self.exchange.equity()
        was_locked = self.risk.locked
        self.risk.unlock(equity)
        self.paused = False
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

    async def _release_if_flat(self, leader, symbol):
        key = self._owner_key(leader, symbol)
        if self.owners.get(key) != leader:
            return
        live = await self.exchange.position_size(symbol)
        if abs(live) <= 1e-12:
            await self._cancel_pending(leader, symbol, 'account position is flat')
            old = self.runtime_state.get('protection', {}).pop(key, None)
            if old:
                try:
                    await self.exchange.cancel_protection(self._protection_id(old), symbol)
                except Exception as exc:
                    # Gate removes a conditional order automatically when the
                    # position reaches zero or its trigger fires, then answers
                    # AUTO_ORDER_NOT_FOUND to a delete.  Treat both flavors of
                    # "order is gone" as an already-completed cancellation
                    # instead of aborting the release with the owner intact.
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
            self._save_state()

    @staticmethod
    def _protection_id(value):
        # State files written by older versions contain the id directly.
        return value.get('id') if isinstance(value, dict) else value

    async def _sync_protection(self, leader, symbol):
        cfg = self.leaders[leader].get('position_stop_loss') or {}
        if not cfg.get('enabled') or cfg.get('max_loss_usdt') is None:
            return
        detail = await self.exchange.fetch_position_detail(symbol)
        if not detail:
            return
        contracts = abs(float(detail.get('contracts') or 0))
        contract_size = abs(float(detail.get('contractSize') or 1))
        position_amount = contracts * contract_size
        entry = float(detail.get('entryPrice') or 0)
        if not position_amount or entry <= 0:
            return
        side = str(detail.get('side') or '').lower()
        loss = float(cfg['max_loss_usdt'])
        # Gate and other contract venues report ``contracts`` rather than
        # base-asset quantity.  Convert before translating a dollar loss into
        # a price distance, otherwise non-unit contract sizes trigger early.
        trigger = entry - loss / position_amount if side == 'long' else entry + loss / position_amount
        mark = float(detail.get('markPrice') or detail.get('markPx') or 0)
        if trigger <= 0 or (mark > 0 and ((side == 'long' and trigger >= mark) or (side == 'short' and trigger <= mark))):
            log.warning('Protection price invalid for %s: side=%s entry=%g mark=%g trigger=%g; no order created', symbol, side, entry, mark, trigger)
            if hasattr(self.exchange, 'cancel_protection_orders'):
                await self.exchange.cancel_protection_orders(symbol)
            return
        # Native stop orders are intentionally owned by the exchange; this demo
        # records the latest id for later replacement/cancellation.
        key = self._owner_key(leader, symbol)
        old = self.runtime_state.get('protection', {}).get(key)
        old_id = self._protection_id(old)
        old_contracts = float(old.get('contracts') or 0) if isinstance(old, dict) else 0
        old_trigger = float(old.get('trigger') or 0) if isinstance(old, dict) else 0
        if hasattr(self.exchange, 'cancel_protection_orders'):
            await self.exchange.cancel_protection_orders(symbol, old_id)
        # Do not churn orders on every reconciliation tick.  Recreate only
        # when the live position or its loss-based trigger has changed.
        if old_id and old_contracts and old_trigger and abs(old_contracts - contracts) <= 1e-12 and abs(old_trigger - trigger) <= max(abs(trigger), 1) * 1e-10:
            return
        if old_id and hasattr(self.exchange, 'cancel_protection'):
            try:
                await self.exchange.cancel_protection(old_id, symbol)
            except Exception:
                # The stop may already have fired or been canceled manually.
                # Treat an unknown old id as already removed and continue.
                log.info('Protection %s for %s is already gone; replacing it', old_id, symbol)
        oid = await self.exchange.set_protection(symbol, 'sell' if side == 'long' else 'buy', contracts, trigger)
        if self.state_store:
            self.runtime_state.setdefault('protection', {})[key] = {
                'id': oid, 'contracts': contracts, 'trigger': trigger,
            }
            self._save_state()

    async def reconcile_protections(self):
        """Reconcile exchange positions with locally tracked protection orders.

        This catches manual closes and partial closes, which do not produce a
        leader signal and therefore never pass through ``process``.
        """
        async with self._lock:
            for key, leader in list(self.owners.items()):
                try:
                    symbol = key.split(':', 1)[1]
                    if self.symbol_map.is_skipped(symbol):
                        continue
                    live = await self.exchange.position_size(symbol)
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
                            self._mark_stopped(leader, symbol)
                        await self._release_if_flat(leader, symbol)
                    else:
                        if not self.entered.get(f'{leader}:{symbol}'):
                            self.entered[f'{leader}:{symbol}'] = True
                            self._save_state()
                        await self._sync_protection(leader, symbol)
                except ValueError as exc:
                    if str(exc).startswith('No USDT perpetual market'):
                        self.runtime_state.get('protection', {}).pop(key, None)
                        self.owners.pop(key, None)
                        self._save_state()
                        log.warning('Dropped protection state for unsupported market %s', symbol)
                    else:
                        log.warning('Protection reconciliation failed for %s', key, exc_info=True)
                except Exception:
                    log.warning('Protection reconciliation failed for %s', key, exc_info=True)

    async def prepare_startup(self):
        # Failure must propagate; unknown account state is never zero exposure.
        await self.exchange.fetch_positions()

    async def _skip(self, leader, signal, reason):
        log.info('ORDER SKIPPED leader=%s symbol=%s reason=%s', leader, signal.symbol, reason)
        if self.notifier:
            await self.notifier.notify_skipped(leader, signal, reason)

    async def process(self, leader, positions):
        async with self._lock:
            cfg = self.leaders[leader]
            denominator = float(cfg['total_margin'])
            if denominator <= 0:
                raise ValueError('total_margin must be positive')
            equity = await self.exchange.equity()
            budget = self.exchange.config.get('fixed_balance_usdt')
            budget = equity if budget is None else float(budget)
            ratio = budget / denominator * float(cfg.get('coefficient', 1))
            startup = leader not in self.previous
            signals = diff_positions(leader, self.previous.get(leader, {}), positions, ratio)
            self.previous[leader] = positions
            self.previous_at[leader] = time.time()
            # A stop-out suppression ends once the leader itself is flat; the
            # next open is then a genuinely new episode worth copying.
            for key in [k for k in self.stopped if k.startswith(f'{leader}:')]:
                symbol = key[len(leader) + 1:]
                position = positions.get(symbol)
                if position is None or abs(position.size) <= 1e-12:
                    self.stopped.pop(key, None)
                    self._save_state()
            if startup:
                for key in list(self.small_order_carry):
                    if key.startswith(f'{leader}:') and key[len(leader) + 1:] not in positions:
                        self.small_order_carry.pop(key)
                        self._save_state()
                log.info('STARTUP leader=%s equity=%g sizing_balance=%g total_margin=%g ratio=%g positions=%d', leader, equity, budget, denominator, ratio, len(positions))
            for s in signals:
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
                    if s.kind in ('open', 'increase', 'close_reverse') and (self.paused or self.risk.locked):
                        reason = 'new exposure is paused' if self.paused else 'account is hard-stop locked'
                        await self._skip(leader, s, reason)
                        continue
                    stopped_key = f'{leader}:{s.symbol}'
                    if stopped_key in self.stopped and s.kind in ('open', 'increase'):
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
                    if s.kind == 'open' and self.pending_orders.get(self._pending_key(leader, s.symbol)):
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
                        self._mark_stopped(leader, s.symbol)
                        await self._release_if_flat(leader, s.symbol)
                        await self._skip(leader, s, 'account is flat (native close?); waiting for the leader to close and reopen')
                        continue
                    if startup:
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
                        current = await self.exchange.entry_quote(s.symbol, 'buy' if target > 0 else 'sell')
                        entry = positions[s.symbol].entry_price
                        if entry <= 0 or current <= 0:
                            raise ValueError('startup price unavailable')
                        if (target > 0 and current > entry) or (target < 0 and current < entry):
                            await self._skip(leader, s, f'startup price unfavorable: market={current:g}, leader entry={entry:g}, target={target:g}, current position={live:g}')
                            continue
                    if s.kind == 'close_reverse':
                        await self._skip(leader, s, 'reversal requires reconciliation; automatic reversal disabled')
                        continue
                    reduce = s.kind in ('decrease', 'close')
                    carry = self.small_order_carry.get(f'{leader}:{s.symbol}', {})
                    carry_amount = float(carry.get('amount') or 0)
                    if reduce:
                        carry_reducing = (carry.get('side') == 'buy' and s.delta < 0) or (carry.get('side') == 'sell' and s.delta > 0)
                        if carry_amount and s.kind == 'decrease' and carry_reducing:
                            remaining = max(0, carry_amount - abs(s.delta))
                            self._set_carry(leader, s.symbol, carry.get('side'), remaining)
                            if abs(s.delta) <= carry_amount:
                                await self._skip(leader, s, 'reduction absorbed by unfilled small increases')
                                continue
                            s = replace(s, delta=(abs(s.delta) - carry_amount) * (1 if s.delta > 0 else -1))
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
                                    self._mark_stopped(leader, s.symbol)
                                await self._release_if_flat(leader, s.symbol)
                            await self._skip(leader, s, 'no matching account position to reduce')
                            continue
                        amount = abs(live) if s.kind == 'close' else min(abs(live), abs(s.delta))
                    else:
                        amount = abs(s.delta)
                    side = 'buy' if s.delta > 0 else 'sell'
                    if not reduce and not startup and carry.get('side') == side:
                        amount += carry_amount
                    elif not reduce and carry_amount and carry.get('side') != side:
                        self._set_carry(leader, s.symbol)
                    typ = 'limit' if not startup and cfg.get('late_entry_enabled') and s.kind == 'open' else 'market'
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
                    if not reduce:
                        self._set_carry(leader, s.symbol)
                    log.info('ORDER RESULT leader=%s symbol=%s id=%s status=%s amount=%s', leader, s.symbol, order.id, order.status, order.amount)
                    if typ == 'limit' and not reduce and order.status == 'open':
                        self._track_pending(leader, s.symbol, order)
                    if order.status == 'closed' and order.amount:
                        if not reduce:
                            self.entered[stopped_key] = True
                            self._save_state()
                        self.fill_events.append({'account': self._account_key(leader), 'leader': leader,
                                                 'order_id': str(order.id), 'symbol': s.symbol,
                                                 'side': side, 'quantity': float(order.amount),
                                                 'price': float(order.price or s.price or 0),
                                                 'reduce_only': reduce, 'timestamp': time.time()})
                    if self.notifier:
                        if order.status == 'closed':
                            await self.notifier.notify_filled(leader, replace(s, delta=order.amount if side == 'buy' else -order.amount), order)
                        else:
                            await self.notifier.notify_accepted(leader, s, order)
                    if reduce:
                        await self._release_if_flat(leader, s.symbol)
                    elif order.status == 'closed':
                        await self._sync_protection(leader, s.symbol)
                except Exception as exc:
                    log.exception('Order processing failed: %s %s', leader, s.symbol)
                    if self.notifier:
                        await self.notifier.notify_failed(leader, s, exc)
            return signals

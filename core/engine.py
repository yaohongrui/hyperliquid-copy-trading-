import asyncio
import logging
from dataclasses import replace
from .detector import diff_positions

log = logging.getLogger(__name__)

class Engine:
    def __init__(self, exchange, leaders, account_state=None, notifier=None):
        self.exchange = exchange
        self.leaders = leaders
        self.state = account_state
        self.notifier = notifier
        self.previous = {}
        self.paused = False
        self._lock = asyncio.Lock()

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
            if startup:
                log.info('STARTUP leader=%s equity=%g sizing_balance=%g total_margin=%g ratio=%g positions=%d', leader, equity, budget, denominator, ratio, len(positions))
            for s in signals:
                try:
                    if s.symbol in cfg.get('symbol_blacklist', []) or s.symbol + 'USDT' in cfg.get('symbol_blacklist', []):
                        await self._skip(leader, s, 'symbol is blacklisted')
                        continue
                    if self.paused and s.kind in ('open', 'increase', 'close_reverse'):
                        await self._skip(leader, s, 'new exposure is paused')
                        continue
                    live = await self.exchange.position_size(s.symbol)
                    if startup:
                        target = s.target_size
                        if live * target < 0:
                            await self._skip(leader, s, 'opposite account position exists')
                            continue
                        gap = max(0, abs(target) - abs(live))
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
                    if reduce:
                        if not live or live * s.delta >= 0:
                            await self._skip(leader, s, 'no matching account position to reduce')
                            continue
                        amount = abs(live) if s.kind == 'close' else min(abs(live), abs(s.delta))
                    else:
                        amount = abs(s.delta)
                    side = 'buy' if s.delta > 0 else 'sell'
                    typ = 'limit' if not startup and cfg.get('late_entry_enabled') and s.kind == 'open' else 'market'
                    price = None
                    if typ == 'limit':
                        anchor = positions[s.symbol].entry_price
                        price = anchor * (1 + (-1 if side == 'buy' else 1) * cfg.get('late_entry_offset_pct', 0) / 100)
                    order = await self.exchange.create_order(s.symbol, side, amount, typ, price, reduce)
                    log.info('ORDER RESULT leader=%s symbol=%s id=%s status=%s amount=%s', leader, s.symbol, order.id, order.status, order.amount)
                    if self.notifier:
                        if order.status == 'closed':
                            await self.notifier.notify_filled(leader, replace(s, delta=order.amount if side == 'buy' else -order.amount), order)
                        else:
                            await self.notifier.notify_accepted(leader, s, order)
                except Exception as exc:
                    log.exception('Order processing failed: %s %s', leader, s.symbol)
                    if self.notifier:
                        await self.notifier.notify_failed(leader, s, exc)
            return signals

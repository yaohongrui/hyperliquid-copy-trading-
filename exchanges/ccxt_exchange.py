import asyncio
import logging
import ccxt.async_support as ccxt
from decimal import Decimal
import time

from core.models import Order
from core.symbols import SymbolMap, normalize
from .base import OrderQuantityTooSmall

log = logging.getLogger(__name__)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class CcxtExchange:
    """Small adapter for the configured USDT perpetual account."""

    def __init__(self, config, symbol_map=None):
        self.config = config
        self.symbol_map = symbol_map if isinstance(symbol_map, SymbolMap) else SymbolMap(symbol_map)
        name = str(config.get("exchange", "")).lower()
        if name == "gate":
            cls = ccxt.gate
            secret = config.get("secret_key")
        elif name == "binance":
            cls = ccxt.binanceusdm
            secret = config.get("api_secret")
        else:
            raise ValueError(f"Unsupported execution exchange: {name}")
        self.name = name
        self.client = cls({
            "apiKey": config.get("api_key"),
            "secret": secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": "swap",
                "fetchOpenOrders": {"warnWithoutSymbol": False},
            },
        })
        self._markets_loaded = False
        # set_leverage is a signed REST call; the configured leverage is
        # static, so only send it when the market's cached value changes.
        self._leverage_set = {}
        # binanceusdm leaves market.limits.leverage.max empty, so the per-symbol
        # cap is resolved from the leverage brackets once and cached.
        self._leverage_max = {}
        self._ledger_since = None

    async def start(self):
        await self.client.load_markets()
        self._markets_loaded = True

    async def stop(self):
        await self.client.close()

    async def _binance_max_leverage(self, market):
        """Highest leverage Binance accepts for ``market`` (tier-1 bracket)."""
        if market not in self._leverage_max:
            try:
                tiers = await self.client.fetch_leverage_tiers([market])
            except (NotImplementedError, AttributeError, ccxt.NotSupported, ccxt.ExchangeError):
                tiers = {}
            values = tiers.get(market) if isinstance(tiers, dict) else tiers
            self._leverage_max[market] = max(
                (_float(row.get('maxLeverage')) for row in values or []), default=0.0) or None
        return self._leverage_max[market]

    def _symbol(self, symbol):
        raw = str(symbol or "").strip().upper().replace("_", "/")
        market = self.client.markets.get(raw)
        if market and market.get("swap"):
            return raw
        mapped = self.symbol_map.target(symbol)
        if mapped:
            candidate = mapped.strip().upper()
            if "/" in candidate and candidate in self.client.markets:
                if not self._is_usdt_swap(self.client.markets[candidate]):
                    raise ValueError(f"symbol_map target {mapped} is not a USDT perpetual market")
                return candidate
            base = normalize(mapped)
        else:
            # Hyperliquid HIP-3 symbols are namespaced (e.g. XYZ:AMD), while
            # Binance/Gate list the underlying perpetual as AMD/USDT:USDT.
            base = normalize(symbol)
        candidate = f"{base}/USDT:USDT"
        market = self.client.markets.get(candidate)
        if market and self._is_usdt_swap(market):
            return candidate
        for candidate, market in self.client.markets.items():
            if self._is_usdt_swap(market) and market.get("base", "").upper() == base:
                return candidate
        if mapped:
            raise ValueError(f"No USDT perpetual market for {symbol} (symbol_map -> {mapped})")
        raise ValueError(f"No USDT perpetual market for {symbol}; add a symbol_map entry if the venue names it differently")

    def _unified_symbol(self, contract):
        """Map Gate's ``BASE_QUOTE`` contract id to the unified swap symbol."""
        for symbol, market in self.client.markets.items():
            if market.get('id') == contract and market.get('swap'):
                return symbol
        return contract

    @staticmethod
    def _is_usdt_swap(market):
        return bool(market.get("swap")) and market.get("quote", "").upper() == "USDT"

    def validate_symbol_map(self):
        """Resolve every mapping once so typos fail at startup, not at order time."""
        errors = []
        for name, target in self.symbol_map.entries().items():
            try:
                self._symbol(name)
            except ValueError as exc:
                errors.append(f"{name} -> {target}: {exc}")
        if errors:
            raise ValueError("; ".join(errors))

    async def create_order(self, symbol, side, amount, order_type="market", price=None, reduce_only=False):
        market = self._symbol(symbol)
        contract_size = float(self.client.markets[market].get('contractSize') or 1)
        try:
            contracts = float(self.client.amount_to_precision(market, amount / contract_size))
        except ccxt.InvalidOrder as exc:
            raise OrderQuantityTooSmall(str(exc)) from exc
        minimum = self.client.markets[market].get('limits', {}).get('amount', {}).get('min') or 0
        if contracts <= 0 or Decimal(str(contracts)) < Decimal(str(minimum)):
            raise OrderQuantityTooSmall(f'quantity {contracts:g} is below exchange minimum {minimum}')
        if order_type == 'limit' and (price is None or price <= 0):
            raise ValueError('limit price must be positive')
        params = {"reduceOnly": True} if reduce_only else {}
        # Gate perpetual market orders require an explicit IOC/FOK policy.
        if self.name == "gate" and order_type == "market":
            # ccxt versions differ in which alias they forward to Gate.
            params["timeInForce"] = "IOC"
            params["tif"] = "ioc"
        leverage = self.config.get("leverage")
        if leverage and not reduce_only:
            maximum = self.client.markets[market].get('limits', {}).get('leverage', {}).get('max')
            if not maximum and self.name == 'binance':
                maximum = await self._binance_max_leverage(market)
            effective = min(int(leverage), int(maximum)) if maximum else int(leverage)
            if self._leverage_set.get(market) != effective:
                if self.name == 'gate' and self.config.get('margin_mode') in ('cross', 'crossed'):
                    await self.client.set_leverage(0, market, {'cross_leverage_limit': effective})
                else:
                    await self.client.set_leverage(effective, market)
                self._leverage_set[market] = effective
        try:
            result = await self.client.create_order(market, order_type, side, contracts, price, params)
        except ccxt.InvalidOrder as exc:
            # Binance enforces a separate minimum notional (currently often
            # 5 USDT) at order submission. Treat it like a small quantity so
            # the engine can accumulate this increment for a later retry.
            message = str(exc)
            lowered = message.lower()
            small_order_error = (
                '-4164' in lowered
                or 'notional must be no smaller than' in lowered
                or 'minimum notional' in lowered
                or 'minimum order size' in lowered
                or 'minimum order amount' in lowered
                or 'order size is too small' in lowered
                or 'amount is less than the minimum' in lowered
            )
            if small_order_error:
                raise OrderQuantityTooSmall(message) from exc
            raise
        return Order(
            str(result.get("id", "")), symbol, side, float(result.get('filled') or result.get('amount') or contracts) * contract_size, result.get('average') or result.get('price'),
            result.get("status", "open"), reduce_only,
        )

    async def fetch_positions(self):
        return await self.client.fetch_positions()

    async def fetch_position_detail(self, symbol):
        market = self._symbol(symbol)
        rows = await self.client.fetch_positions([market])
        for row in rows:
            if row.get('symbol') == market and float(row.get('contracts') or 0):
                return row
        return None

    async def set_protection(self, symbol, side, amount, trigger_price):
        """Place an exchange-native stop that closes the whole position.

        ``closePosition=true`` is important here: a stop order with a fixed
        quantity can remain too large after a manual partial close. Binance
        rejects ``quantity``/``reduceOnly`` together with closePosition and
        resolves the quantity when the trigger fires.
        """
        market = self._symbol(symbol)
        trigger = float(self.client.price_to_precision(market, trigger_price))
        if self.name == 'binance':
            params = {'stopPrice': trigger, 'closePosition': True}
            result = await self.client.create_order(market, 'STOP_MARKET', side, None, None, params)
        elif self.name == 'gate':
            # Gate's futures API exposes trigger orders through CCXT's
            # unified triggerPrice parameter.  reduceOnly keeps the order
            # from opening a reverse position when it fires.
            params = {'triggerPrice': trigger, 'reduceOnly': True}
            result = await self.client.create_order(market, 'market', side, amount, None, params)
        else:
            params = {'triggerPrice': trigger, 'reduceOnly': True}
            result = await self.client.create_order(market, 'market', side, amount, None, params)
        return str(result.get('id', ''))

    async def cancel_protection(self, order_id, symbol):
        if self.name == 'gate':
            return await self.client.privateFuturesDeleteSettlePriceOrdersOrderId({
                'settle': self.config.get('settle', 'usdt'),
                'order_id': str(order_id),
            })
        return await self.cancel_order(order_id, symbol)

    async def cancel_protection_orders(self, symbol, keep_id=None):
        """Remove duplicate conditional reduce-only orders for one market."""
        if self.name == 'gate':
            market = self._symbol(symbol)
            contract = self.client.markets[market]['id']
            orders = await self.client.privateFuturesGetSettlePriceOrders({
                'settle': self.config.get('settle', 'usdt'), 'status': 'open',
            })
            for order in orders or []:
                initial = order.get('initial') or {}
                oid = str(order.get('id_string') or order.get('id') or '')
                if (oid and oid != str(keep_id or '') and
                        str(initial.get('contract', '')).upper() == contract.upper() and
                        bool(initial.get('is_reduce_only') or initial.get('reduce_only'))):
                    await self.cancel_protection(oid, symbol)
            return
        market = self._symbol(symbol)
        for order in await self.client.fetch_open_orders(market):
            oid = str(order.get('id') or '')
            if keep_id and oid == str(keep_id):
                continue
            info = order.get('info') or {}
            conditional = order.get('triggerPrice') is not None or order.get('stopPrice') is not None
            conditional = conditional or bool(info.get('trigger') or info.get('stopPrice') or info.get('stop_price'))
            reduce_only = order.get('reduceOnly')
            if reduce_only is None:
                reduce_only = info.get('reduceOnly', info.get('reduce_only', False))
            if oid and conditional and reduce_only:
                await self.cancel_order(oid, symbol)

    async def fetch_open_orders(self):
        return await self.client.fetch_open_orders()

    async def fetch_realized_pnl(self):
        """Return recent exchange realized-PnL rows in the console schema.

        Rows carry ``source`` so the store can retire rows from a superseded
        feed (e.g. Gate trade history, which reports no PnL at all).

        Gate's closed-position endpoint only reports a position once the whole
        episode is over, and it merges a stop-out with a reopen into a single
        still-open episode.  The account book (ledger) is the only feed that
        carries every realized close, so Gate rows come from there.
        """
        if self.name == 'gate':
            rows = await self._gate_ledger_rows()
            if rows is not None:
                return rows
        if self.name == 'binance' and self.client.has.get('fetchLedger'):
            # binanceusdm exposes neither fetchIncome nor an account-wide
            # fetchMyTrades, but its account book (the income endpoint) carries
            # every REALIZED_PNL row.  Commission rows share the trade id, so
            # fold them into the same console row instead of losing the fee.
            try:
                ledger = await self.client.fetch_ledger(limit=1000)
            except (NotImplementedError, AttributeError, ccxt.NotSupported):
                ledger = None
            if ledger is not None:
                rows, fees = {}, {}
                for entry in ledger:
                    info = entry.get('info') or {}
                    kind = str(info.get('incomeType') or '').upper()
                    trade_id = str(info.get('tradeId') or '')
                    if kind == 'COMMISSION':
                        if trade_id:
                            fees[trade_id] = fees.get(trade_id, 0.0) + abs(_float(entry.get('amount')))
                        continue
                    if kind != 'REALIZED_PNL':
                        continue
                    timestamp = entry.get('timestamp')
                    if timestamp and timestamp > 10_000_000_000:
                        timestamp /= 1000
                    raw_symbol = info.get('symbol')
                    rows[trade_id or entry.get('id')] = {
                        'id': trade_id or entry.get('id'), 'source': 'income',
                        'symbol': self._unified_symbol(raw_symbol) if raw_symbol else None,
                        'side': None, 'amount': 0.0, 'price': 0.0,
                        'realizedPnl': _float(entry.get('amount')), 'fee': 0.0,
                        'timestamp': timestamp,
                    }
                for key, row in rows.items():
                    row['fee'] = fees.get(key, 0.0)
                return list(rows.values())
        if self.client.has.get('fetchIncome'):
            try:
                income = await self.client.fetch_income(limit=100)
                rows = []
                for row in income or []:
                    info = row.get('info') or {}
                    kind = str(row.get('type') or info.get('incomeType') or info.get('type') or '').lower()
                    if kind and not any(x in kind for x in ('realized', 'pnl', 'profit')):
                        continue
                    amount = row.get('amount')
                    if amount is None:
                        amount = info.get('income', info.get('realizedPnl', info.get('realised_pnl', 0)))
                    rows.append({'id': row.get('id') or info.get('tranId'), 'source': 'income',
                                 'symbol': row.get('symbol'), 'side': row.get('side'),
                                 'amount': row.get('amount'), 'price': row.get('price'),
                                 'realizedPnl': float(amount or 0), 'fee': 0,
                                 'timestamp': row.get('timestamp')})
                if rows:
                    return rows
            except (NotImplementedError, AttributeError, ccxt.NotSupported):
                pass
        # Gate never reports realized PnL through trade history; the closed
        # position endpoint is the only source that carries it.
        if self.client.has.get('fetchPositionsHistory'):
            try:
                closed = await self.client.fetch_positions_history(limit=100)
            except (NotImplementedError, AttributeError, ccxt.NotSupported):
                closed = []
            rows = [self._closed_position_row(row) for row in closed or []]
            rows = [row for row in rows if row]
            if rows:
                return rows
        if not self.client.has.get('fetchMyTrades'):
            return []
        try:
            rows = await self.client.fetch_my_trades(limit=100)
        except ccxt.ArgumentsRequired:
            # Some venues (binanceusdm) cannot list account-wide trades.
            return []
        result = []
        for row in rows or []:
            info = row.get('info') or {}
            realized = row.get('realizedPnl')
            if realized is None:
                realized = info.get('realizedPnl', info.get('realised_pnl', info.get('realized_pnl', 0)))
            fee = row.get('fee') or {}
            result.append({
                'id': row.get('id'), 'source': 'my_trades', 'symbol': row.get('symbol'), 'side': row.get('side'),
                'amount': row.get('amount'), 'price': row.get('price'), 'realizedPnl': float(realized or 0),
                'fee': float(fee.get('cost') or 0), 'timestamp': row.get('timestamp'),
            })
        return result

    async def _gate_ledger_rows(self):
        """Realized closes from Gate's futures account book.

        Each ``pnl`` entry is one closing execution and references the order in
        its ``text`` (``CONTRACT:ORDER_ID``); sibling ``fee`` entries share the
        same text, so they fold into the same console row.  The dedupe key is
        stable, which lets repeated refreshes insert idempotently.  Pagination
        walks backwards with ``to`` until a page comes back short.

        Once a watermark exists, only the recent overlap window is fetched;
        the full backward walk is reserved for startup or a backlog.
        """
        settle = self.config.get('settle', 'usdt')
        since = getattr(self, '_ledger_since', None)
        newest = since
        rows = {}
        until = None
        for _ in range(10):
            params = {'settle': settle, 'limit': 100}
            if since is not None:
                params['from'] = max(0, int(since) - 300)
            if until is not None:
                params['to'] = until
            try:
                book = await self.client.privateFuturesGetSettleAccountBook(params)
            except (AttributeError, ccxt.NotSupported, ccxt.PermissionDenied, ccxt.AuthenticationError, ccxt.ExchangeError):
                return None if not rows else self._ledger_rows_result(rows)
            if not book:
                break
            oldest = None
            for entry in book:
                stamp = _float(entry.get('time'))
                if stamp and (oldest is None or stamp < oldest):
                    oldest = stamp
                if stamp and (newest is None or stamp > newest):
                    newest = stamp
                kind = str(entry.get('type') or '').lower()
                if kind not in ('pnl', 'fee'):
                    continue
                text = str(entry.get('text') or '')
                contract, _, order_id = text.partition(':')
                if not contract or not order_id:
                    continue
                key = (contract, order_id)
                row = rows.setdefault(key, {'id': f"ledger:{contract}:{order_id}", 'source': 'ledger',
                                            'symbol': self._unified_symbol(contract),
                                            'side': None, 'amount': 0, 'price': 0,
                                            'realizedPnl': 0.0, 'fee': 0.0, 'timestamp': 0.0,
                                            'has_pnl': False})
                change = _float(entry.get('change'))
                if kind == 'pnl':
                    row['realizedPnl'] += change
                    row['has_pnl'] = True
                else:
                    # Fees are negative changes in the book; store cost positive.
                    row['fee'] -= change
                if stamp > row['timestamp']:
                    row['timestamp'] = stamp
            if len(book) < 100 or oldest is None:
                break
            until = int(oldest) - 1
        if newest is not None:
            self._ledger_since = newest
        return self._ledger_rows_result(rows)

    @staticmethod
    def _ledger_rows_result(rows):
        # Fee-only keys belong to opening executions; without a closing leg
        # they would fabricate losing trades.  A closing leg with exactly zero
        # PnL is still a real close, so keep it.
        result = []
        for row in rows.values():
            if row.pop('has_pnl', False):
                result.append(row)
        return result

    @staticmethod
    def _closed_position_row(row):
        """Map a closed-position record onto the console trade schema.

        Gate splits PnL into price/fee/funding legs; the legs must stay separate
        because the console computes ``net = gross - fees + funding`` and the
        endpoint's total ``pnl`` already folds in fee and funding.
        """
        info = row.get('info') or {}
        # Only Gate's closed-position payload carries these legs; other venues
        # also expose position history and must not be parsed as Gate records.
        if info.get('contract') is None or info.get('pnl_pnl') is None:
            return None
        identifier = f"{info.get('contract')}:{info.get('time')}"
        if identifier == ':None' or identifier == ':':
            identifier = f"{row.get('symbol')}:{row.get('timestamp')}"
        if not identifier or identifier == ':':
            return None
        return {
            'id': identifier, 'source': 'position_close',
            'symbol': row.get('symbol'), 'side': row.get('side'),
            'amount': _float(info.get('accum_size')),
            'price': _float(info.get('long_price') or info.get('short_price')),
            'realizedPnl': _float(info.get('pnl_pnl')),
            'fee': -_float(info.get('pnl_fee')),
            'funding': _float(info.get('pnl_fund')),
            'timestamp': row.get('timestamp'),
        }

    async def fetch_cash_flows(self):
        """Normalize deposits, withdrawals and funding payments for analytics."""
        rows = []
        for method, kind, sign in (("fetch_deposits", "deposit", 1), ("fetch_withdrawals", "withdrawal", -1),
                                   ("fetch_funding_history", "funding", 1)):
            fetcher = getattr(self.client, method, None)
            if not fetcher or self.client.has.get(method) is False:
                continue
            try:
                values = await fetcher(limit=100)
            except (NotImplementedError, AttributeError, ccxt.NotSupported, ccxt.PermissionDenied):
                continue
            for row in values or []:
                amount = row.get("amount")
                if amount is None:
                    info = row.get("info") or {}
                    amount = info.get("income", info.get("amount", info.get("realizedPnl", 0)))
                rows.append({"id": row.get("id"), "kind": kind, "amount": float(amount or 0) * sign,
                             "timestamp": row.get("timestamp") or time.time() * 1000})
        for row in rows:
            if row["timestamp"] > 10_000_000_000:
                row["timestamp"] /= 1000
        return rows

    async def position_size(self, symbol):
        market = self._symbol(symbol)
        rows = await self.client.fetch_positions([market])
        return sum(float(p.get('contracts') or 0) * float(p.get('contractSize') or self.client.markets[market].get('contractSize') or 1) * (-1 if p.get('side') == 'short' else 1) for p in rows if p.get('symbol') == market)

    async def entry_quote(self, symbol, side):
        book = await self.client.fetch_order_book(self._symbol(symbol), 5)
        levels = book['asks' if side == 'buy' else 'bids']
        if not levels:
            raise ValueError('empty execution order book')
        return float(levels[0][0])

    async def equity(self):
        balance = await self.client.fetch_balance({'type': 'swap'})
        if self.name == 'gate':
            rows = balance.get('info') or []
            row = next(x for x in rows if str(x.get('currency')).upper() == 'USDT')
            return float(row['total']) + float(row.get('unrealised_pnl') or 0)
        account = balance.get('USDT') or {}
        wallet_equity = float(account.get('total') or account.get('free') or 0)
        positions = await self.client.fetch_positions()
        unrealized = sum(float(position.get('unrealizedPnl') or 0) for position in positions)
        return wallet_equity + unrealized

    async def wallet_balance(self):
        """Return wallet balance excluding current position unrealized PnL."""
        balance = await self.client.fetch_balance({'type': 'swap'})
        if self.name == 'gate':
            rows = balance.get('info') or []
            row = next(x for x in rows if str(x.get('currency')).upper() == 'USDT')
            return float(row.get('total') or 0)
        account = balance.get('USDT') or {}
        return float(account.get('total') or account.get('free') or 0)

    @staticmethod
    def _order_gone(exc):
        message = str(exc).upper()
        return any(token in message for token in (
            'ORDER_NOT_FOUND', 'ORDER NOT FOUND', 'NOT_FOUND', 'NOT FOUND',
            'UNKNOWN_ORDER', 'UNKNOWN ORDER', 'ALREADY FILLED', 'ALREADY CLOSED',
            'NOT OPEN', 'DOES NOT EXIST', 'NO SUCH ORDER',
        ))

    async def cancel_all_orders(self):
        """Cancel every regular and conditional order on the account."""
        canceled = 0
        errors = []
        try:
            orders = await self.client.fetch_open_orders()
        except Exception as exc:
            orders = []
            errors.append(exc)
        for order in orders or []:
            order_id = order.get('id')
            if not order_id:
                continue
            try:
                await self.client.cancel_order(order_id, order.get('symbol'))
                canceled += 1
            except Exception as exc:
                if not self._order_gone(exc):
                    errors.append(exc)
        if self.name == 'gate':
            # Gate keeps trigger/conditional orders outside fetch_open_orders.
            try:
                rows = await self.client.privateFuturesGetSettlePriceOrders({
                    'settle': self.config.get('settle', 'usdt'), 'status': 'open',
                })
            except (AttributeError, ccxt.NotSupported):
                rows = []
            except Exception as exc:
                rows = []
                errors.append(exc)
            for row in rows or []:
                order_id = str(row.get('id_string') or row.get('id') or '')
                if not order_id:
                    continue
                try:
                    await self.cancel_protection(order_id, None)
                    canceled += 1
                except Exception as exc:
                    if not self._order_gone(exc):
                        errors.append(exc)
        if errors:
            raise RuntimeError(f'failed to cancel {len(errors)} order(s): {errors[0]}') from errors[0]
        return canceled

    async def has_open_positions(self):
        return any(abs(float(position.get('contracts') or 0)) > 0
                   for position in await self.client.fetch_positions())

    async def close_all_positions(self, attempts=3):
        """Cancel orders and market-close every open USDT perpetual position.

        Retries the whole cancel/close cycle so a transient rejection or a
        partially filled book cannot leave exposure open.  Raises if positions
        remain, which lets the caller retry instead of recording a false
        "flattened" state.
        """
        closed = 0
        last_error = None
        for attempt in range(attempts):
            cancel_error = None
            try:
                await self.cancel_all_orders()
            except Exception as exc:
                cancel_error = exc
                log.warning('Failed to cancel orders before flattening', exc_info=True)
            positions = [position for position in await self.client.fetch_positions()
                         if position.get('symbol') and abs(float(position.get('contracts') or 0)) > 0]
            if not positions and cancel_error is None:
                return closed
            for position in positions:
                symbol = position.get('symbol')
                contracts = abs(float(position.get('contracts') or 0))
                contract_size = float(
                    position.get('contractSize')
                    or self.client.markets.get(symbol, {}).get('contractSize')
                    or 1
                )
                side = 'sell' if str(position.get('side')).lower() == 'long' else 'buy'
                try:
                    # create_order takes base-asset quantity and converts it to
                    # contracts itself, so the position's contract count must
                    # be multiplied by contractSize first.
                    await self.create_order(
                        symbol, side, contracts * contract_size, 'market', reduce_only=True,
                    )
                    closed += 1
                except Exception as exc:
                    last_error = exc
                    log.warning('Failed to close %s (%g contracts)', symbol, contracts, exc_info=True)
            if attempt < attempts - 1:
                await asyncio.sleep(1)
        remaining = [position for position in await self.client.fetch_positions()
                     if position.get('symbol') and abs(float(position.get('contracts') or 0)) > 0]
        if remaining:
            raise RuntimeError(
                'failed to flatten position(s): '
                + ', '.join(str(position.get('symbol')) for position in remaining)
            ) from last_error
        await self.cancel_all_orders()
        return closed

    async def cancel_order(self, order_id, symbol=None):
        return await self.client.cancel_order(order_id, self._symbol(symbol) if symbol else None)

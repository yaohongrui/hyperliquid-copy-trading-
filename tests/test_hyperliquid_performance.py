import unittest
from unittest.mock import patch

from exchanges.hyperliquid_exchange import HyperliquidExchange


class FakeInfo:
    """Offline stand-in for ``hyperliquid.info.Info``."""

    coin_to_asset = {"BTC": 0, "ETH": 1}
    asset_to_sz_decimals = {0: 5, 1: 4}

    def __init__(self, fills=None, funding=None, ledger=None, orders=None):
        self.fills = list(fills or [])
        self.funding = list(funding or [])
        self.ledger = list(ledger or [])
        self.orders = dict(orders or {})
        self.calls = []

    def user_fills_by_time(self, address, start_time, end_time=None, aggregate_by_time=False):
        self.calls.append(("fills", start_time, end_time, aggregate_by_time))
        return list(self.fills)

    def user_funding_history(self, user, startTime, endTime=None):
        self.calls.append(("funding", startTime, endTime))
        return list(self.funding)

    def user_non_funding_ledger_updates(self, user, startTime, endTime=None):
        self.calls.append(("ledger", startTime, endTime))
        return list(self.ledger)

    def query_order_by_oid(self, user, oid):
        self.calls.append(("order", oid))
        return self.orders.get(int(oid))


def make_exchange(info=None, config=None):
    exchange = HyperliquidExchange(config or {})
    exchange.info = info or FakeInfo()
    exchange.address = "0xabc"
    exchange.dexes = [""]
    exchange._coins = {"BTC": "BTC", "ETH": "ETH"}
    return exchange


def _order(order, status):
    return {"status": "order", "order": {"order": order, "status": status}}


class AdapterConfigTests(unittest.TestCase):
    def test_name_and_request_timeout_defaults(self):
        exchange = HyperliquidExchange({})
        self.assertEqual(exchange.name, "hyperliquid")
        self.assertEqual(exchange.request_timeout, 10)
        self.assertEqual(HyperliquidExchange({"request_timeout_seconds": 3}).request_timeout, 3)

    def test_default_cash_flow_types_cover_external_capital(self):
        exchange = HyperliquidExchange({})
        defaults = {name.lower() for name in exchange.cash_flow_types}
        self.assertTrue({
            "deposit", "withdraw", "send", "receive", "internaltransfer",
            "spottransfer", "vaultdeposit", "vaultwithdraw",
        } <= defaults)
        # Perp<->spot moves are internal and must not count as capital flow.
        self.assertNotIn("accountclasstransfer", defaults)
        self.assertNotIn("subaccounttransfer", defaults)


class FillMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_perp_fills_are_mapped_and_spot_rows_filtered(self):
        info = FakeInfo(fills=[
            {"tid": 111, "oid": 42, "coin": "BTC", "side": "B", "sz": "0.5",
             "px": "100", "closedPnl": "5", "fee": "0.1", "time": 1700000000000},
            # Spot fills have no closedPnl and must be dropped.
            {"tid": 112, "oid": 43, "coin": "PURR", "side": "A", "sz": "10",
             "px": "0.1", "fee": "0", "time": 1700000001000},
        ])
        exchange = make_exchange(info)

        rows = await exchange.fetch_realized_pnl(since=1700000000, until=1700000100)

        self.assertEqual(info.calls[0], ("fills", 1700000000000, 1700000100000, False))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], {
            "id": "hl:fill:111", "symbol": "BTC", "side": "buy", "amount": 0.5,
            "price": 100.0, "realizedPnl": 5.0, "fee": 0.1, "funding": 0.0,
            "order_id": "42", "source": "hl_fill", "timestamp": 1700000000.0,
        })

    async def test_sell_side_and_missing_tid_fallback(self):
        info = FakeInfo(fills=[
            {"hash": "0xabc", "oid": 9, "coin": "ETH", "side": "A", "sz": "2",
             "px": "50", "closedPnl": "-1", "fee": "0.02", "time": 1700000000000},
        ])
        exchange = make_exchange(info)

        rows = await exchange.fetch_performance_rows(since=1700000000, until=1700000100)

        self.assertEqual(rows[0]["side"], "sell")
        self.assertEqual(rows[0]["id"], "hl:fill:0xabc:9:1700000000000:ETH")
        self.assertEqual(rows[0]["realizedPnl"], -1.0)


ZERO_HASH = "0x" + "0" * 64


class FundingMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_funding_rows_are_zero_trade_count_entries(self):
        info = FakeInfo(funding=[
            {"hash": "0xf1", "time": 1700000100000,
             "delta": {"usdc": "-0.25", "coin": "USDC"}},
        ])
        exchange = make_exchange(info)

        rows = await exchange.fetch_performance_rows(since=1700000000, until=1700000200)

        self.assertEqual(rows, [{
            "id": "hl:funding:0xf1:USDC:1700000100000:-0.25",
            "symbol": "USDC", "side": "", "amount": 0.0,
            "price": 0.0, "realizedPnl": 0.0, "fee": 0.0, "funding": -0.25,
            "order_id": None, "source": "hl_funding", "timestamp": 1700000100.0,
        }])

    async def test_zero_hash_funding_rows_do_not_collapse(self):
        # Live ``user_funding_history`` returns an all-zero top-level hash and
        # puts the coin inside delta.
        info = FakeInfo(funding=[
            {"hash": ZERO_HASH, "time": 1700000100000,
             "delta": {"usdc": "-0.25", "coin": "ETH"}},
            {"hash": ZERO_HASH, "time": 1700000200000,
             "delta": {"usdc": "0.5", "coin": "ETH"}},
            {"hash": ZERO_HASH, "time": 1700000300000,
             "delta": {"usdc": "-0.1", "coin": "BTC"}},
        ])
        exchange = make_exchange(info)

        rows = await exchange.fetch_performance_rows(since=1700000000, until=1700000400)

        self.assertEqual(len(rows), 3)
        self.assertEqual([row["symbol"] for row in rows], ["ETH", "ETH", "BTC"])
        self.assertEqual([row["funding"] for row in rows], [-0.25, 0.5, -0.1])
        self.assertEqual(len({row["id"] for row in rows}), 3)


class CashFlowMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_ledger_maps_withdraw_negative_and_converts_ms(self):
        info = FakeInfo(ledger=[
            {"hash": ZERO_HASH, "time": 1700000200000,
             "delta": {"type": "deposit", "usdc": "100.0"}},
            {"hash": ZERO_HASH, "time": 1700000300000,
             "delta": {"type": "withdraw", "usdc": "40.0"}},
            # ustd / amount fallbacks for non-USDC ledger entries.
            {"hash": ZERO_HASH, "time": 1700000350000,
             "delta": {"type": "deposit", "ustd": "7.5"}},
            {"hash": ZERO_HASH, "time": 1700000360000,
             "delta": {"type": "internalTransfer", "usdc": "5"}},
        ])
        exchange = make_exchange(info)

        rows = await exchange.fetch_cash_flows(since=1700000000, until=1700000400)

        self.assertEqual(rows[0]["kind"], "deposit")
        self.assertEqual(rows[0]["amount"], 100.0)
        self.assertEqual(rows[1]["kind"], "withdraw")
        self.assertEqual(rows[1]["amount"], -40.0)
        self.assertEqual(rows[2]["kind"], "deposit")
        self.assertEqual(rows[2]["amount"], 7.5)
        # internalTransfer is part of the default external-capital set.
        self.assertEqual(rows[3]["kind"], "internalTransfer")
        self.assertEqual(rows[3]["amount"], 5.0)
        self.assertEqual(len(rows), 4)
        # Zero-hash rows stay distinct because time/type/amount are included.
        self.assertEqual(len({row["id"] for row in rows}), 4)
        self.assertEqual(rows[0]["timestamp"], 1700000200.0)
        # Milliseconds are sent to the SDK even when the caller passes seconds.
        self.assertEqual(info.calls[0], ("ledger", 1700000000000, 1700000400000))

    async def test_cash_flow_types_can_override_the_default(self):
        info = FakeInfo(ledger=[
            {"hash": ZERO_HASH, "time": 1700000200000,
             "delta": {"type": "deposit", "usdc": "100.0"}},
            {"hash": ZERO_HASH, "time": 1700000360000,
             "delta": {"type": "internalTransfer", "usdc": "5"}},
        ])
        exchange = make_exchange(info)

        rows = await exchange.fetch_cash_flows(
            since=1700000000, until=1700000400, types=["internalTransfer"])

        self.assertEqual([row["kind"] for row in rows], ["internalTransfer"])

    async def test_send_inflows_are_counted_by_default(self):
        # HL reports plain transfers into the trading account as ``send``.
        info = FakeInfo(ledger=[
            {"hash": "0xs1", "time": 1700000200000,
             "delta": {"type": "send", "usdc": "9.99"}},
            {"hash": "0xs2", "time": 1700000300000,
             "delta": {"type": "send", "usdc": "48.0"}},
            {"hash": "0xa1", "time": 1700000400000,
             "delta": {"type": "accountClassTransfer", "usdc": "10"}},
            {"hash": "0xa2", "time": 1700000500000,
             "delta": {"type": "subAccountTransfer", "usdc": "10"}},
        ])
        exchange = make_exchange(info)

        rows = await exchange.fetch_cash_flows(since=1700000000, until=1700000600)

        self.assertEqual([row["kind"] for row in rows], ["send", "send"])
        self.assertEqual([row["amount"] for row in rows], [9.99, 48.0])
        self.assertEqual(len({row["id"] for row in rows}), 2)


class PaginationTests(unittest.TestCase):
    def test_window_is_bisected_and_rows_deduped(self):
        calls = []

        def fetch(start, end):
            calls.append((start, end))
            if (start, end) == (0, 120000):
                return [{"tid": index, "time": index} for index in range(4)]
            if start == 0:
                return [{"tid": 0, "time": 0}, {"tid": 1, "time": 1}]
            return [{"tid": 1, "time": 1}, {"tid": 2, "time": 2}]

        exchange = make_exchange()

        rows = exchange._paged(fetch, 0, 120000, page_size=4, min_window_ms=60000)

        self.assertEqual(calls, [(0, 120000), (0, 60000), (60000, 120000)])
        self.assertEqual([row["tid"] for row in rows], [0, 1, 2])


class OrderQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_order_maps_partial_fill(self):
        info = FakeInfo(orders={7: _order({
            "oid": 7, "coin": "BTC", "side": "B", "sz": "0.2",
            "origSz": "0.5", "limitPx": "100", "avgPx": "99.5",
        }, "open")})
        exchange = make_exchange(info)

        order = await exchange.fetch_order("7", "BTC")

        self.assertEqual(order, {"status": "open", "filled": 0.3, "remaining": 0.2,
                                 "average": 99.5, "price": 100.0})

    async def test_fetch_order_maps_filled_and_canceled(self):
        info = FakeInfo(orders={
            8: _order({"oid": 8, "coin": "BTC", "sz": "0", "origSz": "1",
                       "avgPx": "50"}, "filled"),
            9: _order({"oid": 9, "coin": "BTC", "sz": "1", "origSz": "1",
                       "limitPx": "48"}, "canceled"),
        })
        exchange = make_exchange(info)

        filled = await exchange.fetch_order(8)
        canceled = await exchange.fetch_order(9)

        self.assertEqual(filled["status"], "closed")
        self.assertEqual(filled["filled"], 1.0)
        self.assertEqual(filled["remaining"], 0.0)
        self.assertEqual(filled["average"], 50.0)
        self.assertEqual(canceled["status"], "canceled")

    async def test_unknown_oid_returns_none(self):
        info = FakeInfo(orders={123: {"status": "unknownOid"}})
        exchange = make_exchange(info)
        self.assertIsNone(await exchange.fetch_order(123))

    async def test_empty_status_reply_raises_instead_of_looking_missing(self):
        # A ``null`` body (throttled/errored request) is not an unknown order.
        exchange = make_exchange(FakeInfo(orders={}))
        with self.assertRaises(RuntimeError):
            await exchange.fetch_order(123)


class LookbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_lookback_walks_day_sized_chunks(self):
        info = FakeInfo()
        exchange = make_exchange(info)
        now = 1_700_000_000.0

        with patch("time.time", return_value=now):
            await exchange.fetch_performance_rows(lookback_days=2)

        fill_calls = [call for call in info.calls if call[0] == "fills"]
        self.assertEqual(len(fill_calls), 2)
        day_ms = 86400 * 1000
        self.assertEqual(fill_calls[0][1], int((now - 2 * 86400) * 1000))
        self.assertEqual(fill_calls[0][2], int((now - 86400) * 1000))
        self.assertEqual(fill_calls[-1][2], int(now * 1000))
        self.assertGreaterEqual(fill_calls[0][2] - fill_calls[0][1], 0)
        self.assertLessEqual(fill_calls[0][2] - fill_calls[0][1], day_ms)


if __name__ == "__main__":
    unittest.main()

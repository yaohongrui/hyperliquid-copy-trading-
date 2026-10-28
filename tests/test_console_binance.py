"""Console handling of leaders whose source is not Hyperliquid.

The console must never feed a Binance leader through the Hyperliquid REST
fallback, and a PATCH for a Binance leader must not persist or stream an
``address`` even when the payload carries one.
"""

import types
import unittest
from unittest.mock import patch

from aiohttp import web

from api.console import ConsoleService

HL_ADDRESS = "0x" + "ab" * 20
BAD_ADDRESS = "not-an-address"


class FakeRequest:
    def __init__(self, name, payload):
        self.match_info = {"name": name}
        self._payload = payload

    async def json(self):
        return self._payload


class RecordingEngine:
    def __init__(self, leaders):
        self.leaders = leaders
        self.previous_at = {}
        self.config_updates = []
        self.config_manager = types.SimpleNamespace(update_leader=self._config_update)
        self.processed = []
        self.process_calls = []

    def _config_update(self, name, changes):
        self.config_updates.append((name, dict(changes)))

    async def update_leader_config(self, name, changes):
        self.processed.append((name, dict(changes)))
        return False

    async def process(self, name, snapshot):
        self.process_calls.append((name, snapshot))


class RecordingStream:
    def __init__(self):
        self.calls = []

    async def set_addresses(self, addresses):
        self.calls.append(list(addresses))


class RecordingStore:
    def __init__(self):
        self.audits = []

    def audit(self, action, detail):
        self.audits.append((action, dict(detail)))


def make_service(engine, leader_stream=None):
    service = ConsoleService.__new__(ConsoleService)
    service.engine = engine
    service.leader_stream = leader_stream
    service.store = RecordingStore()
    service._last_rest = 0.0
    service._rest_interval = 60.0
    return service


class LeaderFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_leaders_are_never_fetched_by_the_hl_fallback(self):
        leaders = {
            "hl": {"source": "hyperliquid", "address": HL_ADDRESS, "enabled": True},
            "bn": {"source": "binance", "portfolio_id": "123", "enabled": True},
        }
        engine = RecordingEngine(leaders)
        service = make_service(engine)
        fetched = []
        snapshot = {"BTC": object()}

        async def fake_snapshot(cfg, session=None):
            fetched.append(cfg)
            return snapshot

        with patch("api.console.leader_snapshot", fake_snapshot):
            await service._refresh_leader_fallback()

        self.assertEqual(fetched, [leaders["hl"]])
        self.assertEqual(engine.process_calls, [("hl", snapshot)])

    async def test_disabled_hyperliquid_leaders_are_skipped(self):
        leaders = {
            "hl": {"source": "hyperliquid", "address": HL_ADDRESS, "enabled": False},
            "bn": {"source": "binance", "portfolio_id": "123", "enabled": True},
        }
        engine = RecordingEngine(leaders)
        service = make_service(engine)
        fetched = []

        async def fake_snapshot(cfg, session=None):
            fetched.append(cfg)
            return {}

        with patch("api.console.leader_snapshot", fake_snapshot):
            await service._refresh_leader_fallback()

        self.assertEqual(fetched, [])
        self.assertEqual(engine.process_calls, [])

    async def test_throttle_prevents_repeated_rest_calls(self):
        leaders = {"hl": {"source": "hyperliquid", "address": HL_ADDRESS}}
        engine = RecordingEngine(leaders)
        service = make_service(engine)
        fetched = []

        async def fake_snapshot(cfg, session=None):
            fetched.append(cfg)
            return {}

        with patch("api.console.leader_snapshot", fake_snapshot):
            await service._refresh_leader_fallback()
            await service._refresh_leader_fallback()

        self.assertEqual(len(fetched), 1)


class UpdateLeaderTests(unittest.IsolatedAsyncioTestCase):
    async def test_binance_patch_drops_address_and_skips_hl_paths(self):
        leaders = {"bn": {"source": "binance", "portfolio_id": "123", "coefficient": 1.0}}
        engine = RecordingEngine(leaders)
        stream = RecordingStream()
        service = make_service(engine, stream)
        request = FakeRequest("bn", {"enabled": True, "coefficient": 2.0, "address": ""})
        snapshot_calls = []

        async def fake_snapshot(cfg, session=None):
            snapshot_calls.append(cfg)
            return {}

        with patch("api.console.leader_snapshot", fake_snapshot):
            response = await service.update_leader(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(engine.config_updates, [("bn", {"enabled": True, "coefficient": 2.0})])
        self.assertEqual(engine.processed, [("bn", {"enabled": True, "coefficient": 2.0})])
        self.assertNotIn("address", engine.processed[0][1])
        self.assertEqual(stream.calls, [])
        self.assertEqual(snapshot_calls, [])
        self.assertEqual(service.store.audits, [("leader.update", {"leader": "bn", "fields": ["enabled", "coefficient"]})])

    async def test_binance_patch_with_only_address_is_rejected(self):
        leaders = {"bn": {"source": "binance", "portfolio_id": "123"}}
        engine = RecordingEngine(leaders)
        service = make_service(engine)
        request = FakeRequest("bn", {"address": "0xdeadbeef"})

        with self.assertRaises(web.HTTPBadRequest):
            await service.update_leader(request)

        self.assertEqual(engine.config_updates, [])
        self.assertEqual(engine.processed, [])

    async def test_hyperliquid_patch_rejects_a_bad_address(self):
        leaders = {"hl": {"source": "hyperliquid", "address": HL_ADDRESS}}
        engine = RecordingEngine(leaders)
        stream = RecordingStream()
        service = make_service(engine, stream)
        request = FakeRequest("hl", {"address": BAD_ADDRESS})

        with self.assertRaises(web.HTTPBadRequest):
            await service.update_leader(request)

        self.assertEqual(engine.config_updates, [])
        self.assertEqual(engine.processed, [])
        self.assertEqual(stream.calls, [])

    async def test_hyperliquid_patch_forwards_a_good_address_unchanged(self):
        leaders = {"hl": {"source": "hyperliquid", "address": HL_ADDRESS}}
        engine = RecordingEngine(leaders)
        service = make_service(engine, RecordingStream())
        request = FakeRequest("hl", {"address": HL_ADDRESS})
        snapshot_calls = []

        async def fake_snapshot(cfg, session=None):
            snapshot_calls.append(cfg)
            return {}

        with patch("api.console.leader_snapshot", fake_snapshot):
            response = await service.update_leader(request)

        self.assertEqual(response.status, 200)
        self.assertEqual(engine.processed, [("hl", {"address": HL_ADDRESS})])
        self.assertEqual(engine.config_updates, [("hl", {"address": HL_ADDRESS})])
        # update_leader_config returned False, so no reseed snapshot is needed.
        self.assertEqual(snapshot_calls, [])


if __name__ == "__main__":
    unittest.main()

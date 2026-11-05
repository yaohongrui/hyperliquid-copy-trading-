"""Console leader update and REST fallback behaviour."""

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
    async def test_disabled_hyperliquid_leaders_are_skipped(self):
        leaders = {
            "hl": {"source": "hyperliquid", "address": HL_ADDRESS, "enabled": False},
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

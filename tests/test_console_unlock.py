"""Console endpoint for clearing a hard-stop lock.

The web control is the third way to unlock (alongside Telegram and the
engine API), so it must rebaseline exactly like ``engine.unlock_hard_stop``
and must not report success when the equity lookup fails.
"""

import json
import tempfile
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from api.console import ConsoleService


class RecordingStore:
    def __init__(self):
        self.audits = []

    def audit(self, action, detail):
        self.audits.append((action, dict(detail)))


class FakeRisk:
    def __init__(self, locked=True):
        self.locked = locked


class FakeEngine:
    def __init__(self, locked=True, error=None):
        self.risk = FakeRisk(locked)
        self.paused = locked
        self.error = error
        self.unlocks = 0

    async def unlock_hard_stop(self):
        self.unlocks += 1
        if self.error:
            raise self.error
        was_locked = self.risk.locked
        self.risk.locked = False
        self.paused = False
        return {"was_locked": was_locked, "equity": 95.0, "baseline": 95.0}


class ConsoleUnlockTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def service(self, engine):
        service = ConsoleService(engine, {}, str(Path(self.directory.name) / "console.sqlite3"))
        service.store = RecordingStore()
        service._snapshot = {"status": {"paused": engine.paused, "risk_locked": engine.risk.locked}}
        return service

    async def request(self, engine, action):
        service = self.service(engine)
        app = web.Application()
        app.router.add_post("/api/v1/control/{action}", service.control)
        async with TestClient(TestServer(app)) as client:
            response = await client.post(f"/api/v1/control/{action}")
            raw = await response.text()
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw
            return response.status, body, service

    async def test_unlock_clears_the_lock_and_reports_the_new_baseline(self):
        engine = FakeEngine(locked=True)
        status, body, service = await self.request(engine, "unlock_hard_stop")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "paused": False, "was_locked": True,
                                "equity": 95.0, "baseline": 95.0})
        self.assertFalse(engine.risk.locked)
        self.assertFalse(engine.paused)
        self.assertEqual(service.store.audits, [
            ("control.unlock_hard_stop", {"was_locked": True, "equity": 95.0, "baseline": 95.0}),
        ])
        self.assertEqual(service._snapshot["status"], {"paused": False, "risk_locked": False})

    async def test_unlock_when_already_unlocked_still_resets_the_baseline(self):
        engine = FakeEngine(locked=False)
        status, body, _ = await self.request(engine, "unlock_hard_stop")

        self.assertEqual(status, 200)
        self.assertFalse(body["was_locked"])
        self.assertEqual(body["baseline"], 95.0)

    async def test_equity_lookup_failure_is_reported_not_swallowed(self):
        engine = FakeEngine(locked=True, error=RuntimeError("exchange unavailable"))
        status, body, service = await self.request(engine, "unlock_hard_stop")

        self.assertEqual(status, 500)
        self.assertEqual(body, "hard stop unlock failed")
        self.assertTrue(engine.risk.locked)
        self.assertEqual(service.store.audits, [])
        self.assertEqual(service._snapshot["status"], {"paused": True, "risk_locked": True})

    async def test_unknown_control_action_is_still_404(self):
        status, _, _ = await self.request(FakeEngine(), "flatten")
        self.assertEqual(status, 404)

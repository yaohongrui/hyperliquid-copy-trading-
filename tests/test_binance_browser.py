import unittest
import json
from unittest.mock import AsyncMock, MagicMock

from sources.binance.browser import BinanceAuthError, BinanceBrowserAuth, BrowserAuthConfig


class FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self, url):
        return self._cookies


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    def _auth_with_cookies(self, cookies):
        auth = BinanceBrowserAuth(BrowserAuthConfig())
        auth._context = FakeContext(cookies)
        auth._page = object()
        return auth

    def live_auth(self, payloads):
        auth = self._auth_with_cookies([{"name": "r20t", "value": "session"}])
        auth._live_headers = {"csrftoken": "test-token", "clienttype": "web"}
        auth._page = AsyncMock()
        auth._page.evaluate.side_effect = [
            {"status": status, "text": json.dumps(payload)}
            for status, payload in payloads
        ]
        return auth

    async def test_empty_positions_require_identity_and_visibility(self):
        auth = self.live_auth([
            (200, {"code": "000000", "data": []}),
            (200, {"code": "000000", "data": {"userId": "test"}}),
            (200, {"code": "000000", "data": {"positionShow": True}}),
        ])
        self.assertEqual(await auth.fetch_positions_live("leader"), [])
        self.assertEqual(auth._page.evaluate.await_count, 3)
        for call in auth._page.evaluate.call_args_list:
            self.assertEqual(call.args[1]["headers"], auth._live_headers)

    async def test_false_empty_is_rejected_when_identity_expired(self):
        auth = self.live_auth([
            (200, {"code": "000000", "data": []}),
            (401, {"code": "100002001", "data": None}),
        ])
        with self.assertRaises(BinanceAuthError):
            await auth.fetch_positions_live("leader")

    async def test_hidden_or_unknown_visibility_is_not_empty_position(self):
        for data in [{"positionShow": False}, {}, None]:
            auth = self.live_auth([
                (200, {"code": "000000", "data": []}),
                (200, {"code": "000000", "data": {"userId": "test"}}),
                (200, {"code": "000000", "data": data}),
            ])
            with self.assertRaises(BinanceAuthError):
                await auth.fetch_positions_live("leader")

    async def test_missing_headers_prevents_request(self):
        auth = self.live_auth([])
        auth._live_headers = {}
        with self.assertRaises(BinanceAuthError):
            await auth.fetch_positions_live("leader")
        auth._page.evaluate.assert_not_awaited()

    async def test_navigation_replaces_headers_only_after_private_identity(self):
        auth = self.live_auth([])
        response = MagicMock()
        response.json = AsyncMock(return_value={"code": "000000", "data": {"userId": "test"}})
        response.request.all_headers = AsyncMock(return_value={
            "csrftoken": "fresh", "clienttype": "web", "cookie": "never-export",
        })
        pending = MagicMock()
        async def resolved():
            return response
        pending.value = resolved()
        event = AsyncMock()
        event.__aenter__.return_value = pending
        auth._page.expect_response = MagicMock(return_value=event)
        auth._verify_live_identity = AsyncMock()
        await auth._navigate_live_session("https://www.binance.com/test")
        self.assertEqual(auth._live_headers, {"csrftoken": "fresh", "clienttype": "web"})
        auth._verify_live_identity.assert_awaited_once()

    async def test_navigation_failure_discards_stale_headers(self):
        auth = self.live_auth([])
        auth._page.expect_response = MagicMock(side_effect=TimeoutError)
        with self.assertRaises(BinanceAuthError):
            await auth._navigate_live_session("https://www.binance.com/test")
        self.assertEqual(auth._live_headers, {})

    async def test_device_and_waf_cookies_do_not_count_as_login(self):
        auth = self._auth_with_cookies([
            {"name": "bnc-uuid", "value": "device"},
            {"name": "aws-waf-token", "value": "waf"},
        ])

        self.assertFalse(await auth.is_logged_in())

    async def test_p20t_counts_as_login(self):
        auth = self._auth_with_cookies([{"name": "p20t", "value": "session"}])

        self.assertTrue(await auth.is_logged_in())

    async def test_empty_or_expired_login_cookie_does_not_count(self):
        auth = self._auth_with_cookies([
            {"name": "r20t", "value": "", "expires": 1999999999},
        ])
        self.assertFalse(await auth.is_logged_in())

        auth = self._auth_with_cookies([
            {"name": "r20t", "value": "session", "expires": 1},
        ])
        self.assertFalse(await auth.is_logged_in())

    def test_login_cookie_expiry_returns_persistent_p20t_expiry(self):
        expiry = BinanceBrowserAuth.login_cookie_expiry([
            {"name": "p20t", "value": "secret", "expires": 1780000000},
            {"name": "other", "value": "value", "expires": 1790000000},
        ])

        self.assertEqual(expiry, 1780000000)

    def test_login_cookie_expiry_ignores_session_cookie_and_missing_p20t(self):
        self.assertIsNone(BinanceBrowserAuth.login_cookie_expiry([
            {"name": "p20t", "value": "secret", "expires": -1},
        ]))
        self.assertIsNone(BinanceBrowserAuth.login_cookie_expiry([
            {"name": "csrftoken", "value": "secret", "expires": 1780000000},
        ]))


if __name__ == "__main__":
    unittest.main()

import asyncio
import unittest

from sources.binance.config import BinanceWebConfig
from sources.binance.poller import (
    _build_headers,
    cookie_expired,
    cookie_expiry_ms,
    missing_cookie_fields,
    PollAuthError,
    Poller,
)


class CookieValidationTests(unittest.TestCase):
    def test_cookie_expiry_is_parsed_and_detected(self):
        cookie = "foo=bar; BNC_FV_KEY_EXPIRE=1700000000000; theme=dark"

        self.assertEqual(cookie_expiry_ms(cookie), 1700000000000)
        self.assertTrue(cookie_expired(cookie, now_ms=1700000000001))

    def test_cookie_without_expiry_is_not_marked_expired(self):
        self.assertIsNone(cookie_expiry_ms("foo=bar"))
        self.assertFalse(cookie_expired("foo=bar", now_ms=1700000000001))

    def test_malformed_expiry_is_ignored(self):
        self.assertIsNone(cookie_expiry_ms("BNC_FV_KEY_EXPIRE=not-a-timestamp"))
        self.assertFalse(cookie_expired("BNC_FV_KEY_EXPIRE=not-a-timestamp", now_ms=1700000000001))

    def test_expired_fingerprint_cookie_is_rejected_before_request(self):
        from unittest.mock import AsyncMock
        poller = Poller(BinanceWebConfig(cookie="p20t=ok; BNC_FV_KEY_EXPIRE=1"))
        poller._session = AsyncMock()
        with self.assertRaises(PollAuthError):
            asyncio.run(poller.fetch_positions("leader-1"))
        poller._session.get.assert_not_called()

    def test_p20t_is_the_required_cookie_field(self):
        self.assertEqual(missing_cookie_fields("foo=bar"), ["p20t"])
        self.assertEqual(missing_cookie_fields("foo=bar; p20t=value"), [])

    def test_request_headers_fall_back_to_cookie_fingerprint_values(self):
        cfg = BinanceWebConfig(cookie="bnc-uuid=uuid-1; BNC_FV_KEY=fvideo-1")

        headers = _build_headers(cfg, "portfolio")

        self.assertEqual(headers["BNC-UUID"], "uuid-1")
        self.assertEqual(headers["FVIDEO-ID"], "fvideo-1")

    def test_unset_web_values_do_not_break_header_serialization(self):
        # An empty ${ENV} placeholder parses as None, which aiohttp cannot
        # serialize as a header value.
        cfg = BinanceWebConfig(cookie=None, csrf_token=None, device_info=None,
                               fvideo_token=None, user_agent=None)

        headers = _build_headers(cfg, "portfolio")

        self.assertTrue(all(isinstance(value, str) for value in headers.values()))
        self.assertEqual(headers["Cookie"], "")
        self.assertEqual(headers["User-Agent"], "Mozilla/5.0")


if __name__ == "__main__":
    unittest.main()

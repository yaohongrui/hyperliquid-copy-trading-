import unittest

from main import _hyperliquid_addresses


class HyperliquidAddressTests(unittest.TestCase):
    def test_only_enabled_leaders_and_integer_addresses(self):
        leaders = {
            "a": {"enabled": True, "address": 123},
            "b": {"enabled": False, "address": "0xbeef"},
            "c": {"enabled": True, "address": "0xcafe"},
        }
        self.assertEqual(_hyperliquid_addresses(leaders), ["0x7b", "0xcafe"])


if __name__ == "__main__":
    unittest.main()

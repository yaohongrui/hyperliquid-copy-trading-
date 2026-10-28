import unittest

from sources.binance.config import build_sources


class BuildSourcesTests(unittest.TestCase):
    def test_missing_section_is_empty(self):
        self.assertEqual(build_sources({}), {})

    def test_hybrid_defaults_and_typed_config(self):
        sources = build_sources({
            "position_sources": {
                "binance_live": {
                    "type": "BINANCE",
                    "browser": {"profile_dir": "data/profile", "headless": True},
                    "session": {"auth_probe_portfolio_id": "123"},
                }
            }
        })
        source = sources["binance_live"]
        self.assertEqual(source.mode, "hybrid")
        self.assertEqual(source.browser.profile_dir, "data/profile")
        self.assertTrue(source.browser.headless)
        self.assertEqual(source.session.auth_probe_portfolio_id, "123")
        self.assertEqual(source.web.cookie, "")
        self.assertEqual(source.web.user_agent, "Mozilla/5.0")

    def test_empty_web_mapping_uses_defaults(self):
        sources = build_sources({
            "position_sources": {
                "b": {"type": "binance", "mode": "hybrid", "web": {}}
            }
        })
        self.assertEqual(sources["b"].web.csrf_token, "")

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(ValueError):
            build_sources({"position_sources": {"b": {"type": "gate"}}})

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(TypeError):
            build_sources({
                "position_sources": {"b": {"type": "binance", "bogus": 1}}
            })


if __name__ == "__main__":
    unittest.main()

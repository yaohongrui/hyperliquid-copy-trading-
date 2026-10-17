import unittest

from core.symbols import SymbolMap, normalize


class NormalizeTests(unittest.TestCase):
    def test_reduces_namespace_quote_and_separator(self):
        self.assertEqual(normalize("XYZ:AMD"), "AMD")
        self.assertEqual(normalize("AMD/USDT:USDT"), "AMD")
        self.assertEqual(normalize(" amd_usdt "), "AMD")
        self.assertEqual(normalize("XYZ:AMD/USDT:USDT"), "AMD")
        self.assertEqual(normalize(None), "")


class SymbolMapTests(unittest.TestCase):
    def test_exact_match_precedes_normalized(self):
        mapping = SymbolMap({"XYZ:SKHX": "skip", "SKHX": "SKHX/USDT:USDT"})
        self.assertTrue(mapping.is_skipped("XYZ:SKHX"))
        self.assertTrue(mapping.is_skipped(" xyz:skhx "))
        self.assertFalse(mapping.is_skipped("SKHX"))
        self.assertEqual(mapping.target("SKHX"), "SKHX/USDT:USDT")
        self.assertIsNone(mapping.target("XYZ:SKHX"))

    def test_normalized_fallback(self):
        mapping = SymbolMap({"AMD": "AMD/USDT:USDT"})
        for symbol in ("XYZ:AMD", "AMD_USDT", "amd/usdt:usdt"):
            self.assertEqual(mapping.target(symbol), "AMD/USDT:USDT")

    def test_skip_aliases(self):
        for value in (None, "", "skip", "SKIP", " ignore ", "none", "null"):
            mapping = SymbolMap({"FOO": value})
            self.assertTrue(mapping.is_skipped("FOO"), value)
            self.assertIsNone(mapping.target("FOO"))
        self.assertFalse(SymbolMap({"FOO": "skip"}).is_skipped("BAR"))

    def test_skip_wins_normalized_collision_regardless_of_order(self):
        for entries in (
            [("AMD", "AMD/USDT:USDT"), ("XYZ:AMD", "skip")],
            [("XYZ:AMD", "skip"), ("AMD", "AMD/USDT:USDT")],
        ):
            mapping = SymbolMap(dict(entries))
            self.assertTrue(mapping.is_skipped("OTHER:AMD"))
            self.assertIsNone(mapping.target("OTHER:AMD"))
            self.assertEqual(mapping.target("AMD"), "AMD/USDT:USDT")
            self.assertFalse(mapping.is_skipped("AMD"))

    def test_display_returns_original_hl_key(self):
        mapping = SymbolMap({"XYZ:SKHX": "SKHX/USDT:USDT"})
        self.assertEqual(mapping.display("SKHX/USDT:USDT"), "XYZ:SKHX")
        self.assertEqual(mapping.display("SKHX_USDT"), "XYZ:SKHX")
        self.assertEqual(mapping.display("xyz:skhx"), "XYZ:SKHX")
        self.assertEqual(mapping.display("FOO/USDT:USDT"), "FOO")
        self.assertEqual(mapping.display(None), "")

    def test_display_duplicate_normalized_targets_first_wins(self):
        mapping = SymbolMap({"XYZ:AMD": "AMD/USDT:USDT", "AMD": "AMD/USDT:USDT"})
        self.assertEqual(mapping.display("AMD/USDT:USDT"), "XYZ:AMD")
        self.assertEqual(mapping.display("XYZ:AMD"), "XYZ:AMD")
        self.assertEqual(mapping.display("AMD"), "AMD")

    def test_non_mapping_config_is_rejected(self):
        self.assertEqual(SymbolMap(None).entries(), {})
        for value in ([], "bad", 1):
            with self.assertRaises(ValueError):
                SymbolMap(value)

    def test_entries_excludes_skips_and_uses_canonical_keys(self):
        mapping = SymbolMap({"amd": "AMD/USDT:USDT", "XYZ:SKHX": "skip"})
        self.assertEqual(mapping.entries(), {"AMD": "AMD/USDT:USDT"})


if __name__ == "__main__":
    unittest.main()

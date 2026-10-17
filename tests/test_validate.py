import unittest

from validate import _validate_symbol_map


class ValidateSymbolMapTests(unittest.TestCase):
    def test_absent_or_empty_is_valid(self):
        _validate_symbol_map(None)
        _validate_symbol_map({})

    def test_valid_forms(self):
        _validate_symbol_map({
            "AMD": "AMD/USDT:USDT",
            "SKHX": None,
            "FOO": "skip",
            "BAR": "BAR_USDT",
        })

    def test_non_mapping_is_rejected(self):
        for value in ([], "bad", 1):
            with self.assertRaises(AssertionError):
                _validate_symbol_map(value)

    def test_invalid_keys_are_rejected(self):
        for key in (1, "", "   ", ":"):
            with self.assertRaises(AssertionError):
                _validate_symbol_map({key: "AMD"})

    def test_invalid_values_are_rejected(self):
        for value in (1, [], {}):
            with self.assertRaises(AssertionError):
                _validate_symbol_map({"AMD": value})


if __name__ == "__main__":
    unittest.main()

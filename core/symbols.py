"""Bridge Hyperliquid coin names to execution-venue instruments.

Hyperliquid reports each coin under its own name (``SKHX``, or a namespaced
HIP-3 form such as ``XYZ:SKHX``), while Gate and Binance may list the same
underlying under a different name.  ``symbol_map`` in config.yaml bridges the
two: keys are Hyperliquid names, values are the venue instrument, written either
as a CCXT unified symbol (``AMD/USDT:USDT``), a bare base asset (``AMD``) or a
native venue name (``AMD_USDT``).

A value of ``skip`` (or null/empty) marks a symbol as untradable, so the engine
ignores all of its signals instead of surfacing an order failure on every event.
"""

SKIP_VALUES = {"", "skip", "ignore", "none", "null"}

_SKIP = object()
_MISSING = object()


def normalize(name):
    """Reduce a symbol to its base asset.

    ``XYZ:AMD``, ``AMD/USDT:USDT``, ``amd`` and ``AMD_USDT`` all normalize to
    ``AMD``, so mapping keys survive namespaces, quotes and casing.
    """
    text = str(name or "").strip().upper().replace("_", "/")
    return text.split("/")[0].split(":")[-1]


def _key(name):
    """Canonical exact-match key: trimmed and upper-cased, namespace preserved."""
    return str(name or "").strip().upper()


def _target(value):
    if value is None:
        return _SKIP
    text = str(value).strip()
    return _SKIP if text.lower() in SKIP_VALUES else text


class SymbolMap:
    def __init__(self, mapping=None):
        if mapping is None:
            mapping = {}
        if not isinstance(mapping, dict):
            raise ValueError("symbol_map must be a mapping of Hyperliquid symbol to venue instrument")
        self.mapping = {str(key): value for key, value in mapping.items()}
        self._exact = {}
        self._normalized = {}
        self._names = {}
        for key, value in self.mapping.items():
            name = _key(key)
            base = normalize(name)
            if not name or not base:
                continue
            target = _target(value)
            self._exact[name] = target
            if target is _SKIP:
                # A skip entry always wins the base fallback, regardless of order.
                self._normalized[base] = _SKIP
            else:
                self._normalized.setdefault(base, target)
                reverse = normalize(target)
                if reverse:
                    self._names.setdefault(reverse, name)

    def __bool__(self):
        return bool(self._exact)

    def _resolve(self, symbol):
        key = _key(symbol)
        if key in self._exact:
            return self._exact[key]
        return self._normalized.get(normalize(symbol), _MISSING)

    def is_skipped(self, symbol):
        return self._resolve(symbol) is _SKIP

    def target(self, symbol):
        """Venue instrument for a Hyperliquid symbol, or None when unmapped/skipped."""
        value = self._resolve(symbol)
        return value if isinstance(value, str) else None

    def display(self, symbol):
        """Show a venue instrument under its Hyperliquid name when mapped."""
        key = _key(symbol)
        if key in self._exact:
            return key
        base = normalize(symbol)
        return self._names.get(base, base)

    def entries(self):
        """Hyperliquid name -> venue instrument pairs that must resolve."""
        return {key: value for key, value in self._exact.items() if value is not _SKIP}

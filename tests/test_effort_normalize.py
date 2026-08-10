from __future__ import annotations

import unittest

from agentlog.normalize.effort import normalize_effort


class EffortNormalizeTests(unittest.TestCase):
    def test_identity_canonical_values(self) -> None:
        for raw in ("low", "medium", "high", "xhigh", "ultra", "max"):
            canonical, source = normalize_effort(raw)
            self.assertEqual(canonical, raw)
            self.assertEqual(source, raw)

    def test_aliases(self) -> None:
        self.assertEqual(normalize_effort("med"), ("medium", "med"))
        self.assertEqual(normalize_effort("x-high"), ("xhigh", "x-high"))
        self.assertEqual(normalize_effort("HIGH"), ("high", "HIGH"))

    def test_unknown_retains_source(self) -> None:
        self.assertEqual(normalize_effort("ludicrous"), ("unknown", "ludicrous"))

    def test_empty_is_none(self) -> None:
        self.assertEqual(normalize_effort(None), (None, None))
        self.assertEqual(normalize_effort(""), (None, None))
        self.assertEqual(normalize_effort("  "), (None, None))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agentlog.pricing import estimate_cost, load_pricing


class PricingTests(unittest.TestCase):
    def test_empty_table_keeps_cost_unavailable(self) -> None:
        table = load_pricing()
        self.assertEqual(table.version, "0")
        self.assertEqual(table.models, {})
        result = estimate_cost(
            model="claude-opus-4-6",
            input_tokens=1000,
            output_tokens=500,
            pricing=table,
        )
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["usd"])

    def test_configured_model_estimates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pricing.toml"
            path.write_text(
                """
version = "1"
as_of = "2026-08-09"

[[models]]
id = "demo-model"
input_per_mtok = 1.0
output_per_mtok = 2.0
cache_read_per_mtok = 0.1
""",
                encoding="utf-8",
            )
            table = load_pricing(path)
            result = estimate_cost(
                model="demo-model",
                input_tokens=1_000_000,
                output_tokens=500_000,
                cache_read_input_tokens=1_000_000,
                pricing=table,
            )
            self.assertEqual(result["status"], "estimated")
            self.assertAlmostEqual(result["usd"], 1.0 + 1.0 + 0.1)


if __name__ == "__main__":
    unittest.main()

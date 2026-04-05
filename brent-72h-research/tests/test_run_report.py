import copy
import json
import statistics
import unittest
from pathlib import Path

from scripts.fetch_public_data import validate_snapshot
from scripts.run_report import Engine


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_PATH = REPO_ROOT / "assets" / "market_snapshot.template.json"


def load_template_payload():
    return json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))


class BrentRunReportTests(unittest.TestCase):
    def test_sparse_chain_does_not_trigger_black76_mode(self):
        payload = load_template_payload()
        payload["chain"] = {
            "source_name": "Sparse public chain",
            "source_url": "https://example.com/sparse-chain",
            "options": [
                {
                    "strike": 73.0,
                    "option_type": "call",
                    "expiry": "2026-05-29",
                    "price": 3.1,
                },
                {
                    "strike": 73.0,
                    "option_type": "put",
                    "expiry": "2026-05-29",
                    "price": 2.9,
                },
            ],
        }

        engine = Engine(payload)

        self.assertEqual(engine.mode, "BSM 代理模式")

    def test_validate_snapshot_rejects_chain_without_strike_level_coverage(self):
        payload = load_template_payload()
        payload.pop("proxy_option_surface", None)
        payload["chain"] = {
            "source_name": "Sparse public chain",
            "source_url": "https://example.com/sparse-chain",
            "options": [
                {
                    "strike": 73.0,
                    "option_type": "call",
                    "expiry": "2026-05-29",
                    "price": 3.1,
                },
                {
                    "strike": 73.0,
                    "option_type": "put",
                    "expiry": "2026-05-29",
                    "price": 2.9,
                },
            ],
        }

        errors, _warnings = validate_snapshot(payload)

        self.assertTrue(
            any("Mode A" in item or "coverage" in item for item in errors),
            errors,
        )

    def test_simulated_distribution_tracks_inferred_median_target(self):
        payload = load_template_payload()
        payload["forecast"]["median_72h_target"] = 79.2
        engine = Engine(payload)

        prices = engine.simulate_prices(engine.compute_atm_iv())

        self.assertAlmostEqual(
            statistics.median(prices),
            payload["forecast"]["median_72h_target"],
            delta=0.5,
        )

    def test_report_uses_chinese_market_sentiment_and_dynamic_option_scenario(self):
        payload = load_template_payload()

        report = Engine(copy.deepcopy(payload)).compute_report()

        self.assertNotIn("fairly priced", report)
        self.assertNotIn("panic-priced", report)
        self.assertNotIn("complacent", report)
        self.assertIn("市场情绪：", report)
        self.assertIn("76.00", report)


if __name__ == "__main__":
    unittest.main()

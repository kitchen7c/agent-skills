import unittest

from scripts.fetch_public_data import validate_snapshot


def make_snapshot() -> dict:
    return {
        "trade_date": "2026-03-31",
        "as_of_utc": "2026-03-31T08:00:00Z",
        "market": {
            "contracts": {
                key: {
                    "label": f"Brent {key.upper()}",
                    "symbol": f"BRN_{key.upper()}",
                    "used_price": 73.0,
                    "used_source_name": "Source A",
                    "used_source_url": "https://example.com/source",
                    "used_timestamp_utc": "2026-03-31T07:30:00Z",
                    "used_reference_type": "last_close",
                    "cross_checks": [
                        {
                            "source_name": "Source A",
                            "source_url": "https://example.com/a",
                            "timestamp_utc": "2026-03-31T07:30:00Z",
                            "price": 73.0,
                            "reference_type": "last_close",
                        },
                        {
                            "source_name": "Source B",
                            "source_url": "https://example.com/b",
                            "timestamp_utc": "2026-03-31T07:31:00Z",
                            "price": 73.02,
                            "reference_type": "last_close",
                        },
                    ],
                }
                for key in ("m2", "m3", "m4")
            },
            "ovx": {
                "level": 30.0,
                "timestamp_utc": "2026-03-31T07:30:00Z",
                "source_name": "OVX Source",
                "source_url": "https://example.com/ovx",
                "proxy": True,
            },
            "positioning": {
                "value_text": "proxy",
                "timestamp_utc": "2026-03-30T00:00:00Z",
                "source_name": "Positioning Source",
                "source_url": "https://example.com/pos",
                "proxy": True,
            },
        },
        "history": {
            "m2_daily_closes": [
                {"date": f"2026-03-{day:02d}", "close": 70.0 + day * 0.1}
                for day in range(1, 22)
            ]
        },
        "forecast": {
            "direction": "偏多",
            "median_72h_target": 74.0,
        },
        "strategies": {
            "futures": {"type": "outright"},
            "options": {"name": "call spread"},
        },
        "proxy_option_surface": {
            "atm_iv": 0.3,
            "skew_proxy": {"call25_iv": 0.28, "put25_iv": 0.32},
        },
    }


class ValidateSnapshotSourceTests(unittest.TestCase):
    def test_placeholder_urls_warn_by_default(self) -> None:
        errors, warnings = validate_snapshot(make_snapshot())

        self.assertEqual(errors, [])
        self.assertTrue(any("占位" in item for item in warnings))

    def test_placeholder_urls_fail_in_strict_mode(self) -> None:
        errors, warnings = validate_snapshot(make_snapshot(), strict_sources=True)

        self.assertEqual(warnings, [])
        self.assertTrue(any("占位" in item for item in errors))


if __name__ == "__main__":
    unittest.main()

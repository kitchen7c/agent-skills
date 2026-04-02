import unittest

from scripts.hnxcl import (
    format_price_html,
    normalize_price_display,
    normalize_report_data,
    normalize_status_display,
)


class ReportNormalizationTests(unittest.TestCase):
    def test_keeps_flat_status_visible(self):
        self.assertEqual(normalize_status_display("持平"), "持平")

    def test_backfills_fob_status_from_chart_items(self):
        report = normalize_report_data(
            {
                "price_strip": [
                    {"label": "FOB 新加坡 (ABX 1)", "value": "445", "status": "-"},
                    {"label": "FOB 韩国 (ABX 2)", "value": "455", "status": "-"},
                    {"label": "FOB 伊朗 (散装)", "value": "385", "status": "-"},
                ],
                "chart": {
                    "items": [
                        {"label": "新加坡 ABX 1", "previous": 440, "current": 445},
                        {"label": "韩国离岸价", "previous": 460, "current": 455},
                        {"label": "伊朗散装价", "previous": 385, "current": 385},
                    ]
                },
            },
            "2026年04月02日",
        )

        self.assertEqual(report["price_strip"][0]["status"], "▲ 5")
        self.assertEqual(report["price_strip"][1]["status"], "▼ 5")
        self.assertEqual(report["price_strip"][2]["status"], "持平")

    def test_backfills_main_contract_status_from_existing_market_deltas(self):
        report = normalize_report_data(
            {
                "main_price": "3320",
                "main_price_status": "-",
                "price_strip": [
                    {"label": "FOB 新加坡 (ABX 1)", "value": "445", "status": "-"},
                    {"label": "FOB 韩国 (ABX 2)", "value": "455", "status": "-"},
                    {"label": "FOB 伊朗 (散装)", "value": "385", "status": "-"},
                ],
                "chart": {
                    "items": [
                        {"label": "新加坡 ABX 1", "previous": 440, "current": 445},
                        {"label": "韩国离岸价", "previous": 460, "current": 455},
                        {"label": "伊朗散装价", "previous": 385, "current": 385},
                    ]
                },
            },
            "2026年04月02日",
        )

        self.assertEqual(report["main_price_status"], "▲ 5")

    def test_formats_usd_prices_with_prefix_and_unit(self):
        self.assertEqual(
            normalize_price_display("445", label="FOB 新加坡 (ABX 1)"),
            "$445/吨",
        )

    def test_formats_cny_prices_with_prefix_and_unit(self):
        self.assertEqual(
            normalize_price_display("3320", label="上海主力合约 (BU主力)"),
            "¥3320/吨",
        )

    def test_formats_price_ranges_with_currency_and_unit(self):
        self.assertEqual(
            normalize_price_display("440-445", label="FOB 韩国 (ABX 2)"),
            "$440-445/吨",
        )

    def test_formats_usd_status_with_spacing_and_currency(self):
        self.assertEqual(
            normalize_status_display("上涨15", label="FOB 新加坡 (ABX 1)"),
            "▲ $15",
        )

    def test_formats_cny_status_with_spacing_and_currency(self):
        self.assertEqual(
            normalize_status_display("-30|-0.64%", label="上海主力合约 (BU主力)"),
            "▼ ¥30",
        )

    def test_keeps_flat_status_without_currency_symbol(self):
        self.assertEqual(
            normalize_status_display("持平", label="FOB 韩国 (ABX 2)"),
            "持平",
        )

    def test_wraps_unit_text_for_price_strip_values(self):
        self.assertEqual(
            format_price_html("$685/吨"),
            '$685<span class="unit-text">/吨</span>',
        )

    def test_wraps_unit_text_for_main_price_value(self):
        self.assertEqual(
            format_price_html("¥4284/吨"),
            '¥4284<span class="unit-text">/吨</span>',
        )


if __name__ == "__main__":
    unittest.main()

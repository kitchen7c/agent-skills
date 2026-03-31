import tempfile
import unittest
from pathlib import Path

from scripts.hnxcl import (
    build_embedded_font_css,
    extract_json_payload,
    font_source_to_css_url,
    inject_pdf_font_styles,
    prepare_output_path,
    render_report_template,
    resolve_chinese_font_paths,
)


class HnxclFontTests(unittest.TestCase):
    def test_build_embedded_font_css_includes_font_faces_and_family_stack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            regular = Path(tmpdir) / "NotoSansSC-Regular.ttf"
            bold = Path(tmpdir) / "NotoSansSC-Bold.ttf"
            regular.write_bytes(b"regular-font-bytes")
            bold.write_bytes(b"bold-font-bytes")

            css = build_embedded_font_css(
                {"regular": regular.resolve().as_uri(), "bold": bold.resolve().as_uri()}
            )

        self.assertIn("@font-face", css)
        self.assertIn("Noto Sans SC Embedded", css)
        self.assertIn("data:font/ttf;base64,", css)
        self.assertIn("Noto Sans SC", css)
        self.assertIn("*, *::before, *::after", css)
        self.assertIn("!important", css)

    def test_font_source_to_css_url_embeds_local_file_as_data_uri(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            font_path = Path(tmpdir) / "NotoSansSC-Regular.ttf"
            font_path.write_bytes(b"regular-font-bytes")

            css_url = font_source_to_css_url(font_path.resolve().as_uri())

        self.assertTrue(css_url.startswith("data:font/ttf;base64,"))
        self.assertNotIn("file://", css_url)

    def test_inject_pdf_font_styles_inserts_style_before_head_close(self):
        html = "<html><head><title>x</title></head><body>中文</body></html>"

        result = inject_pdf_font_styles(html, "body { font-family: Test; }")

        self.assertIn("body { font-family: Test; }", result)
        self.assertLess(result.index("body { font-family: Test; }"), result.index("</head>"))

    def test_resolve_chinese_font_paths_prefers_bundled_assets(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fonts_dir = Path(tmpdir) / "fonts"
            fonts_dir.mkdir()
            regular = fonts_dir / "NotoSansSC-Regular.ttf"
            bold = fonts_dir / "NotoSansSC-Bold.ttf"
            regular.write_bytes(b"regular")
            bold.write_bytes(b"bold")

            resolved = resolve_chinese_font_paths(fonts_dir)

            self.assertEqual(resolved["regular"], regular.resolve().as_uri())
            self.assertEqual(resolved["bold"], bold.resolve().as_uri())

    def test_prepare_output_path_removes_existing_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "existing.html"
            target.write_text("old", encoding="utf-8")

            prepared = prepare_output_path(target)

            self.assertEqual(prepared, target)
            self.assertFalse(target.exists())

    def test_prepare_output_path_creates_missing_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "nested" / "report.pdf"

            prepared = prepare_output_path(target)

            self.assertEqual(prepared, target)
            self.assertTrue(target.parent.exists())

    def test_extract_json_payload_parses_markdown_fenced_json(self):
        payload = extract_json_payload(
            """```json
            {"report_date": "2026年03月31日", "market_summary": ["测试"]}
            ```"""
        )

        self.assertEqual(payload["report_date"], "2026年03月31日")
        self.assertEqual(payload["market_summary"], ["测试"])

    def test_render_report_template_replaces_dynamic_tokens(self):
        template = """
        <div>{{REPORT_DATE}}</div>
        <div>{{PRICE_STRIP_ITEMS}}</div>
        <div>{{MARKET_SUMMARY_HTML}}</div>
        """
        report_data = {
            "report_date": "2026年03月31日",
            "price_strip": [{"label": "华东", "value": "3700", "status": "上涨 20"}],
            "market_summary": ["**强势** 运行"],
        }

        rendered = render_report_template(template, report_data)

        self.assertIn("2026年03月31日", rendered)
        self.assertIn("华东", rendered)
        self.assertIn("3700", rendered)
        self.assertIn("<strong>强势</strong>", rendered)


if __name__ == "__main__":
    unittest.main()

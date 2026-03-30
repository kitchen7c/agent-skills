import tempfile
import unittest
from pathlib import Path

from scripts.hnxcl import (
    build_embedded_font_css,
    inject_pdf_font_styles,
    prepare_output_path,
    resolve_chinese_font_paths,
)


class HnxclFontTests(unittest.TestCase):
    def test_build_embedded_font_css_includes_font_faces_and_family_stack(self):
        css = build_embedded_font_css(
            {
                "regular": "file:///tmp/NotoSansSC-Regular.ttf",
                "bold": "file:///tmp/NotoSansSC-Bold.ttf",
            }
        )

        self.assertIn("@font-face", css)
        self.assertIn("Noto Sans SC Embedded", css)
        self.assertIn("file:///tmp/NotoSansSC-Regular.ttf", css)
        self.assertIn("file:///tmp/NotoSansSC-Bold.ttf", css)
        self.assertIn("Noto Sans SC", css)

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


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from scripts.hnxcl import (
    ArgusStageError,
    article_text_looks_like_target_report,
    build_source_metadata,
    determine_exit_code,
    build_embedded_font_css,
    build_generated_report_stem,
    compute_single_page_pdf_size,
    extract_news_candidates_from_source_text,
    extract_report_date_from_filename,
    extract_json_payload,
    filename_matches_target_date,
    font_source_to_css_url,
    inject_pdf_font_styles,
    is_current_report_file,
    normalize_forecast_reason,
    normalize_status_display,
    prepare_output_path,
    render_report_template,
    resolve_chinese_font_paths,
    score_publication_candidate,
    select_publication_candidate,
)


class HnxclFontTests(unittest.TestCase):
    def test_uv_project_metadata_exists_for_pinned_runtime(self):
        project_root = Path(__file__).resolve().parent

        self.assertTrue((project_root / "pyproject.toml").exists())
        self.assertTrue((project_root / ".python-version").exists())

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

    def test_filename_matches_target_date_supports_common_formats(self):
        self.assertTrue(
            filename_matches_target_date(
                "Argus Asia Bitumen Daily 20260331.pdf", "20260331"
            )
        )
        self.assertTrue(
            filename_matches_target_date(
                "Argus-Asia-Bitumen-Daily-2026-03-31.pdf", "20260331"
            )
        )
        self.assertTrue(
            filename_matches_target_date(
                "Argus Asia Bitumen Daily 31-Mar-2026.pdf", "20260331"
            )
        )
        self.assertFalse(
            filename_matches_target_date(
                "Argus Asia Bitumen Daily 20260330.pdf", "20260331"
            )
        )

    def test_is_current_report_file_requires_existing_matching_pdf(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "Argus Asia Bitumen Daily 20260331_zh.pdf"
            target.write_bytes(b"pdf")

            self.assertTrue(is_current_report_file(target, "20260331"))
            self.assertFalse(is_current_report_file(target, "20260330"))
            self.assertFalse(is_current_report_file(target.with_name("other.pdf"), "20260331"))

    def test_compute_single_page_pdf_size_adds_padding_and_px_units(self):
        width, height = compute_single_page_pdf_size(900, 2200)

        self.assertEqual(width, "980px")
        self.assertEqual(height, "2280px")

    def test_extract_report_date_from_filename_supports_common_formats(self):
        self.assertEqual(
            extract_report_date_from_filename("Argus Asia Bitumen Daily 20260331.pdf"),
            "2026年03月31日",
        )
        self.assertEqual(
            extract_report_date_from_filename("Argus-Asia-Bitumen-Daily-2026-03-31.pdf"),
            "2026年03月31日",
        )
        self.assertEqual(
            extract_report_date_from_filename("Argus Asia Bitumen Daily 31-Mar-2026.pdf"),
            "2026年03月31日",
        )
        self.assertEqual(extract_report_date_from_filename("Argus Asia Bitumen Daily.pdf"), "-")

    def test_build_generated_report_stem_uses_current_date(self):
        self.assertEqual(
            build_generated_report_stem("2026年03月31日"),
            "Argus_Asia_Bitumen_Daily_20260331",
        )

    def test_select_publication_candidate_prefers_target_date_match(self):
        candidates = [
            {"text": "Argus Asia Bitumen Daily PDF 20260330"},
            {"text": "Argus Asia Bitumen Daily PDF 20260331"},
            {"text": "Argus Asia Bitumen Daily PDF 20260329"},
        ]

        selected = select_publication_candidate(candidates, "20260331")

        self.assertEqual(selected["text"], "Argus Asia Bitumen Daily PDF 20260331")

    def test_select_publication_candidate_falls_back_to_latest_candidate(self):
        candidates = [
            {"text": "Argus Asia Bitumen Daily PDF 20260329"},
            {"text": "Argus Asia Bitumen Daily PDF 20260330"},
        ]

        selected = select_publication_candidate(candidates, "20260331")

        self.assertEqual(selected["text"], "Argus Asia Bitumen Daily PDF 20260330")

    def test_score_publication_candidate_supports_semantic_match(self):
        score = score_publication_candidate(
            {"text": "Asia bitumen daily report download center 20260331"},
            "20260331",
        )

        self.assertGreater(score, 0)

    def test_score_publication_candidate_prefers_exact_report_title_over_generic_news(self):
        target = score_publication_candidate(
            {"text": "Asia bitumen daily: Singapore and S Korea prices rise 31 Mar 26"},
            "20260331",
        )
        generic = score_publication_candidate(
            {"text": "US-Iran war: Latest news 01 Apr 26"},
            "20260331",
        )

        self.assertGreater(target, generic)

    def test_normalize_status_display_converts_text_direction_to_arrow(self):
        self.assertEqual(normalize_status_display("上涨"), "▲")
        self.assertEqual(normalize_status_display("下跌"), "▼")
        self.assertEqual(normalize_status_display("持平"), "-")

    def test_normalize_status_display_extracts_absolute_change_from_mixed_text(self):
        self.assertEqual(normalize_status_display("100|2.13%"), "▲100")
        self.assertEqual(normalize_status_display("-11|-0.24%"), "▼11")
        self.assertEqual(normalize_status_display("上涨 25"), "▲25")

    def test_normalize_forecast_reason_expands_numeric_support_and_resistance(self):
        self.assertIn(
            "买盘承接",
            normalize_forecast_reason("630", "新加坡 (ABX 1)", "support"),
        )
        self.assertIn(
            "上方压力",
            normalize_forecast_reason("660", "新加坡 (ABX 1)", "resistance"),
        )

    def test_extract_news_candidates_from_source_text_prefers_news_like_sentences(self):
        source_text = """
        Argus will close assessments early on 2 April because of the Singapore public holiday.
        Thailand said it reached an agreement with Iran to allow more tankers through the Strait of Hormuz.
        Iran approved 20 Pakistan-flagged vessels to transit the waterway.
        Singapore bitumen prices were unchanged amid weak buying interest.
        """

        items = extract_news_candidates_from_source_text(source_text, limit=3)

        self.assertEqual(len(items), 3)
        self.assertTrue(any("public holiday" in item.lower() for item in items))

    def test_article_text_looks_like_target_report_rejects_generic_news_page(self):
        generic_news = """
        News & analysis
        US-Iran war: Latest news
        Freight – Clean – Asia: Market comment
        Asia bitumen daily: Singapore and S Korea prices rise
        """

        self.assertFalse(article_text_looks_like_target_report(generic_news))

    def test_article_text_looks_like_target_report_accepts_bitumen_daily_body(self):
        report_text = """
        Asia bitumen daily: Singapore and S Korea prices rise
        Singapore bitumen prices rose as offers increased and buying interest remained thin.
        South Korea prices also firmed on tighter prompt availability.
        """

        self.assertTrue(article_text_looks_like_target_report(report_text))

    def test_build_source_metadata_marks_article_fallback_reason(self):
        metadata = build_source_metadata(
            source_type="argus_direct_article_fallback",
            source_label="Argus Asia Bitumen Daily",
            source_report_date="2026年03月31日",
            source_name="Argus Direct Article",
            fallback_reason="Publications selector timeout",
        )

        self.assertIn("文章回退", metadata["argus_source_date_note"])
        self.assertIn("Publications selector timeout", metadata["argus_source_date_note"])
        self.assertEqual(metadata["argus_source_file"], "Argus Direct Article")

    def test_determine_exit_code_uses_stage_specific_mapping(self):
        self.assertEqual(
            determine_exit_code(ArgusStageError("login", "bad credentials")),
            2,
        )
        self.assertEqual(
            determine_exit_code(ArgusStageError("report_generation", "llm failed")),
            6,
        )

    def test_determine_exit_code_falls_back_for_unknown_errors(self):
        self.assertEqual(determine_exit_code(RuntimeError("boom")), 1)


if __name__ == "__main__":
    unittest.main()

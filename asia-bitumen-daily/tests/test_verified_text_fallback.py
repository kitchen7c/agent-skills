import tempfile
import unittest
from pathlib import Path

from scripts import hnxcl


class VerifiedTextFallbackTests(unittest.TestCase):
    def test_argus_pdf_source_note_hides_technical_fallback_reason(self):
        metadata = hnxcl.build_source_metadata(
            source_type="argus_pdf",
            source_label="Argus PDF",
            source_report_date="2026年04月17日",
            source_name="Argus Asia Bitumen Daily  (2026-04-17).pdf",
            fallback_reason="legacy publication UI unavailable: pdf button not found; Script error for: requireConfig",
        )

        self.assertIn("来源: Argus PDF", metadata["argus_source_date_note"])
        self.assertIn("引用日期: 2026年04月17日", metadata["argus_source_date_note"])
        self.assertNotIn("回退原因:", metadata["argus_source_date_note"])

    def test_loads_explicit_verified_text_fallback_with_source_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            verified_path = Path(tmpdir) / "verified-20260414.txt"
            verified_path.write_text("Verified Argus body text", encoding="utf-8")

            result = hnxcl.load_verified_text_fallback(
                verified_text_path=verified_path,
                verified_report_date="2026-04-14",
                fallback_reason="今日自动抓取仍卡在 Publications/iframe，显式沿用最新已核验正文",
            )

            self.assertEqual(result.source_type, "verified_text_fallback")
            self.assertEqual(result.source_report_date, "2026年04月14日")
            self.assertEqual(result.source_name, verified_path.name)
            self.assertEqual(result.text_content, "Verified Argus body text")
            self.assertIn("显式沿用最新已核验正文", result.fallback_reason)

    def test_builds_verified_fallback_source_note(self):
        metadata = hnxcl.build_source_metadata(
            source_type="verified_text_fallback",
            source_label="Argus Verified Text",
            source_report_date="2026年04月14日",
            source_name="verified-20260414.txt",
            fallback_reason="显式沿用已核验正文",
        )

        self.assertIn("来源: 已核验正文回退", metadata["argus_source_date_note"])
        self.assertIn("引用日期: 2026年04月14日", metadata["argus_source_date_note"])
        self.assertIn("回退原因: 显式沿用已核验正文", metadata["argus_source_date_note"])

    def test_verified_fallback_prompt_context_marks_text_as_historical(self):
        fetch_result = hnxcl.FetchResult(
            source_type="verified_text_fallback",
            source_name="verified-20260414.txt",
            source_report_date="2026年04月14日",
            text_content="Verified Argus body text",
            fallback_reason="显式沿用已核验正文",
        )

        source_kind_prompt, source_usage_prompt = hnxcl.build_source_prompt_context(fetch_result)

        self.assertIn("已核验 Argus 正文文本", source_kind_prompt)
        self.assertIn("不要将其表述为今日新一期已确认内容", source_usage_prompt)
        self.assertIn("2026年04月14日", source_usage_prompt)

    def test_get_asia_bitumen_daily_raises_stage_error_when_all_fetch_paths_fail(self):
        downloader = hnxcl.ArgusDownloader()
        downloader.download_publication_pdf = lambda target_date: None
        downloader.fetch_article_fallback = lambda target_date, fallback_reason: None
        downloader.fetch_publication_pdf_via_http_fallback = lambda target_date, fallback_reason: None
        downloader.get_oilchem_asphalt_price = lambda: None
        downloader.verified_text_path = None

        with self.assertRaises(hnxcl.ArgusStageError) as ctx:
            downloader.get_asia_bitumen_daily()

        self.assertEqual(ctx.exception.stage, "publications_download")
        self.assertIn("未获取到可用的 Argus 源内容", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

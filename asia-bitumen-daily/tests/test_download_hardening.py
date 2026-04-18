import tempfile
import unittest
from pathlib import Path

from scripts import hnxcl


class FakeRow:
    def __init__(self, text):
        self._text = text

    def inner_text(self, timeout=None):
        return self._text

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        return None

    def locator(self, selector):
        raise AssertionError(f"Unexpected nested selector: {selector}")


class FakeLocatorCollection:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, has_text=None):
        if has_text is None:
            return FakeLocatorCollection(self._rows)
        matched = []
        for row in self._rows:
            text = row.inner_text()
            if hasattr(has_text, "search"):
                if has_text.search(text):
                    matched.append(row)
            elif str(has_text) in text:
                matched.append(row)
        return FakeLocatorCollection(matched)

    def count(self):
        return len(self._rows)

    def nth(self, index):
        return self._rows[index]


class FakeSurface:
    def __init__(self, rows=None, name="surface"):
        self._rows = list(rows or [])
        self.url = f"https://example.com/{name}"

    def locator(self, selector):
        if selector in (
            ".scrollable-menu-container li.publication-item",
            "div, li, tr, article, section",
        ):
            return FakeLocatorCollection(self._rows)
        raise AssertionError(f"Unexpected selector: {selector}")


class FakePage(FakeSurface):
    def __init__(self, rows=None, frames=None):
        super().__init__(rows=rows, name="page")
        self.frames = list(frames or [])


class MissingLocator:
    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        raise RuntimeError("missing locator")

    def inner_text(self, timeout=None):
        raise RuntimeError("missing locator")

    def input_value(self, timeout=None):
        raise RuntimeError("missing locator")

    def get_attribute(self, name):
        return None

    def locator(self, selector):
        return self


class SimpleLocator:
    def __init__(self, text="", value=None, children=None):
        self._text = text
        self._value = value
        self._children = children or {}

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        return None

    def inner_text(self, timeout=None):
        return self._text

    def input_value(self, timeout=None):
        if self._value is None:
            raise RuntimeError("no value")
        return self._value

    def get_attribute(self, name):
        if name == "value":
            return self._value
        return None

    def locator(self, selector):
        return self._children.get(selector, MissingLocator())


class FakeDirectPublicationSurface(FakeSurface):
    def __init__(self, preview_text, date_value, publication_id="291"):
        super().__init__(rows=[], name=f"integration/publication?publicationId={publication_id}")
        self.url = f"https://direct.argusmedia.com/integration/publication?publicationId={publication_id}"
        pdf_button = SimpleLocator(text="Download PDF")
        preview = SimpleLocator(
            text=preview_text,
            children={
                "#pdf-button": pdf_button,
                "h2": SimpleLocator(text="Argus Asia Bitumen Daily"),
            },
        )
        self._selector_map = {
            "#pdf-button": pdf_button,
            "#publication-preview": preview,
            "#publication-library": SimpleLocator(text=preview_text),
            "#date-picker": SimpleLocator(value=date_value),
            "#alt-date": SimpleLocator(value=date_value),
            "input.date-input": SimpleLocator(value=date_value),
            "h2": SimpleLocator(text="Argus Asia Bitumen Daily"),
        }

    def locator(self, selector):
        if selector in self._selector_map:
            return self._selector_map[selector]
        return super().locator(selector)


class CountingMissingSurface(FakeSurface):
    def __init__(self, name="blank"):
        super().__init__(rows=[], name=name)
        self.calls = []
        self.url = "about:blank"

    def locator(self, selector):
        self.calls.append(selector)
        return MissingLocator()


class FakeNavigatingPage(FakePage):
    def __init__(self, frames=None):
        super().__init__(rows=[], frames=frames)
        self.url = "https://direct.argusmedia.com/"
        self.goto_calls = []

    def goto(self, url):
        self.goto_calls.append(url)
        self.url = url

    def wait_for_load_state(self, state):
        return None

    def wait_for_timeout(self, ms):
        return None


class FakePdfResponse:
    def __init__(self, url, headers, body):
        self.url = url
        self.headers = headers
        self._body = body

    def body(self):
        return self._body


class DownloadHardeningTests(unittest.TestCase):
    def test_ensure_target_publication_view_navigates_to_publication_url(self):
        frame = FakeDirectPublicationSurface(
            preview_text="Argus Asia Bitumen Daily Download PDF",
            date_value="17 Apr 26",
        )
        downloader = hnxcl.ArgusDownloader()
        downloader.page = FakeNavigatingPage(frames=[frame])

        result = downloader.ensure_target_publication_view()

        self.assertEqual(result, "direct_publication_iframe")
        self.assertEqual(
            downloader.page.goto_calls,
            ["https://direct.argusmedia.com/publication?publicationId=291"],
        )

    def test_document_surfaces_prioritize_target_publication_frame(self):
        blank = CountingMissingSurface()
        frame = FakeDirectPublicationSurface(
            preview_text="Argus Asia Bitumen Daily Download PDF",
            date_value="17 Apr 26",
        )
        downloader = hnxcl.ArgusDownloader()
        downloader.page = FakeNavigatingPage(frames=[blank, frame])

        mode = downloader.ensure_target_publication_view()

        self.assertEqual(mode, "direct_publication_iframe")
        self.assertEqual(blank.calls, [])

    def test_collect_publication_candidates_reads_direct_publication_preview(self):
        frame = FakeDirectPublicationSurface(
            preview_text="Argus Asia Bitumen Daily Download PDF",
            date_value="17 Apr 26",
        )
        downloader = hnxcl.ArgusDownloader()
        downloader.page = FakePage(rows=[], frames=[frame])

        candidates = downloader.collect_publication_candidates("20260417")

        self.assertEqual(len(candidates), 1)
        self.assertIs(candidates[0]["surface"], frame)
        self.assertIn("17 Apr 26", candidates[0]["text"])
        self.assertIn("Download PDF", candidates[0]["text"])

    def test_collect_publication_candidates_reads_frame_surfaces(self):
        frame = FakeSurface(
            rows=[
                FakeRow(
                    "Argus Asia Bitumen Daily: 15-Apr-2026\nPDF\nAsia Bitumen Daily"
                )
            ],
            name="frame",
        )
        downloader = hnxcl.ArgusDownloader()
        downloader.page = FakePage(rows=[], frames=[frame])

        candidates = downloader.collect_publication_candidates("20260415")

        self.assertEqual(len(candidates), 1)
        self.assertIs(candidates[0]["surface"], frame)
        self.assertIn("Argus Asia Bitumen Daily", candidates[0]["text"])

    def test_download_publication_pdf_falls_back_when_download_event_breaks(self):
        downloader = hnxcl.ArgusDownloader()
        selected = {"text": "Argus Asia Bitumen Daily: 15-Apr-2026", "row": object()}
        expected = hnxcl.FetchResult(
            source_type="argus_pdf",
            source_name="Argus Asia Bitumen Daily (20260415).pdf",
            source_report_date="2026年04月15日",
        )
        calls = []

        downloader.open_publications_menu = lambda: calls.append("open_menu")
        downloader.page = type("Page", (), {"wait_for_timeout": lambda self, ms: None})()
        downloader.collect_publication_candidates = lambda target_date: [selected]

        def fail_download(candidate, target_date):
            calls.append(("browser_download", candidate["text"], target_date))
            raise RuntimeError("expect_download timed out")

        def alternate_capture(candidate, target_date, fallback_reason):
            calls.append(("alternate_capture", candidate["text"], target_date, fallback_reason))
            return expected

        downloader._download_candidate_via_browser_download = fail_download
        downloader._download_candidate_via_page_pdf_capture = alternate_capture

        result = downloader.download_publication_pdf("20260415")

        self.assertIs(result, expected)
        self.assertEqual(calls[0], "open_menu")
        self.assertEqual(calls[1], ("browser_download", selected["text"], "20260415"))
        self.assertEqual(calls[2][0], "alternate_capture")
        self.assertIn("expect_download timed out", calls[2][3])

    def test_builds_pdf_result_from_workspaces_api_response(self):
        downloader = hnxcl.ArgusDownloader()
        with tempfile.TemporaryDirectory() as tmpdir:
            downloader.get_target_dir = lambda: Path(tmpdir)
            downloader.is_expected_report_file = lambda path, target_date: True
            response = FakePdfResponse(
                url="https://direct.argusmedia.com/workspaces/api/publication",
                headers={
                    "content-type": "application/pdf",
                    "content-disposition": 'attachment; filename="Argus Asia Bitumen Daily  (2026-04-17).pdf"',
                },
                body=b"%PDF-1.7 test",
            )

            result = downloader._build_pdf_fetch_result_from_response(
                response,
                target_date="20260417",
                stage="publications_download",
                fallback_reason="xhr pdf",
            )

            self.assertEqual(result.source_name, "Argus Asia Bitumen Daily  (2026-04-17).pdf")
            self.assertEqual(result.source_report_date, "2026年04月17日")
            self.assertTrue(result.pdf_path.exists())
            self.assertEqual(result.pdf_path.read_bytes(), b"%PDF-1.7 test")

    def test_download_publication_pdf_prefers_direct_publication_path(self):
        downloader = hnxcl.ArgusDownloader()
        expected = hnxcl.FetchResult(
            source_type="argus_pdf",
            source_name="Argus Asia Bitumen Daily (20260417).pdf",
            source_report_date="2026年04月17日",
        )

        calls = []
        downloader._download_publication_pdf_direct = lambda target_date: calls.append(
            ("direct", target_date)
        ) or expected
        downloader.collect_publication_candidates = lambda target_date: (_ for _ in ()).throw(
            AssertionError("generic candidate scan should not run when direct path succeeds")
        )
        downloader.page = type("Page", (), {"wait_for_timeout": lambda self, ms: None})()

        result = downloader.download_publication_pdf("20260417")

        self.assertIs(result, expected)
        self.assertEqual(calls, [("direct", "20260417")])


if __name__ == "__main__":
    unittest.main()

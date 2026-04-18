import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from scripts import hnxcl


class FakeJsonResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeDingTalkRequests:
    def __init__(self):
        self.calls = []

    def get(self, url):
        self.calls.append({"method": "get", "url": url})
        return FakeJsonResponse({"access_token": "oapi-token"})

    def post(self, url, json=None, files=None, headers=None):
        self.calls.append(
            {
                "method": "post",
                "url": url,
                "json": json,
                "files": files,
                "headers": headers,
            }
        )
        if url.endswith("/v1.0/oauth2/accessToken"):
            return FakeJsonResponse({"accessToken": "new-token"})
        if "media/upload" in url:
            return FakeJsonResponse({"media_id": "@uploaded-media-id"})
        if url.endswith("/v1.0/robot/oToMessages/batchSend"):
            return FakeJsonResponse({"processQueryKey": "sent-key"})
        return FakeJsonResponse({})


class FakePdfResponse:
    def __init__(self, url, headers, body):
        self.url = url
        self.headers = headers
        self._body = body

    def body(self):
        return self._body


class DownloadHardeningTests(unittest.TestCase):
    def test_download_publication_pdf_falls_back_when_download_event_breaks(self):
        downloader = hnxcl.ArgusDownloader()
        expected = hnxcl.FetchResult(
            source_type="argus_pdf",
            source_name="Argus Asia Bitumen Daily (20260415).pdf",
            source_report_date="2026年04月15日",
        )
        calls = []

        def fail_agent_browser(target_date):
            calls.append(("agent-browser", target_date))
            raise RuntimeError("agent-browser unavailable")

        downloader._download_publication_pdf_via_agent_browser = fail_agent_browser
        downloader.fetch_publication_pdf_via_http_fallback = lambda target_date, fallback_reason: calls.append(
            ("http-fallback", target_date, fallback_reason)
        ) or expected
        downloader.ensure_argus_playwright_session = lambda: (_ for _ in ()).throw(
            AssertionError("playwright fallback should not run")
        )

        result = downloader.download_publication_pdf("20260415")

        self.assertIs(result, expected)
        self.assertEqual(calls[0], ("agent-browser", "20260415"))
        self.assertEqual(calls[1][0], "http-fallback")
        self.assertEqual(calls[1][1], "20260415")
        self.assertIn("agent-browser unavailable", calls[1][2])

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
        downloader._download_publication_pdf_via_agent_browser = lambda target_date: calls.append(
            ("agent-browser", target_date)
        ) or expected
        downloader.collect_publication_candidates = lambda target_date: (_ for _ in ()).throw(
            AssertionError("generic candidate scan should not run when direct path succeeds")
        )
        downloader.page = type("Page", (), {"wait_for_timeout": lambda self, ms: None})()

        result = downloader.download_publication_pdf("20260417")

        self.assertIs(result, expected)
        self.assertEqual(calls, [("agent-browser", "20260417")])

    def test_download_publication_pdf_tries_direct_api_when_state_probe_breaks(self):
        downloader = hnxcl.ArgusDownloader()
        expected = hnxcl.FetchResult(
            source_type="argus_pdf",
            source_name="Argus Asia Bitumen Daily (20260417).pdf",
            source_report_date="2026年04月17日",
        )
        calls = []

        downloader._download_publication_pdf_via_agent_browser = lambda target_date: (_ for _ in ()).throw(
            RuntimeError("agent-browser eval --stdin failed")
        )
        downloader._fetch_publication_pdf_via_direct_api = lambda target_date, fallback_reason: calls.append(
            ("direct-api", target_date, fallback_reason)
        ) or expected
        downloader.fetch_publication_pdf_via_http_fallback = lambda target_date, fallback_reason: (_ for _ in ()).throw(
            AssertionError("HTTP fallback should not run when direct API fallback succeeds")
        )

        result = downloader.download_publication_pdf("20260417")

        self.assertIs(result, expected)
        self.assertEqual(calls[0][0], "direct-api")
        self.assertEqual(calls[0][1], "20260417")
        self.assertIn("agent-browser eval --stdin failed", calls[0][2])

    def test_agent_browser_download_uses_direct_api_when_legacy_iframe_is_blank(self):
        downloader = hnxcl.ArgusDownloader()
        expected = hnxcl.FetchResult(
            source_type="argus_pdf",
            source_name="Argus Asia Bitumen Daily (20260417).pdf",
            source_report_date="2026年04月17日",
        )
        calls = []

        downloader._agent_browser_login_argus = lambda: {
            "href": downloader.publication_entrypoint_url(),
            "hasIframe": True,
            "hasPdfButton": False,
            "iframeError": "Script error for: requireConfig",
        }
        downloader._fetch_publication_pdf_via_direct_api = lambda target_date, fallback_reason: calls.append(
            ("direct-api", target_date, fallback_reason)
        ) or expected

        result = downloader._download_publication_pdf_via_agent_browser("20260417")

        self.assertIs(result, expected)
        self.assertEqual(
            calls,
            [("direct-api", "20260417", "agent-browser authenticated session")],
        )

    def test_agent_browser_download_prefers_direct_api_before_legacy_ui(self):
        downloader = hnxcl.ArgusDownloader()
        expected = hnxcl.FetchResult(
            source_type="argus_pdf",
            source_name="Argus Asia Bitumen Daily (20260417).pdf",
            source_report_date="2026年04月17日",
        )
        calls = []

        downloader._agent_browser_login_argus = lambda: {
            "href": downloader.publication_entrypoint_url(),
            "hasIframe": True,
            "hasPdfButton": False,
            "iframeBlank": True,
            "iframeError": "Script error for: requireConfig",
        }
        downloader._fetch_publication_pdf_via_direct_api = lambda target_date, fallback_reason: calls.append(
            ("direct-api", target_date, fallback_reason)
        ) or expected
        downloader._agent_browser_install_publication_capture = lambda: calls.append("install-capture")
        downloader._agent_browser_trigger_publication_download = lambda: (_ for _ in ()).throw(
            AssertionError("legacy UI should not be used when direct API already succeeds")
        )

        result = downloader._download_publication_pdf_via_agent_browser("20260417")

        self.assertIs(result, expected)
        self.assertEqual(
            calls,
            [("direct-api", "20260417", "agent-browser authenticated session")],
        )

    def test_build_agent_browser_command_includes_session_and_executable(self):
        downloader = hnxcl.ArgusDownloader()
        downloader.agent_browser_executable_path = "/tmp/fake-browser"
        downloader.agent_browser_session = "argus-session"

        command = downloader._build_agent_browser_command(["open", "https://example.com"], json_output=True)

        self.assertEqual(
            command,
            [
                "agent-browser",
                "--executable-path",
                "/tmp/fake-browser",
                "--session",
                "argus-session",
                "--json",
                "open",
                "https://example.com",
            ],
        )

    def test_agent_browser_login_uses_url_probe_when_state_eval_breaks(self):
        downloader = hnxcl.ArgusDownloader()
        publication_url = downloader.publication_entrypoint_url()

        def fake_run_agent_browser(args, **kwargs):
            if args[0] == "open":
                return ""
            if args[0] == "get" and args[1] == "url":
                return publication_url
            raise AssertionError(f"unexpected agent-browser command: {args}")

        downloader.run_agent_browser = fake_run_agent_browser
        downloader.agent_browser_wait = lambda *args, **kwargs: None
        downloader._agent_browser_publication_state = lambda: (_ for _ in ()).throw(
            RuntimeError("agent-browser eval --stdin failed")
        )
        downloader._agent_browser_session_is_authenticated = lambda: True

        with patch.dict(
            os.environ,
            {
                "ARGUS_EMAIL": "user@example.com",
                "ARGUS_PASSWORD": "secret",
            },
        ):
            state = downloader._agent_browser_login_argus()

        self.assertEqual(state["href"], publication_url)
        self.assertTrue(state["stateProbeFailed"])

    def test_agent_browser_login_does_not_treat_publication_url_as_authenticated_without_cookies(self):
        downloader = hnxcl.ArgusDownloader()
        publication_url = downloader.publication_entrypoint_url()

        def fake_run_agent_browser(args, **kwargs):
            if args[0] == "open":
                return ""
            if args[0] == "get" and args[1] == "url":
                return publication_url
            raise AssertionError(f"unexpected agent-browser command: {args}")

        downloader.run_agent_browser = fake_run_agent_browser
        downloader.agent_browser_wait = lambda *args, **kwargs: None
        downloader._agent_browser_publication_state = lambda: (_ for _ in ()).throw(
            RuntimeError("agent-browser eval --stdin failed")
        )
        downloader._agent_browser_session_is_authenticated = lambda: False

        with patch.dict(
            os.environ,
            {
                "ARGUS_EMAIL": "user@example.com",
                "ARGUS_PASSWORD": "secret",
            },
        ):
            with self.assertRaises(hnxcl.ArgusStageError) as ctx:
                downloader._agent_browser_login_argus()

        self.assertEqual(ctx.exception.stage, "login")

    def test_agent_browser_login_attempts_form_login_when_url_probe_hits_login_page(self):
        downloader = hnxcl.ArgusDownloader()
        login_url = "https://myaccount.argusmedia.com/login?ReturnUrl=..."
        publication_url = downloader.publication_entrypoint_url()
        calls = []

        url_sequence = iter([login_url, publication_url])

        def fake_run_agent_browser(args, **kwargs):
            calls.append(list(args))
            if args[0] == "open":
                return ""
            if args[0] == "get" and args[1] == "url":
                return next(url_sequence)
            if args[0] in {"fill", "find"}:
                return ""
            raise AssertionError(f"unexpected agent-browser command: {args}")

        downloader.run_agent_browser = fake_run_agent_browser
        downloader.agent_browser_wait = lambda *args, **kwargs: None
        downloader._agent_browser_publication_state = lambda: (_ for _ in ()).throw(
            RuntimeError("agent-browser eval --stdin failed")
        )
        downloader._agent_browser_session_is_authenticated = lambda: True

        with patch.dict(
            os.environ,
            {
                "ARGUS_EMAIL": "user@example.com",
                "ARGUS_PASSWORD": "secret",
            },
        ):
            state = downloader._agent_browser_login_argus()

        self.assertEqual(state["href"], publication_url)
        self.assertIn(["fill", 'input[type="email"]', "user@example.com"], calls)
        self.assertIn(["fill", 'input[type="password"]', "secret"], calls)

    def test_render_html_image_via_agent_browser_outputs_real_jpeg_when_requested(self):
        calls = []

        def fake_runner(args, **kwargs):
            calls.append((list(args), dict(kwargs)))
            command = args[0]
            if command == "eval" and kwargs.get("stdin_text") == "document.fonts ? document.fonts.status : 'unsupported'":
                return "loaded"
            if command == "eval" and "containerWidth" in kwargs.get("stdin_text", ""):
                return {
                    "containerWidth": 900,
                    "containerHeight": 1800,
                    "fontStatus": "loaded",
                    "sampleTextCheck": True,
                    "bodyFontFamily": "Noto Sans",
                    "fontFamilies": [],
                        "title": "Report",
                }
            if command == "eval":
                return {"width": 900, "height": 1800}
            if command == "screenshot":
                Image.new("RGB", (20, 20), "white").save(args[1], format="PNG")
                return ""
            return ""

        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = Path(tmpdir) / "report.html"
            image_path = Path(tmpdir) / "report.jpg"
            html_path.write_text("<html><body><div id='report-container'>ok</div></body></html>", encoding="utf-8")

            metrics = hnxcl.render_html_image_via_agent_browser(
                runner=fake_runner,
                html_path=html_path,
                output_image_path=image_path,
                session_name="html-image-test",
            )

            self.assertTrue(image_path.exists())
            self.assertEqual(image_path.read_bytes()[:2], b"\xff\xd8")
            self.assertEqual(metrics["imageSize"]["width"], "980px")
            self.assertEqual(metrics["imageSize"]["height"], "1880px")
            self.assertEqual(calls[0][0][0], "open")
            self.assertTrue(calls[0][1]["allow_file_access"])
            screenshot_calls = [call for call in calls if call[0][0] == "screenshot"]
            self.assertEqual(len(screenshot_calls), 1)
            self.assertTrue(screenshot_calls[0][1]["full_page"])

    def test_send_image_to_dingtalk_uses_previewable_image_message(self):
        fake_requests = FakeDingTalkRequests()

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "report.jpg"
            image_path.write_bytes(b"\xff\xd8fake-jpeg")

            with patch.dict(
                os.environ,
                {
                    "DINGTALK_APP_KEY": "app-key",
                    "DINGTALK_APP_SECRET": "app-secret",
                },
            ), patch.object(hnxcl, "import_requests", return_value=fake_requests):
                delivered = hnxcl.send_file_to_dingtalk(str(image_path), "42706")

        self.assertTrue(delivered)
        upload_call = next(call for call in fake_requests.calls if "media/upload" in call["url"])
        self.assertIn("type=image", upload_call["url"])
        self.assertEqual(upload_call["files"]["media"][2], "image/jpeg")

        send_call = next(
            call
            for call in fake_requests.calls
            if call["url"].endswith("/v1.0/robot/oToMessages/batchSend")
        )
        self.assertEqual(send_call["json"]["msgKey"], "sampleImageMsg")
        self.assertEqual(
            json.loads(send_call["json"]["msgParam"]),
            {"photoURL": "@uploaded-media-id"},
        )


if __name__ == "__main__":
    unittest.main()

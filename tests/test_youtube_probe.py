#!/usr/bin/env python3
"""Offline tests for YouTube media probing and allowlisted browser export."""
import io
from pathlib import Path
import sys
import tarfile
import tempfile
from unittest import TestCase, mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

import youtube_probe as yp
import download_results as dr


GOOD_CDN = "https://rr1---sn-8xgp1vo.googlevideo.com/videoplayback?foo=bar"


class YouTubeProbeTests(TestCase):
    def test_strict_cdn_host_and_scheme(self):
        self.assertTrue(yp.is_googlevideo(GOOD_CDN))
        for bad in (
            "http://rr1.googlevideo.com/videoplayback",
            "https://googlevideo.com/videoplayback",
            "https://googlevideo.com.evil.test/videoplayback",
            "https://youtube.com/watch?v=jNQXAC9IVRw",
            "https://evil.test.googlevideo.com.evil.test/",
            "https://user:pass@rr1.googlevideo.com/videoplayback",
            "file:///data/data/com.termux/files/home/key",
        ):
            self.assertFalse(yp.is_googlevideo(bad), bad)

    def test_extraction_uses_one_fixed_itag_and_explicit_proxy(self):
        def fake_runner(cmd, **kwargs):
            self.assertEqual(cmd[cmd.index("-f") + 1], "18")
            self.assertEqual(cmd[cmd.index("--proxy") + 1], "")
            self.assertTrue(kwargs["capture_output"])
            self.assertNotIn("HTTPS_PROXY", kwargs["env"])
            return type("Result", (), {"returncode": 0,
                                       "stdout": GOOD_CDN + "\n",
                                       "stderr": ""})()

        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://evil.test:8"}):
            url, status, error = yp.extract_media_url(
                "jNQXAC9IVRw", None, 5, runner=fake_runner)
        self.assertEqual(url, GOOD_CDN)
        self.assertEqual(status, "extract_ok")
        self.assertEqual(error, "")

    def test_extractor_rejects_non_cdn_and_bot_errors(self):
        def bad_url(cmd, **kwargs):
            return type("R", (), {"returncode": 0, "stdout": "https://evil.test/x\n",
                                   "stderr": ""})()
        self.assertEqual(yp.extract_media_url("jNQXAC9IVRw", "http://127.0.0.1:17890",
                                              5, runner=bad_url)[1], "invalid_cdn_url")
        def bad_auth(cmd, **kwargs):
            return type("R", (), {"returncode": 1, "stdout": "",
                                   "stderr": "Please sign in to confirm you are not a bot"})()
        self.assertEqual(yp.extract_media_url("jNQXAC9IVRw", None, 5,
                                              runner=bad_auth)[2],
                         "youtube_auth_or_bot_check")
        def missing_format(cmd, **kwargs):
            return type("R", (), {"returncode": 1, "stdout": "",
                                   "stderr": "Requested format is not available"})()
        self.assertEqual(yp.extract_media_url("jNQXAC9IVRw", None, 5,
                                              runner=missing_format)[2],
                         "itag_18_unavailable")

    def test_only_real_media_mime_and_bytes_count_as_delivery(self):
        class Response:
            status = 206
            def __init__(self, mime, url):
                self.headers = {"Content-Type": mime}
                self.url = url
                self.left = 80000
            def geturl(self):
                return self.url
            def read(self, amount):
                n = min(self.left, amount)
                self.left -= n
                return b"z" * n
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass

        class Opener:
            def __init__(self, response):
                self.response = response
            def open(self, req, timeout):
                return self.response

        with mock.patch.object(yp, "_opener", return_value=Opener(
                Response("video/mp4", GOOD_CDN))):
            result = yp.media_range(GOOD_CDN, None, 5, cap_bytes=65536)
        self.assertEqual(result[0], "media_bytes_ok")
        self.assertEqual(result[3], 65536)

        with mock.patch.object(yp, "_opener", return_value=Opener(
                Response("text/html", GOOD_CDN))):
            result = yp.media_range(GOOD_CDN, None, 5)
        self.assertEqual(result[0], "non_media_response")
        self.assertEqual(result[3], 0)

        with mock.patch.object(yp, "_opener", return_value=Opener(
                Response("video/mp4", "https://evil.test/video"))):
            result = yp.media_range(GOOD_CDN, None, 5)
        self.assertEqual(result[0], "bad_redirect")
        self.assertEqual(result[3], 0)

    def test_tls_connection_reset_is_evidence_not_crash(self):
        # Regression: Python 3.14 urllib wraps a TLS reset in URLError.
        from urllib.error import URLError

        class FailingOpener:
            def __init__(self, exc):
                self.exc = exc

            def open(self, req, timeout):
                raise self.exc

        for reset in (ConnectionResetError(104, "Connection reset by peer"),
                      URLError(ConnectionResetError(104, "Connection reset by peer"))):
            with mock.patch.object(yp, "_opener", return_value=FailingOpener(reset)):
                state, status, elapsed, reason = yp.watch_page("jNQXAC9IVRw", None, 5)
                self.assertEqual((state, status, reason),
                                 ("page_failed", "", "connection_reset"))
                self.assertGreaterEqual(elapsed, 0)

                media, status, _, size, _, reason = yp.media_range(GOOD_CDN, None, 5)
                self.assertEqual((media, status, size, reason),
                                 ("media_failed", "", 0, "connection_reset"))

        with mock.patch.object(yp, "watch_page", return_value=(
                "page_failed", "", 15, "connection_reset")), \
             mock.patch.object(yp, "extract_media_url", return_value=(
                "", "extract_failed", "extractor_or_network_error")):
            result = yp.probe("jNQXAC9IVRw", None, 5)
        self.assertEqual(result["watch_status"], "page_failed")
        self.assertEqual(result["watch_error_category"], "connection_reset")
        self.assertEqual(result["extract_error_category"], "extractor_or_network_error")
        self.assertEqual(result["error_category"], "connection_reset")
        self.assertEqual(result["media_status"], "not_tested")

    def test_https_error_recorded_as_http_status(self):
        from urllib.error import HTTPError
        exc = HTTPError(GOOD_CDN, 403, "Forbidden", {}, None)

        class FailingOpener:
            def open(self, req, timeout):
                raise exc

        with mock.patch.object(yp, "_opener", return_value=FailingOpener()):
            self.assertEqual(yp.media_range(GOOD_CDN, None, 5)[:4],
                             ("media_failed", 403, "", 0))
            self.assertEqual(yp.watch_page("jNQXAC9IVRw", None, 5)[1], 403)

    def test_invalid_id_fails_before_network(self):
        with self.assertRaises(ValueError):
            yp.probe("https://youtube.com/watch?v=bad", None, 5)

    def test_archive_excludes_secrets_and_external_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "sample-youtube-run"
            folder.mkdir()
            (folder / "report.md").write_text("report")
            (folder / "environment.json").write_text('{"mode": "youtube"}')
            (folder / "youtube-results.csv").write_text("protocol,media_bytes\nDIRECT,0\n")
            (folder / "private-mihomo.yaml").write_text("TOP SECRET")
            (folder / "mihomo.log").write_text("SIGNED PRIVATE CDN URL")
            (folder / "external").symlink_to(root / "do-not-include.txt")
            (root / "do-not-include.txt").write_text("SECRET OUTSIDE")
            with mock.patch.object(dr.Path, "home", return_value=root):
                archive = dr.create_archive(folder)
            with tarfile.open(archive) as tar:
                names = set(tar.getnames())
            self.assertEqual(names, {
                folder.name + "/report.md", folder.name + "/environment.json",
                folder.name + "/youtube-results.csv",
            })
            self.assertNotIn("TOP SECRET", archive.read_bytes().decode(
                "latin-1", errors="ignore"))

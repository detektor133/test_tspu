#!/usr/bin/env python3
"""Offline regression tests; do not claim these simulate RF networks or DPI."""
import csv
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlsplit
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))
from generate_report import generate, valid_level
from http_benchmark import HEADERS, run_level, percentile_or_missing
from compare_runs import combine
import vps_session


class TestFieldReport(unittest.TestCase):
    def test_budget_is_upper_bound_even_with_parallel_workers(self):
        payload = b"p" * 1024
        digest = hashlib.sha256(payload).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "1024")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = urlsplit("http://127.0.0.1:%d/payload" % server.server_port)
            result = run_level(target, None, 12, 2, 2, 1024, digest,
                               1024, max_requests=5)
            self.assertEqual(result["requests_total"], 5)
            self.assertEqual(result["requests_ok"], 5)
            self.assertEqual(result["bytes_ok"], 5120)
            self.assertEqual(result["measurement_status"], "budget_limited")
            self.assertLess(result["seconds_elapsed"], 2)
            self.assertLess(result["duration_coverage_pct"], 100)
            self.assertEqual(result["latency_samples"], 5)
            self.assertEqual(result["latency_p95_ms"], "")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_valid_level_requires_complete_time_and_sufficient_sample(self):
        row = {
            "status": "ok", "measurement_status": "time_completed",
            "seconds_configured": 6, "seconds_elapsed": 6.03,
            "latency_samples": 25, "requests_ok": 25
        }
        self.assertTrue(valid_level(row))
        self.assertFalse(valid_level({**row, "seconds_elapsed": 1.7}))
        self.assertFalse(valid_level({**row, "latency_samples": 8}))
        self.assertFalse(valid_level({**row, "measurement_status": "budget_limited"}))
        self.assertFalse(valid_level({**row, "measurement_status": ""}))

    def test_no_false_percentiles_from_tiny_samples(self):
        self.assertEqual(percentile_or_missing([1] * 19, .95, 20), "")
        self.assertEqual(percentile_or_missing([1] * 20, .95, 20), 1.0)
        self.assertEqual(percentile_or_missing([1] * 99, .99, 100), "")
        self.assertEqual(percentile_or_missing([1] * 100, .99, 100), 1.0)

    def test_time_window_with_unlimited_requests_is_completed(self):
        payload = b"q" * 1024
        digest = hashlib.sha256(payload).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "1024")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            target = urlsplit("http://127.0.0.1:%d/payload" % server.server_port)
            result = run_level(target, None, 2, .25, 2, 1024, digest, 1024)
            self.assertEqual(result["measurement_status"], "time_completed")
            self.assertGreaterEqual(result["seconds_elapsed"], .25)
            self.assertGreater(result["requests_ok"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_report_marks_budget_limited_and_does_not_chart_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment.json").write_text(json.dumps({
                "network_label_user_declared": "sbermobile",
                "vps_ipv4": "203.0.113.1", "levels": [4],
                "seconds_per_level": 6, "payload_bytes": 262144, "max_mib": 32
            }))
            with (root / "measurements.csv").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["network", "protocol",
                                        "proxy_name", "status", "error"] + HEADERS)
                writer.writeheader()
                writer.writerow({
                    "network": "sbermobile", "protocol": "Hysteria2",
                    "status": "budget_limited", "measurement_status": "budget_limited",
                    "concurrency": 4, "seconds_configured": 6,
                    "seconds_elapsed": 1.72, "requests_ok": 8, "requests_total": 8,
                    "latency_samples": 8, "latency_p95_ms": "",
                    "bytes_ok": 2097152, "throughput_mib_s": 1.16
                })
            report = generate(root).read_text()
            self.assertIn("Пробный/неполный прогон", report)
            self.assertIn("1.72/6.00", report)
            self.assertIn("budget_limited", report)
            self.assertIn("исключены из графиков", report)
            svg = (root / "charts/throughput.svg").read_text()
            self.assertIn("0.00 МиБ/с", svg)

    def test_compare_does_not_promote_short_runs_to_valid_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folders = []
            for net in ("sbermobile", "rostelecom"):
                folder = root / net
                folder.mkdir()
                folders.append(folder)
                (folder / "environment.json").write_text(json.dumps({
                    "network_label_user_declared": net, "seconds_per_level": 6,
                    "payload_bytes": 262144
                }))
                with (folder / "measurements.csv").open("w", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=["network", "protocol",
                                            "proxy_name", "status", "error"] + HEADERS)
                    writer.writeheader()
                    writer.writerow({
                        "network": net, "protocol": "Hysteria2",
                        "concurrency": 4, "requests_ok": 8, "requests_total": 8,
                        "status": "budget_limited", "throughput_mib_s": 1.16
                    })
            result_path = root / "compare.md"
            text = combine(folders, result_path).read_text()
            self.assertIn("не сопоставимо (budget_limited)", text)
            self.assertFalse((root / "compare-speed.svg").exists())

    def test_report_only_uses_measured_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment.json").write_text(json.dumps({
                "network_label_user_declared": "fixture", "vps_ipv4": "203.0.113.1",
                "date_utc": "2026-11-20T10:00:00+00:00", "levels": [1],
                "payload_bytes": 1024, "seconds_per_level": 1, "max_mib": 8
            }))
            with (root / "measurements.csv").open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["network", "protocol",
                                        "proxy_name", "status", "error"] + HEADERS)
                writer.writeheader()
                writer.writerow({
                    "network": "fixture", "protocol": "VLESS REALITY",
                    "status": "ok", "concurrency": 1, "requests_ok": 3,
                    "requests_total": 4, "bytes_ok": 3072,
                    "throughput_mib_s": 0.00293,
                    "latency_p95_ms": 10,
                    "started_utc": "2026-11-20T10:00:00+00:00",
                    "ended_utc": "2026-11-20T10:00:03+00:00",
                })
            report = generate(root).read_text()
            self.assertIn("3/4", report)
            self.assertIn("unverified_legacy", report)
            self.assertIn("Пробный/неполный прогон", report)
            self.assertIn("0.003", report)
            self.assertIn("Не измерены или недоступны профили", report)
            self.assertNotIn("успешно обходит ТСПУ", report)
            svg = (root / "charts/throughput.svg").read_text()
            self.assertIn("<svg", svg)
            self.assertIn("VLESS REALITY", svg)

    def test_real_loopback_origin_and_cpu_csv(self):
        root = Path("/tmp/test-tspu-field-" + secrets.token_hex(8))
        try:
            vps_session.start(root, duration=6, size=2048)
            state = json.loads((root / "session.json").read_text())
            with urlopen("http://127.0.0.1:18180/payload", timeout=3) as resp:
                payload = resp.read()
            self.assertEqual(len(payload), 2048)
            self.assertEqual(hashlib.sha256(payload).hexdigest(), state["sha256"])
            time.sleep(1.3)
            vps_session.stop(root)
            with (root / "server-resources.csv").open() as stream:
                entries = list(csv.DictReader(stream))
            self.assertTrue(entries)
        finally:
            vps_session.stop(root)
            shutil.rmtree(root, ignore_errors=True)

    def test_vps_session_path_safety(self):
        with self.assertRaises(SystemExit):
            vps_session.directory("/etc")
        with self.assertRaises(SystemExit):
            vps_session.directory("/tmp/test-tspu-field-nothex")


if __name__ == "__main__":
    unittest.main()

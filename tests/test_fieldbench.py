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
from generate_report import generate
from http_benchmark import HEADERS, run_level
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
            result = run_level(target, None, 12, .6, 2, 1024, digest,
                               1024, max_requests=5)
            self.assertEqual(result["requests_total"], 5)
            self.assertEqual(result["requests_ok"], 5)
            self.assertEqual(result["bytes_ok"], 5120)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

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

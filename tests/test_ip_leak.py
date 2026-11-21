#!/usr/bin/env python3
"""Offline regression tests for HTTPS public IP and leak verdicts."""
import ipaddress
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))
import ip_leak_probe as ip


def values(v4a="", v4b="", v6=""):
    return {
        "ipify_v4": {"ip": v4a, "status": "ok" if v4a else "error"},
        "icanhazip_v4": {"ip": v4b, "status": "ok" if v4b else "error"},
        "ipify_v6": {"ip": v6, "status": "ok" if v6 else "error"},
    }


class TestExternalIP(unittest.TestCase):
    def test_two_ipv4_services_confirm_vps_egress(self):
        expected = values("178.236.255.170", "178.236.255.170")
        direct = values("5.3.2.1", "5.3.2.1")
        verdict, evidence = ip.compare(expected, direct, expected)
        self.assertEqual(verdict, "vps_egress_confirmed")
        self.assertEqual(evidence["icanhazip_v4"], "matches_vps")

    def test_public_direct_ip_seen_through_proxy_is_possible_leak(self):
        expected = values("178.236.255.170", "178.236.255.170")
        direct = values("5.3.2.1", "5.3.2.1")
        observed = values("5.3.2.1", "178.236.255.170")
        verdict, evidence = ip.compare(observed, direct, expected)
        self.assertEqual(verdict, "possible_ip_leak")
        self.assertEqual(evidence["ipify_v4"], "possible_direct_ip_leak")

    def test_missing_vps_reference_fails_closed(self):
        observed = values("178.236.255.170", "178.236.255.170")
        self.assertEqual(ip.compare(observed, values(), values())[0], "inconclusive")

    def test_visible_ipv6_without_vps_reference_is_inconclusive(self):
        observed = values("178.236.255.170", "178.236.255.170",
                          "2606:4700:4700::1111")
        ref = values("178.236.255.170", "178.236.255.170")
        self.assertEqual(ip.compare(observed, values(), ref)[0], "inconclusive")

    def test_other_egress_is_not_mislabeled_as_real_ip_leak(self):
        observed = values("8.8.8.8", "8.8.8.8")
        vps = values("178.236.255.170", "178.236.255.170")
        direct = values("5.3.2.1", "5.3.2.1")
        self.assertEqual(ip.compare(observed, direct, vps)[0], "unexpected_egress")

    def test_nonpublic_addresses_not_accepted(self):
        class Response:
            status = 200
            def geturl(self):
                return "https://api.ipify.org?format=json"
            def read(self, maxlen):
                return b'{"ip":"192.168.1.7"}'
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass

        class Opener:
            def open(self, req, timeout):
                return Response()

        with mock.patch.object(ip.urllib.request, "build_opener", return_value=Opener()):
            check = ip.get_ip("https://api.ipify.org?format=json", 4)
        self.assertEqual(check["status"], "invalid_ip")
        self.assertEqual(check["ip"], "")

    def test_explicit_proxy_ignores_ambient_proxy_environment(self):
        class Response:
            status = 200
            def geturl(self):
                return "https://api.ipify.org?format=json"
            def read(self, maxlen):
                return b'{"ip":"178.236.255.170"}'
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass

        class Opener:
            def open(self, req, timeout):
                return Response()

        captured = []
        def opener_factory(proxy_handler):
            captured.append(proxy_handler.proxies)
            return Opener()

        with mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://malicious.invalid:88"}):
            with mock.patch.object(ip.urllib.request, "build_opener",
                                   side_effect=opener_factory):
                self.assertEqual(ip.get_ip(
                    "https://api.ipify.org?format=json", 4, proxy=None)["ip"],
                    "178.236.255.170")
                self.assertEqual(ip.get_ip(
                    "https://api.ipify.org?format=json", 4,
                    proxy="http://127.0.0.1:17890")["ip"], "178.236.255.170")
        self.assertEqual(captured[0], {})
        self.assertEqual(captured[1]["https"], "http://127.0.0.1:17890")

    def test_ipv6_only_service_rejects_ipv4_reply(self):
        class Response:
            status = 200
            def geturl(self):
                return "https://api6.ipify.org?format=json"
            def read(self, maxlen):
                return b'{"ip":"178.236.255.170"}'
            def __enter__(self):
                return self
            def __exit__(self, *_):
                pass

        class Opener:
            def open(self, req, timeout):
                return Response()
        with mock.patch.object(ip.urllib.request, "build_opener", return_value=Opener()):
            value = ip.get_ip("https://api6.ipify.org?format=json", 6)
        self.assertEqual(value["status"], "invalid_ip")


if __name__ == "__main__":
    unittest.main()

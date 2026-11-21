#!/usr/bin/env python3
"""HTTP(S) public-IP echo probing via explicit proxy or DIRECT.

Pure stdlib so the same file can run on the VPS to establish its real egress.
This is not a DNS or WebRTC leak test.
"""
import argparse
import datetime as dt
import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

SERVICES = (
    ("ipify_v4", "https://api.ipify.org?format=json", 4),
    ("icanhazip_v4", "https://ipv4.icanhazip.com/", 4),
    ("ipify_v6", "https://api6.ipify.org?format=json", 6),
)


def error_name(exc):
    if isinstance(exc, urllib.error.HTTPError):
        return "http_" + str(exc.code)
    real = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(real, ConnectionResetError):
        return "connection_reset"
    if isinstance(real, (TimeoutError, socket.timeout)):
        return "timeout"
    return type(real).__name__


def get_ip(url, family, proxy=None, timeout=8):
    proxies = {} if proxy is None else {"http": proxy, "https": proxy}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    req = urllib.request.Request(
        url, headers={"User-Agent": "test-tspu-ip/1", "Cache-Control": "no-store"})
    try:
        with opener.open(req, timeout=timeout) as response:
            original = urllib.parse.urlsplit(url)
            final = urllib.parse.urlsplit(response.geturl())
            if (response.status != 200 or final.scheme != "https"
                    or final.hostname != original.hostname):
                return {"ip": "", "status": "bad_response", "error": "redirect_or_http"}
            payload = response.read(256)
        raw = json.loads(payload.decode("utf-8"))["ip"] if "format=json" in url else payload.decode("ascii").strip()
        value = ipaddress.ip_address(raw)
        if not value.is_global or value.version != family:
            return {"ip": "", "status": "invalid_ip", "error": "non_public_or_wrong_family"}
        return {"ip": str(value), "status": "ok", "error": ""}
    except (OSError, ValueError, KeyError, TypeError, UnicodeError) as exc:
        return {"ip": "", "status": "error", "error": error_name(exc)}


def all_probes(proxy=None, timeout=8):
    return {name: get_ip(url, family, proxy, timeout) for name, url, family in SERVICES}


def visible(results, family):
    found = sorted(set(
        results.get(name, {}).get("ip", "")
        for name, _, v in SERVICES if v == family
        if results.get(name, {}).get("ip", "")
    ))
    return ", ".join(found) if found else "нет данных"


def compare(observed, direct, vps):
    """Status and per-service evidence, with missing reference fail-closed."""
    details = {}
    good_v4 = 0
    leak = False
    different = False
    for name, _, family in SERVICES:
        ip = observed.get(name, {}).get("ip", "")
        expected = vps.get(name, {}).get("ip", "")
        unproxied = direct.get(name, {}).get("ip", "")
        if not ip:
            status = "no_response"
        elif not expected:
            status = "no_vps_reference"
        elif ip == expected:
            status = "matches_vps"
            if family == 4:
                good_v4 += 1
        elif unproxied and ip == unproxied:
            status = "possible_direct_ip_leak"
            leak = True
        else:
            status = "unexpected_egress"
            different = True
        details[name] = status
    if leak:
        verdict = "possible_ip_leak"
    elif different:
        verdict = "unexpected_egress"
    elif good_v4 == 2 and details["ipify_v6"] in (
            "matches_vps", "no_response"):
        verdict = "vps_egress_confirmed"
    else:
        verdict = "inconclusive"
    return verdict, details


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=int, default=8)
    args = parser.parse_args()
    if not 3 <= args.timeout <= 20:
        parser.error("--timeout 3..20")
    print(json.dumps(all_probes(None, args.timeout), sort_keys=True))

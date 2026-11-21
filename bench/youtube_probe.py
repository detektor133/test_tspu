#!/usr/bin/env python3
"""Bounded YouTube watch-page + genuine googlevideo media-byte probe.

No browser playback claim. yt-dlp extracts one fixed itag (18) per path;
the HTTP range transfer is performed separately through that SAME path.
No video, signed CDN URLs, YouTube cookies or extractor logs are retained.
"""
import datetime as dt
import http.client as http_client
import errno
import ssl
import os
import re
import subprocess
import sys
import time
from urllib import error, parse, request

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MIME_PREFIXES = ("video/", "audio/", "application/octet-stream")
FIELDS = [
    "network", "protocol", "port", "watch_status", "watch_http",
    "watch_ms", "extract_status", "media_status", "media_http",
    "media_content_type", "media_bytes", "media_ms", "watch_error_category",
    "extract_error_category", "error_category",
    "started_utc", "ended_utc",
]


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def is_googlevideo(url):
    parts = parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https"
            and host.endswith(".googlevideo.com")
            and host != ".googlevideo.com"
            and not parts.username and not parts.password
            and (parts.port is None or parts.port == 443))


def _opener(proxy):
    # Suppress HTTP(S)_PROXY environment variables, even for DIRECT.
    proxies = {} if proxy is None else {"http": proxy, "https": proxy}
    return request.build_opener(request.ProxyHandler(proxies))


def _error_category(exc):
    if isinstance(exc, error.HTTPError):
        return "http_" + str(exc.code)
    # urllib wraps genuine socket/TLS failures in URLError. Keep the
    # underlying symptom so a DIRECT reset is not mistaken for yt-dlp failure.
    reason = exc.reason if isinstance(exc, error.URLError) else exc
    if isinstance(reason, ConnectionResetError) or (
            isinstance(reason, OSError) and reason.errno == errno.ECONNRESET):
        return "connection_reset"
    if isinstance(reason, (TimeoutError, subprocess.TimeoutExpired)) or (
            isinstance(reason, OSError) and reason.errno == errno.ETIMEDOUT):
        return "timeout"
    if isinstance(reason, ssl.SSLEOFError):
        return "tls_unexpected_eof"
    if isinstance(reason, ssl.SSLError):
        return "tls_error"
    return type(reason).__name__


def watch_page(video_id, proxy, timeout):
    """Return verified HTTPS watch response, not a claim of playable video."""
    url = "https://www.youtube.com/watch?v=" + video_id
    begin = time.monotonic()
    req = request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Android; Mobile) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Cache-Control": "no-cache",
    })
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            final_host = parse.urlsplit(resp.geturl()).hostname or ""
            body = resp.read(65536)
            ok = (resp.status == 200 and final_host.lower() in
                  ("www.youtube.com", "youtube.com", "m.youtube.com")
                  and b"<html" in body[:8192].lower())
            return ("page_ok" if ok else "page_unverified",
                    resp.status, round((time.monotonic() - begin) * 1000), "")
    except (OSError, error.URLError, http_client.HTTPException, ValueError) as exc:
        http_status = exc.code if isinstance(exc, error.HTTPError) else ""
        return ("page_failed", http_status,
                round((time.monotonic() - begin) * 1000), _error_category(exc))


def classify_extractor_failure(stderr):
    msg = (stderr or "").lower()
    if "requested format is not available" in msg or "format is not available" in msg:
        return "itag_18_unavailable"
    if "sign in" in msg or "bot" in msg or "po token" in msg:
        return "youtube_auth_or_bot_check"
    if "403" in msg:
        return "http_403_ambiguous"
    if "429" in msg:
        return "youtube_rate_limit"
    if "timed out" in msg or "timeout" in msg:
        return "timeout"
    return "extractor_or_network_error"


def extract_media_url(video_id, proxy, timeout, runner=subprocess.run):
    """Return an HTTPS googlevideo signed URL in memory only; never log it."""
    cmd = [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-cache-dir",
        "--no-playlist", "--quiet", "--no-warnings", "--get-url",
        "--proxy", proxy if proxy is not None else "",
        "--socket-timeout", str(timeout), "--retries", "0",
        "--extractor-retries", "0", "-f", "18",
        "https://www.youtube.com/watch?v=" + video_id,
    ]
    env = {k: v for k, v in os.environ.items() if k.lower() not in
           ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    try:
        output = runner(cmd, text=True, capture_output=True,
                        timeout=timeout + 12, env=env)
    except subprocess.TimeoutExpired:
        return ("", "extract_timeout", "timeout")
    except OSError as exc:
        return ("", "extract_failed", _error_category(exc))
    if output.returncode:
        return ("", "extract_failed", classify_extractor_failure(output.stderr))
    urls = output.stdout.strip().splitlines()
    if len(urls) != 1 or not is_googlevideo(urls[0]):
        return ("", "invalid_cdn_url", "extractor_unexpected_output")
    return (urls[0], "extract_ok", "")


def media_range(url, proxy, timeout, cap_bytes=1048576):
    """Read up to cap_bytes from the real Googlevideo endpoint, never save media."""
    if not is_googlevideo(url):
        return ("invalid_cdn_url", "", "", 0, 0, "invalid_cdn_url")
    begin = time.monotonic()
    req = request.Request(url, headers={
        "Range": "bytes=0-" + str(cap_bytes - 1),
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    })
    try:
        with _opener(proxy).open(req, timeout=timeout) as resp:
            final = resp.geturl()
            mime = resp.headers.get("Content-Type", "").split(";")[0].lower().strip()
            http = resp.status
            if not is_googlevideo(final):
                return ("bad_redirect", http, mime, 0,
                        round((time.monotonic() - begin) * 1000), "bad_redirect")
            if http not in (200, 206) or not mime.startswith(MIME_PREFIXES):
                return ("non_media_response", http, mime, 0,
                        round((time.monotonic() - begin) * 1000),
                        "unexpected_http_or_mime")
            size = 0
            while size < cap_bytes:
                block = resp.read(min(65536, cap_bytes - size))
                if not block:
                    break
                size += len(block)
            status = "media_bytes_ok" if size >= min(cap_bytes, 65536) else "media_too_short"
            return (status, http, mime, size,
                    round((time.monotonic() - begin) * 1000), "" if status == "media_bytes_ok"
                    else "short_media_response")
    except (OSError, error.URLError, http_client.HTTPException, ValueError) as exc:
        http_status = exc.code if isinstance(exc, error.HTTPError) else ""
        return ("media_failed", http_status, "", 0,
                round((time.monotonic() - begin) * 1000), _error_category(exc))


def probe(video_id, proxy, timeout, cap_bytes=1048576):
    """Small real-media transfer and separate page observation on the same route."""
    if not VIDEO_ID_RE.fullmatch(video_id):
        raise ValueError("Нужен 11-символьный YouTube video ID")
    begin = utcnow()
    watch, watch_http, watch_ms, page_error = watch_page(video_id, proxy, timeout)
    media_url, extract_status, extract_error = extract_media_url(video_id, proxy, timeout)
    if media_url:
        media, media_http, mime, count, media_ms, media_error = media_range(
            media_url, proxy, timeout, cap_bytes)
    else:
        media, media_http, mime, count, media_ms, media_error = (
            "not_tested", "", "", 0, 0, extract_error)
    return {
        "watch_status": watch, "watch_http": watch_http, "watch_ms": watch_ms,
        "extract_status": extract_status, "media_status": media,
        "media_http": media_http, "media_content_type": mime,
        "media_bytes": count, "media_ms": media_ms,
        "watch_error_category": page_error,
        "extract_error_category": extract_error,
        "error_category": page_error or media_error,
        "started_utc": begin, "ended_utc": utcnow(),
    }

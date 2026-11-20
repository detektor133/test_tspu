#!/usr/bin/env python3
"""Explicit field-test modes: connectivity, sustained load and duration/volume soak.

No source of truth depends on the Android UI network label. These helpers only
plan a bounded workload, classify measured observations, and summarize raw CSVs.
"""
from collections import defaultdict
from datetime import datetime
import math
import statistics

ALIASES = {
    "reality": "VLESS REALITY", "xhttp": "VLESS XHTTP",
    "hy2": "Hysteria2", "hysteria2": "Hysteria2",
    "tuic": "TUIC", "ss": "Shadowsocks", "shadowsocks": "Shadowsocks",
    "trojan": "Trojan", "anytls": "AnyTLS",
}

DEFAULTS = {
    "smoke": {"protocols": "all", "levels": "1", "seconds": 5,
              "max_mib": 16, "ping_interval": None},
    "load": {"protocols": "reality,hy2,tuic", "levels": "1,4,8,16",
             "seconds": 60, "max_mib": 768, "ping_interval": .2},
    "soak": {"protocols": "reality", "levels": "4", "seconds": 180,
             "max_mib": 512, "ping_interval": .2},
}

def measurement_csv_fields(benchmark_headers, evidence_fields):
    """One CSV contract shared by Android execution and offline regression tests."""
    fields = (["network", "protocol", "proxy_name", "status", "mode", "error",
               "cumulative_mib", "protocol_cumulative_mib"]
              + list(evidence_fields) + list(benchmark_headers))
    if len(fields) != len(set(fields)):
        raise ValueError("Повторяющиеся поля в CSV измерений")
    return fields


WINDOW_FIELDS = ["network", "protocol", "concurrency", "window_start_sec",
                 "window_seconds", "downloads_total", "downloads_ok",
                 "bytes_ok", "throughput_mib_s", "ping_total", "ping_ok",
                 "ping_median_ms", "ping_p95_ms"]


def choose_mode(mode, protocol_arg, levels_arg, seconds_arg, mib_arg, ping_arg, available):
    if mode not in DEFAULTS:
        raise ValueError("Неизвестный режим: " + str(mode))
    cfg = DEFAULTS[mode]
    selected_text = protocol_arg or cfg["protocols"]
    if selected_text == "all":
        selected = [name for name in available]
    else:
        names = [v.strip().lower() for v in selected_text.split(",")]
        if any(name not in ALIASES for name in names):
            raise ValueError("--protocols: reality,xhttp,hy2,tuic,ss,trojan,anytls или all")
        selected = [ALIASES[name] for name in names]
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Список протоколов пуст или содержит повторы")
    missing = [name for name in selected if name not in available]
    if missing:
        raise ValueError("Отсутствуют профили: " + ", ".join(missing))
    try:
        levels = [int(v.strip()) for v in (levels_arg or cfg["levels"]).split(",")]
    except ValueError as exc:
        raise ValueError("--levels: числа через запятую") from exc
    if not levels or len(levels) > 7 or len(set(levels)) != len(levels) or any(not 1 <= x <= 64 for x in levels):
        raise ValueError("--levels: до 7 различных значений 1..64")
    seconds = cfg["seconds"] if seconds_arg is None else seconds_arg
    mib = cfg["max_mib"] if mib_arg is None else mib_arg
    interval = cfg["ping_interval"] if ping_arg is None else ping_arg
    if not (2 <= seconds <= 900) or not (8 <= mib <= 4096):
        raise ValueError("--seconds 2..900 и --max-mib 8..4096")
    if mode == "load" and seconds < 20:
        raise ValueError("Режим load: минимум 20 секунд на ступень")
    if mode == "soak" and seconds < 60:
        raise ValueError("Режим soak: минимум 60 секунд на ступень")
    if interval is not None and not (.1 <= interval <= 5):
        raise ValueError("--ping-interval: 0.1..5 секунд")
    return {"mode": mode, "protocols": selected, "levels": levels,
            "seconds": seconds, "max_mib": mib,
            "ping_interval": interval if mode != "smoke" else None}


def minimum_budget_mib(mode, protocol_count, levels, payload_bytes, smoke_requests=3):
    # Even if a request fails, a full payload's worth of budget is reserved.
    if mode == "smoke":
        required = sum(max(level, smoke_requests) for level in levels) * protocol_count
    else:
        required = sum(max(level, 20) for level in levels) * protocol_count
    return math.ceil(required * payload_bytes / (1024 ** 2))


def classify(mode, measurement, route, threads, min_success_pct=95):
    if not measurement["requests_ok"]:
        return "failed"
    if route["route_status"] in ("no_ingress", "ambiguous"):
        return "route_" + route["route_status"]
    if mode == "smoke":
        return ("smoke_pass" if measurement["requests_failed"] == 0
                else "smoke_degraded")
    if measurement["measurement_status"] == "budget_limited":
        return "budget_limited"
    if measurement["peak_inflight"] < threads:
        return "underfilled_concurrency"
    if measurement["requests_ok"] < 20:
        return "insufficient_samples"
    if measurement["success_rate_pct"] < min_success_pct:
        return "degraded"
    return "ok"


def percentile(samples, q, minimum=20):
    if len(samples) < minimum:
        return ""
    return round(sorted(samples)[math.ceil(q * len(samples)) - 1], 2)


def windows_from_samples(rows, samples, window_seconds=10):
    """Deterministic timeline from raw completed requests; no synthetic traffic.

    Bins are relative to the beginning of each recorded stage. Throughput is
    the bytes of completed downloads per actual bucket duration, rather than
    an assertion that the bytes crossed the wire evenly within that bucket.
    """
    result = []
    grouped = defaultdict(list)
    for sample in samples:
        grouped[(sample.get("protocol"), str(sample.get("concurrency")))].append(sample)
    for row in rows:
        if not row.get("started_utc") or not row.get("seconds_elapsed"):
            continue
        try:
            start = datetime.fromisoformat(row["started_utc"].replace("Z", "+00:00"))
            elapsed = float(row["seconds_elapsed"])
            level = str(row["concurrency"])
            count = max(1, math.ceil(elapsed / window_seconds))
            grouped_samples = grouped.get((row["protocol"], level), [])
        except (ValueError, TypeError, KeyError):
            continue
        buckets = {i: {"download": [], "ping": []} for i in range(count)}
        for item in grouped_samples:
            try:
                when = datetime.fromisoformat(item["ended_utc"].replace("Z", "+00:00"))
                bucket = min(count - 1, max(0, int((when - start).total_seconds() // window_seconds)))
            except (KeyError, TypeError, ValueError):
                continue
            kind = item.get("kind")
            if kind in ("download", "ping"):
                buckets[bucket][kind].append(item)
        for index, payload in buckets.items():
            span = max(.001, min(float(window_seconds), elapsed - index * window_seconds))
            downloads, pings = payload["download"], payload["ping"]
            successful = [s for s in downloads if str(s.get("ok")) == "1"]
            p_ok = [s for s in pings if str(s.get("ok")) == "1"]
            rtts = [float(s["latency_ms"]) for s in p_ok if s.get("latency_ms") not in (None, "")]
            bytes_ok = sum(int(x.get("bytes_ok") or 0) for x in successful)
            result.append({
                "network": row.get("network", ""),
                "protocol": row["protocol"], "concurrency": row["concurrency"],
                "window_start_sec": index * window_seconds,
                "window_seconds": round(span, 3),
                "downloads_total": len(downloads),
                "downloads_ok": len(successful), "bytes_ok": bytes_ok,
                "throughput_mib_s": round(bytes_ok / (1024 ** 2 * span), 3),
                "ping_total": len(pings), "ping_ok": len(p_ok),
                "ping_median_ms": round(statistics.median(rtts), 2) if rtts else "",
                "ping_p95_ms": percentile(rtts, .95),
            })
    return result

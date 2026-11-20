#!/usr/bin/env python3
"""Воспроизводимый HTTP-бенчмарк через локальный HTTP-прокси клиента Mihomo.

Тестируйте только свои серверы / endpoint, для которого есть разрешение.
"""

import argparse
import concurrent.futures
import csv
import hashlib
import http.client
import math
import pathlib
import secrets
import statistics
import ssl
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, urlencode, parse_qsl

HEADERS = [
    'date_utc', 'label', 'concurrency', 'seconds_configured', 'seconds_elapsed',
    'started_utc', 'ended_utc',
    'requests_total', 'requests_ok', 'requests_failed', 'success_rate_pct',
    'bytes_ok', 'throughput_mib_s', 'ttfb_p50_ms', 'ttfb_p95_ms',
    'latency_p50_ms', 'latency_p95_ms', 'latency_p99_ms',
    'measurement_status', 'duration_coverage_pct', 'latency_samples',
    'app_rtt_samples', 'app_rtt_failed', 'app_rtt_p50_ms', 'app_rtt_p95_ms',
]


def percentile(samples, q):
    """Ближайший ранг, 0 при отсутствии успешных запросов."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


MIN_P95_SAMPLES = 20
MIN_P99_SAMPLES = 100


def percentile_or_missing(samples, q, minimum):
    """A percentile from too few completed responses is not a defensible estimate."""
    return round(percentile(samples, q), 2) if len(samples) >= minimum else ""


def request_once(target, proxy, timeout, min_bytes, expected_sha256, max_bytes,
                 verify_nonce=False):
    start = time.monotonic()
    start_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    nonce = secrets.token_hex(12) if verify_nonce else ""
    if verify_nonce:
        query = urlencode([*parse_qsl(target.query, keep_blank_values=True), ("nonce", nonce)])
        target = urlsplit(urlunsplit((target.scheme, target.netloc, target.path, query, "")))
    conn = None
    try:
        if proxy is not None:
            if target.scheme == 'https':
                conn = http.client.HTTPSConnection(proxy.hostname, proxy.port, timeout=timeout,
                                                  context=ssl.create_default_context())
                conn.set_tunnel(target.hostname, target.port or 443)
                request_target = target.path or '/'
                if target.query:
                    request_target += '?' + target.query
            else:
                conn = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=timeout)
                request_target = target.geturl()
        elif target.scheme == 'https':
            conn = http.client.HTTPSConnection(target.hostname, target.port or 443,
                                              timeout=timeout, context=ssl.create_default_context())
            request_target = target.path or '/'
            if target.query:
                request_target += '?' + target.query
        else:
            conn = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=timeout)
            request_target = target.path or '/'
            if target.query:
                request_target += '?' + target.query
        conn.request('GET', request_target, headers={
            'Host': target.netloc,
            'Connection': 'close',
            'Cache-Control': 'no-store',
            'Accept-Encoding': 'identity',
        })
        response = conn.getresponse()
        ttfb = (time.monotonic() - start) * 1000
        digest = hashlib.sha256() if expected_sha256 else None
        count = 0
        while True:
            buf = response.read(65536)
            if not buf:
                break
            count += len(buf)
            if count > max_bytes:
                raise ValueError('ответ превысил ограничение размера')
            if digest:
                digest.update(buf)
        elapsed_ms = (time.monotonic() - start) * 1000
        if response.status != 200 or count < min_bytes:
            raise ValueError(f'HTTP {response.status} или недостаточный размер ответа')
        if digest and digest.hexdigest() != expected_sha256:
            raise ValueError('SHA256 полезной нагрузки не совпал')
        if verify_nonce and response.getheader("X-Test-Nonce") != nonce:
            raise ValueError("ответ не содержит nonce исходного запроса")
        end_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return (True, count, ttfb, elapsed_ms, "", nonce, start_utc, end_utc)
    except (OSError, TimeoutError, ValueError, http.client.HTTPException, ssl.SSLError) as exc:
        end_utc = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        return (False, 0, 0.0, (time.monotonic() - start) * 1000,
                type(exc).__name__, nonce, start_utc, end_utc)
    finally:
        if conn is not None:
            conn.close()


def run_level(target, proxy, threads, seconds, timeout, min_bytes, expected_sha256, max_bytes,
              max_requests=None, on_sample=None, verify_nonce=False,
              ping_target=None, on_ping_sample=None, ping_interval=1.0):
    completed = []
    lock = threading.Lock()
    started = time.monotonic()
    deadline = started + seconds
    reserved = 0
    budget_limited = False
    app_ping = []
    stop_ping = threading.Event()

    def ping_worker():
        # Small HTTP request-response through the SAME selected VPN while
        # download workers saturate it. This is application-level RTT,
        # NOT ICMP RTT; it includes connection/stream setup and forwarding.
        while not stop_ping.is_set() and time.monotonic() < deadline:
            result = request_once(ping_target, proxy, min(timeout, 6), 0, None, 0,
                                  verify_nonce=True)
            app_ping.append(result)
            if on_ping_sample is not None:
                on_ping_sample({
                    "started_utc": result[6], "ended_utc": result[7],
                    "ok": int(result[0]), "bytes_ok": 0,
                    "ttfb_ms": round(result[2], 2) if result[0] else "",
                    "latency_ms": round(result[3], 2),
                    "error_type": result[4], "nonce": result[5],
                })
            stop_ping.wait(min(ping_interval, max(0.0, deadline - time.monotonic())))

    def worker():
        nonlocal reserved, budget_limited
        batch = []
        while time.monotonic() < deadline:
            with lock:
                if max_requests is not None and reserved >= max_requests:
                    # Do not call a quota-truncated run a full-duration load test.
                    if time.monotonic() < deadline:
                        budget_limited = True
                    break
                reserved += 1
            result = request_once(target, proxy, timeout, min_bytes, expected_sha256,
                                  max_bytes, verify_nonce=verify_nonce)
            batch.append(result)
            if on_sample is not None:
                on_sample({
                    "started_utc": result[6], "ended_utc": result[7],
                    "ok": int(result[0]), "bytes_ok": result[1],
                    "ttfb_ms": round(result[2], 2) if result[0] else "",
                    "latency_ms": round(result[3], 2),
                    "error_type": result[4], "nonce": result[5],
                })
        with lock:
            completed.extend(batch)

    pinger = None
    if ping_target is not None:
        pinger = threading.Thread(target=ping_worker, daemon=True)
        pinger.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            tasks = [executor.submit(worker) for _ in range(threads)]
            for task in tasks:
                task.result()
    finally:
        stop_ping.set()
        if pinger is not None:
            pinger.join()  # A request is timeout-bounded; never leak into next profile.
    wall = time.monotonic() - started
    ok = [r for r in completed if r[0]]
    ping_ok = [r for r in app_ping if r[0]]
    first_byte = [r[2] for r in ok]
    full_latency = [r[3] for r in ok]
    return {
        'seconds_elapsed': round(wall, 3),
        'requests_total': len(completed),
        'requests_ok': len(ok),
        'requests_failed': len(completed) - len(ok),
        'success_rate_pct': round(len(ok) / len(completed) * 100, 2) if completed else 0,
        'bytes_ok': sum(r[1] for r in ok),
        'throughput_mib_s': round(sum(r[1] for r in ok) / (1024 ** 2 * wall), 3) if wall else 0,
        'ttfb_p50_ms': round(percentile(first_byte, .5), 2) if ok else '',
        'ttfb_p95_ms': percentile_or_missing(first_byte, .95, MIN_P95_SAMPLES),
        'latency_p50_ms': round(percentile(full_latency, .5), 2) if ok else '',
        'latency_p95_ms': percentile_or_missing(full_latency, .95, MIN_P95_SAMPLES),
        'latency_p99_ms': percentile_or_missing(full_latency, .99, MIN_P99_SAMPLES),
        'measurement_status': 'budget_limited' if budget_limited else 'time_completed',
        'duration_coverage_pct': round(min(100.0, wall / seconds * 100), 1),
        'latency_samples': len(ok),
        'app_rtt_samples': len(ping_ok),
        'app_rtt_failed': len(app_ping) - len(ping_ok),
        'app_rtt_p50_ms': (round(percentile([r[3] for r in ping_ok], .5), 2)
                           if ping_ok else ''),
        'app_rtt_p95_ms': percentile_or_missing([r[3] for r in ping_ok],
                                               .95, MIN_P95_SAMPLES),
    }


def main():
    parser = argparse.ArgumentParser(description='HTTP-трафик через Mihomo с замерами под нагрузкой')
    parser.add_argument('--url', required=True, help='HTTP(S)-адрес контрольного сервера')
    parser.add_argument('--proxy', help='локальный HTTP-прокси, например http://127.0.0.1:7890')
    parser.add_argument('--direct', action='store_true', help='тест без прокси (контроль)')
    parser.add_argument('--levels', default='1,8,32', help='число потоков, через запятую')
    parser.add_argument('--seconds', type=float, default=10, help='время каждого уровня, секунд')
    parser.add_argument('--timeout', type=float, default=6, help='тайм-аут одного HTTP-запроса')
    parser.add_argument('--min-bytes', type=int, default=65536, help='минимум байт корректного ответа')
    parser.add_argument('--max-bytes', type=int, default=64 * 1024 * 1024, help='лимит размера ответа')
    parser.add_argument('--sha256', help='SHA256 ожидаемого содержимого (дополнительная проверка)')
    parser.add_argument('--label', default='без названия', help='сеть, протокол, провайдер')
    parser.add_argument('--output', required=True, type=pathlib.Path, help='путь для CSV (без токенов)')
    args = parser.parse_args()
    target = urlsplit(args.url)
    if target.scheme not in ('http', 'https') or not target.hostname or target.username:
        parser.error('--url должен быть корректным HTTP(S)-адресом без учётных данных')
    if args.direct == bool(args.proxy):
        parser.error('выберите ровно одно: --proxy или --direct')
    proxy = urlsplit(args.proxy) if args.proxy else None
    if proxy and (proxy.scheme != 'http' or not proxy.hostname or proxy.username):
        parser.error('--proxy должен быть HTTP-адресом локального прокси без логина')
    if proxy and proxy.port is None:
        parser.error('--proxy должен содержать порт')
    try:
        levels = [int(x) for x in args.levels.split(',')]
    except ValueError:
        parser.error('--levels: ожидаются целые числа через запятую')
    if not levels or len(levels) > 8 or any(not (1 <= n <= 128) for n in levels):
        parser.error('--levels: от 1 до 8 уровней, каждый от 1 до 128 потоков')
    if not (.5 <= args.seconds <= 3600) or not (.5 <= args.timeout <= 60):
        parser.error('некорректные --seconds или --timeout')
    if not (0 <= args.min_bytes <= args.max_bytes):
        parser.error('неверный диапазон min/max байт')
    if args.sha256 and (len(args.sha256) != 64 or any(ch not in '0123456789abcdef' for ch in args.sha256)):
        parser.error('ожидается SHA256 в виде 64 символов hex (нижний регистр)')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    results = []
    for level in levels:
        started_utc = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        measurement = run_level(target, proxy, level, args.seconds, args.timeout,
                                args.min_bytes, args.sha256, args.max_bytes)
        row = {
            'date_utc': now, 'label': args.label,
            'started_utc': started_utc,
            'ended_utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'concurrency': level, 'seconds_configured': args.seconds,
            **measurement,
        }
        results.append(row)
        p95_ttfb = str(row['ttfb_p95_ms']) if row['ttfb_p95_ms'] != '' else 'н/д (мало выборок)'
        p95_full = str(row['latency_p95_ms']) if row['latency_p95_ms'] != '' else 'н/д (мало выборок)'
        print(f"{args.label}: {level:>3} потоков | {row['requests_ok']}/{row['requests_total']} OK | "
              f"{row['throughput_mib_s']:.3f} МиБ/с | "
              f"{row['seconds_elapsed']:.2f}/{args.seconds:.2f} с "
              f"[{row['measurement_status']}] | p95 TTFB {p95_ttfb} мс | "
              f"p95 полного ответа {p95_full} мс", flush=True)
    # Не сохраняем URL, токены и содержимое HTTP-ответов.
    with args.output.open('w', newline='', encoding='utf-8') as fp:
        writer = csv.DictWriter(fp, fieldnames=HEADERS)
        writer.writeheader()
        writer.writerows(results)
    if not any(r['requests_ok'] for r in results):
        raise SystemExit('Бенчмарк: нет успешных запросов — проверьте клиента и сервер')


if __name__ == '__main__':
    main()
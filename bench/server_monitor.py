#!/usr/bin/env python3
"""Выборка CPU/RSS реальных процессов Mihomo и m-ui по /proc на VPS."""
import argparse
import csv
import os
import pathlib
import time
from datetime import datetime, timezone

PROCESSES = {'mihomo': b'/usr/local/m-ui/mihomo', 'm-ui': b'/usr/local/m-ui/m-ui'}
HEADERS = ['timestamp_utc', 'elapsed_sec', 'mihomo_count', 'mihomo_cpu_pct',
           'mihomo_rss_mib', 'mui_count', 'mui_cpu_pct', 'mui_rss_mib']


def read_processes():
    found = {'mihomo': [], 'm-ui': []}
    for entry in pathlib.Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            argv = (entry / 'cmdline').read_bytes().split(b'\0')
            exe = argv[0]
            kind = next((k for k, val in PROCESSES.items() if exe == val), None)
            if not kind:
                continue
            stat = (entry / 'stat').read_text()
            # /proc/PID/stat: (comm) may contain blanks or ')' — locate last ')'.
            fields = stat.rsplit(')', 1)[1].split()
            ticks = int(fields[11]) + int(fields[12])  # utime+stime (fields 14+15)
            status = (entry / 'status').read_text()
            rss_kib = next((int(line.split()[1]) for line in status.splitlines()
                            if line.startswith('VmRSS:')), 0)
            found[kind].append((entry.name, ticks, rss_kib))
        except (OSError, ValueError, IndexError):
            continue  # процесс мог завершиться между чтениями
    return found


def main():
    ap = argparse.ArgumentParser(description='CPU и память Mihomo/m-ui на VPS')
    ap.add_argument('--duration', type=float, default=60)
    ap.add_argument('--interval', type=float, default=1)
    ap.add_argument('--output', required=True, type=pathlib.Path)
    args = ap.parse_args()
    if not (1 <= args.duration <= 7200) or not (.1 <= args.interval <= 10):
        ap.error('duration 1..7200, interval 0.1..10 секунд')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tick_rate = os.sysconf('SC_CLK_TCK')
    started = time.monotonic()
    prev_time = started
    prev = read_processes()
    count = 0
    with args.output.open('w', newline='', encoding='utf-8') as fp:
        writer = csv.DictWriter(fp, fieldnames=HEADERS)
        writer.writeheader()
        while time.monotonic() - started < args.duration:
            time.sleep(min(args.interval, max(0, started + args.duration - time.monotonic())))
            now = time.monotonic()
            curr = read_processes()
            delta_sec = max(.001, now - prev_time)
            row = {'timestamp_utc': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                   'elapsed_sec': round(now - started, 3)}
            for kind, prefix in [('mihomo', 'mihomo'), ('m-ui', 'mui')]:
                past = {p: tick for p, tick, _ in prev[kind]}
                ticks = sum(max(0, tick - past.get(p, tick)) for p, tick, _ in curr[kind])
                row[f'{prefix}_count'] = len(curr[kind])
                # 100% = одно полностью загруженное логическое ядро.
                row[f'{prefix}_cpu_pct'] = round(ticks / tick_rate / delta_sec * 100, 2)
                row[f'{prefix}_rss_mib'] = round(sum(rss for _, _, rss in curr[kind]) / 1024, 2)
            writer.writerow(row)
            fp.flush()
            count += 1
            prev, prev_time = curr, now
    if count == 0:
        raise SystemExit('Не удалось получить ни одного образца')
    print(f'Сохранено {count} измерений CPU/RSS в {args.output}')


if __name__ == '__main__':
    main()
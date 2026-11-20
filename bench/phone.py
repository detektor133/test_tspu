#!/usr/bin/env python3
"""Android/Termux field test. Data path: phone -> Mihomo -> VPN -> VPS.

SSH is CONTROL ONLY: copy loopback-origin and monitor scripts, start/stop them,
retrieve CPU/RSS. No tethering, reverse forwarding or root on Android.
"""
import argparse
import csv
import datetime as dt
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from urllib import request, parse

try:
    import yaml
except ImportError:
    raise SystemExit("Нет PyYAML. В Termux установите: python -m pip install pyyaml")

from http_benchmark import HEADERS, run_level, request_once
from generate_report import generate
from port_capture import port_evidence

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "test-tspu"
SUB_FILE = CONFIG_DIR / "subscription.url"
MIHOMO = Path.home() / ".local" / "bin" / "mihomo"
PORTS = [
    (443, "VLESS REALITY"), (11443, "VLESS XHTTP"),
    (14443, "Hysteria2"), (8443, "TUIC"), (8388, "Shadowsocks"),
    (9443, "Trojan"), (10443, "AnyTLS"),
]
CSV_FIELDS = ["network", "protocol", "proxy_name", "status", "error",
              "route_status", "route_port", "route_packets", "route_ip_bytes",
              "unexpected_port_packets", "cumulative_mib", "protocol_cumulative_mib"] + HEADERS
SAMPLE_FIELDS = ["network", "protocol", "concurrency", "kind",
                 "started_utc", "ended_utc",
                 "ok", "bytes_ok", "ttfb_ms", "latency_ms", "error_type", "nonce"]
TRANSPORT = {"VLESS REALITY": "tcp", "VLESS XHTTP": "tcp",
             "Hysteria2": "udp", "TUIC": "udp", "Shadowsocks": "tcp",
             "Trojan": "tcp", "AnyTLS": "tcp"}
TYPES = {"VLESS REALITY": "vless", "VLESS XHTTP": "vless",
         "Hysteria2": "hysteria2", "TUIC": "tuic",
         "Shadowsocks": "ss", "Trojan": "trojan", "AnyTLS": "anytls"}


def vps_counters(dest, port, remote):
    response = run_ssh(dest, port, "python3 " + remote
                       + "/vps_session.py counters --dir " + remote)
    return json.loads(response)



def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def configure():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    url = getpass.getpass("Вставьте секретную ссылку Mihomo/Clash (не отобразится): ").strip()
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or not parsed.path.endswith("/clash"):
        raise SystemExit("Нужна HTTPS-ссылка вида https://IP:2053/.../clash")
    fd = os.open(str(SUB_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fp:
        fp.write(url + "\n")
    SUB_FILE.chmod(0o600)
    print("Адрес сохранён приватно в ~/.config/test-tspu/subscription.url (0600)")


def subscription():
    if not SUB_FILE.is_file():
        raise SystemExit("Сначала: python bench/phone.py configure")
    if SUB_FILE.stat().st_mode & 0o077:
        raise SystemExit("Секретный файл открыт другим пользователям: chmod 600 " + str(SUB_FILE))
    url = SUB_FILE.read_text().strip()
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
        raise SystemExit("Некорректная HTTPS-подписка")
    # Не использовать ambient HTTP_PROXY, иначе тестовая машина станет посредником.
    opener = request.build_opener(request.ProxyHandler({}))
    with opener.open(request.Request(url, headers={"User-Agent": "test-tspu-field/1"}), timeout=25) as resp:
        body = resp.read(2 * 1024 * 1024)
    cfg = yaml.safe_load(body)
    if not isinstance(cfg, dict) or not isinstance(cfg.get("proxies"), list):
        raise SystemExit("Mihomo/Clash-подписка не содержит proxies")
    return cfg, parsed.hostname


def run_ssh(dest, port, cmd, timeout=45):
    args = ["ssh", "-p", str(port), "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10", dest, cmd]
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError("SSH к VPS не выполнен. Настройте ключ Termux (ssh -o BatchMode=yes "
                           + dest + " true); ошибка: " + p.stderr[:220])
    return p.stdout.strip()


def remote_scp(port, src, dest):
    p = subprocess.run(["scp", "-q", "-P", str(port), str(src), dest],
                       capture_output=True, text=True, timeout=65)
    if p.returncode:
        raise RuntimeError("SCP: не удалось передать файл; " + p.stderr[:200])


def api(method, path, secret, payload=None):
    url = "http://127.0.0.1:19090" + path
    body = json.dumps(payload).encode() if payload is not None else None
    req = request.Request(url, data=body, method=method,
                          headers={"Authorization": "Bearer " + secret,
                                   "Content-Type": "application/json"})
    opener = request.build_opener(request.ProxyHandler({}))
    with opener.open(req, timeout=3) as response:
        if response.status not in (200, 204):
            raise RuntimeError("Локальный Mihomo API вернул HTTP " + str(response.status))
        data = response.read()
        return json.loads(data) if data else {}


def fail_row(network, label, name, status, error):
    return {"network": network, "protocol": label, "proxy_name": name,
            "status": status, "error": error, "date_utc": now(),
            "started_utc": "", "ended_utc": ""}


def validate_vps(raw):
    try:
        return str(ipaddress.IPv4Address(raw))
    except ipaddress.AddressValueError:
        raise SystemExit("--vps должен быть публичным IPv4 сервера")


def run(a):
    os.umask(0o077)
    server_ip = validate_vps(a.vps)
    if a.network is None or not re.fullmatch(r"[a-zA-Z0-9_-]{2,40}", a.network):
        raise SystemExit("--network: краткая метка сети, например sbermobile или rostelecom")
    if not MIHOMO.is_file() or not os.access(MIHOMO, os.X_OK):
        raise SystemExit("Нет Android Mihomo. Запустите bash bench/setup_termux.sh")
    if a.ssh_port < 1 or a.ssh_port > 65535 or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", a.ssh_user):
        raise SystemExit("Некорректные SSH-параметры")
    if not (3 <= a.seconds <= 120 and 8 <= a.max_mib <= 4096):
        raise SystemExit("--seconds 3..120, --max-mib 8..4096")
    try:
        levels = [int(v.strip()) for v in a.levels.split(",")]
    except ValueError:
        raise SystemExit("--levels: целые числа через запятую")
    if not levels or len(levels) > 7 or any(not 1 <= n <= 64 for n in levels) or len(set(levels)) != len(levels):
        raise SystemExit("--levels: 1..7 уникальных уровней от 1 до 64 потоков")
    subscribed, sub_host = subscription()
    if sub_host != server_ip:
        raise SystemExit("Адрес подписки и --vps различаются. Не тестируйте другой сервер.")
    proxies = subscribed["proxies"]
    nodes = {}
    for node in proxies:
        if not isinstance(node, dict):
            continue
        if node.get("server") != server_ip:
            continue
        for port, label in PORTS:
            if node.get("port") == port and label not in nodes:
                if node.get("type") != TYPES[label]:
                    raise SystemExit("Профиль на порту %d имеет тип %r вместо %s"
                                     % (port, node.get("type"), TYPES[label]))
                nodes[label] = node
    if not nodes:
        raise SystemExit("В подписке нет профилей для указанного VPS")
    # Каждый запрошенный уровень должен иметь шанс задействовать ВСЕ потоки.
    # Иначе ранние протоколы съедят бюджет, а тесты в 32/64 потока превратятся
    # в фиктивные тесты с 1-2 реальными запросами.
    payload_bytes = a.payload_kib * 1024
    total_requests = (a.max_mib * 1024 * 1024) // payload_bytes
    min_requests = len(nodes) * sum(levels)
    if total_requests < min_requests:
        import math
        needed = math.ceil(min_requests * payload_bytes / (1024 * 1024))
        raise SystemExit(
            "Недостаточно лимита для проверки всех профилей на заданных уровнях. "
            "Требуется --max-mib не меньше %d либо уменьшите --levels." % needed)
    names = [n["name"] for n in nodes.values()]
    if len(names) != len(set(names)):
        raise SystemExit("Одинаковые имена прокси: API не сможет выбирать их однозначно")
    tag = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    runid = secrets.token_hex(8)
    output = Path(a.output).expanduser() if a.output else (Path.home() / "test-tspu-results" / (tag + "-" + a.network + "-" + runid[:6]))
    if output.exists():
        raise SystemExit("Каталог результатов уже существует: " + str(output))
    output.mkdir(mode=0o700, parents=True)
    output.chmod(0o700)
    home = output / ".mihomo-home"
    home.mkdir(mode=0o700)
    ctrl_secret = secrets.token_hex(24)
    broken = "__TEST_BROKEN_SOCKS5__"
    if broken in names:
        raise SystemExit("Имя тестового неработающего профиля занято")
    cfg = {"mixed-port": 17890, "allow-lan": False, "mode": "rule",
           "external-controller": "127.0.0.1:19090", "secret": ctrl_secret,
           "log-level": "error",
           "proxies": list(nodes.values()) + [{"name": broken, "type": "socks5",
                                                "server": "127.0.0.1", "port": 1}],
           "proxy-groups": [{"name": "FIELD", "type": "select",
                             "proxies": names + ["DIRECT", broken]}],
           "rules": ["MATCH,FIELD"]}
    conf = output / "private-mihomo.yaml"
    conf.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    conf.chmod(0o600)
    cmd = [str(MIHOMO), "-d", str(home), "-f", str(conf)]
    print("Проверяю конфигурацию Mihomo (секреты не печатаются)...", flush=True)
    validated = subprocess.run(cmd[:1] + ["-t"] + cmd[1:], capture_output=True, text=True, timeout=45)
    if validated.returncode:
        raise SystemExit("Mihomo не принял конфигурацию. Лог сохранён только локально: "
                         + str(conf))
    dest = a.ssh_user + "@" + server_ip
    remote = "/tmp/test-tspu-field-" + runid
    secs = int(len(nodes) * len(levels) * (a.seconds + 15) + 180)
    if secs > 7200:
        raise SystemExit("Слишком длинный тест для защитного таймера VPS")
    print("Сеть: " + a.network + " (метка указана вручную; проверьте Wi-Fi/мобильные данные)")
    print("SSH — только управление VPS; тестовый трафик идёт с телефона через VPN.")
    started_session = False
    process = None
    rows = []
    samples = []
    controls = {"DIRECT": "not_run", "BROKEN_SOCKS5": "not_run"}
    remote_error = ""
    budget = total_requests
    future_levels = [level for _, label in PORTS if label in nodes for level in levels]
    started_at = now()
    try:
        run_ssh(dest, a.ssh_port, "mkdir -m 700 -p " + remote)
        for name in ("origin.py", "server_monitor.py", "port_capture.py", "vps_session.py"):
            remote_scp(a.ssh_port, ROOT / name, dest + ":" + remote + "/" + name)
        response = run_ssh(dest, a.ssh_port,
                           "python3 " + remote + "/vps_session.py start --dir " + remote
                           + " --duration " + str(secs) + " --size " + str(payload_bytes)
                           + " --capture", timeout=30)
        info = json.loads(response)
        started_session = True
        expected = info["sha256"]
        if not info.get("capture"):
            raise RuntimeError("VPS не подтвердил независимый сбор пакетов")
        logfile = (output / "mihomo.log").open("w")
        try:
            process = subprocess.Popen(cmd, stdout=logfile, stderr=subprocess.STDOUT)
        finally:
            logfile.close()
        for _ in range(50):
            if process.poll() is not None:
                raise RuntimeError("Mihomo завершился до запуска API; смотрите приватный mihomo.log")
            try:
                api("GET", "/proxies/FIELD", ctrl_secret)
                break
            except Exception:
                time.sleep(.2)
        else:
            raise RuntimeError("Локальный API Mihomo не отвечает")
        from urllib.parse import urlsplit
        target = urlsplit("http://127.0.0.1:18180/payload")
        ping_target = urlsplit("http://127.0.0.1:18180/ping")
        proxy = urlsplit("http://127.0.0.1:17890")
        # NEGATIVE CONTROLS: a direct call and a definitely broken outbound
        # MUST NOT download the origin. This catches bypasses/fallbacks.
        for label, control in (("DIRECT", "DIRECT"), ("BROKEN_SOCKS5", broken)):
            api("PUT", "/proxies/FIELD", ctrl_secret, {"name": control})
            if api("GET", "/proxies/FIELD", ctrl_secret).get("now") != control:
                raise RuntimeError("Невозможно выбрать отрицательный контроль " + label)
            probe = request_once(target, proxy, min(3, a.timeout), payload_bytes,
                                 expected, payload_bytes, verify_nonce=True)
            if probe[0]:
                controls[label] = "unexpected_success"
                raise RuntimeError("КРИТИЧЕСКАЯ ОШИБКА: " + label
                                   + " смог получить origin. Тест маршрутизации недостоверен!")
            controls[label] = "failed_as_expected"
            print("Отрицательный контроль " + label + ": отказ подтверждён", flush=True)
        for port, label in PORTS:
            node = nodes.get(label)
            if not node:
                rows.append(fail_row(a.network, label, "", "missing",
                                     "Профиля с этим портом нет в подписке"))
                continue
            name = node["name"]
            protocol_bytes_ok = 0
            try:
                api("PUT", "/proxies/FIELD", ctrl_secret, {"name": name})
                selected = api("GET", "/proxies/FIELD", ctrl_secret)
                if selected.get("now") != name:
                    raise RuntimeError("Mihomo не подтвердил выбранный профиль")
            except Exception as exc:
                rows.append(fail_row(a.network, label, name, "switch_error", str(type(exc).__name__)))
                for skipped in levels:
                    if future_levels and future_levels[0] == skipped:
                        future_levels.pop(0)
                continue
            for level in levels:
                if not future_levels or future_levels[0] != level:
                    raise RuntimeError("Внутренняя ошибка распределения лимита по уровням")
                future_levels.pop(0)
                required_later = sum(future_levels)
                # Минимум level запросов для текущего теста, плюс равная доля
                # свободных запросов; будущие уровни гарантированно сохранят квоту.
                spare = max(0, budget - required_later - level)
                quota = level + spare // (len(future_levels) + 1)
                quota = min(quota, budget - required_later)
                before = vps_counters(dest, a.ssh_port, remote)
                t0 = now()
                level_samples = []
                measurement = run_level(
                    target, proxy, level, a.seconds, a.timeout,
                    payload_bytes, expected, payload_bytes, max_requests=quota,
                    on_sample=lambda r: level_samples.append({"kind": "download", **r}),
                    on_ping_sample=lambda r: level_samples.append({"kind": "ping", **r}),
                    ping_target=ping_target, ping_interval=1.0, verify_nonce=True)
                t1 = now()
                # Packet recorder flushes counters periodically (200 ms).
                time.sleep(.3)
                after = vps_counters(dest, a.ssh_port, remote)
                route = port_evidence(before, after, str(port) + "/" + TRANSPORT[label])
                for record in level_samples:
                    samples.append({"network": a.network, "protocol": label,
                                    "concurrency": level, **record})
                budget -= measurement["requests_total"]
                protocol_bytes_ok += measurement["bytes_ok"]
                # Functional success is not automatically a valid sustained benchmark.
                if not measurement["requests_ok"]:
                    quality = "failed"
                elif route["route_status"] != "verified":
                    quality = "route_" + route["route_status"]
                elif measurement["peak_inflight"] < level:
                    quality = "underfilled_concurrency"
                elif measurement["measurement_status"] == "budget_limited":
                    quality = "budget_limited"
                elif measurement["latency_samples"] < 20:
                    quality = "insufficient_samples"
                elif measurement["app_rtt_samples"] < 20:
                    quality = "insufficient_rtt_samples"
                else:
                    quality = "ok"
                row = {"network": a.network, "protocol": label,
                       "proxy_name": name, "status": quality,
                       "error": "", "date_utc": started_at, "started_utc": t0,
                       "ended_utc": t1, "label": a.network + " / " + label,
                       "concurrency": level, "seconds_configured": a.seconds,
                       "cumulative_mib": round((total_requests - budget)
                                               * payload_bytes / (1024 ** 2), 2),
                       "protocol_cumulative_mib": round(protocol_bytes_ok / (1024 ** 2), 2),
                       **route, **measurement}
                rows.append(row)
                p95 = ("%.0f мс" % row["latency_p95_ms"]
                       if row["latency_p95_ms"] != "" else "н/д (<20 успешных ответов)")
                app95 = (str(row["app_rtt_p95_ms"]) + " мс"
                         if row["app_rtt_p95_ms"] != "" else "н/д (<20 ping)")
                print("%s | %2d потоков | %d/%d OK | %.2f МиБ/с | %.2f/%.2f с | %s | "
                      "порт %s: %d пакетов | ping p95 %s | загрузка p95 %s" %
                      (label, level, row["requests_ok"], row["requests_total"],
                       row["throughput_mib_s"], row["seconds_elapsed"], a.seconds,
                       quality, route["route_port"], route["route_packets"], app95, p95),
                      flush=True)
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        remote_error = "%s: %s" % (type(exc).__name__, str(exc)[:180])
        print("Ошибка теста: " + remote_error, file=sys.stderr)
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                process.kill()
        if started_session:
            try:
                run_ssh(dest, a.ssh_port,
                        "python3 " + remote + "/vps_session.py stop --dir " + remote)
                remote_scp(a.ssh_port, dest + ":" + remote + "/server-resources.csv",
                           output / "server-resources.csv")
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                remote_error += " Монитор VPS недоступен: " + type(exc).__name__
        (output / "negative-controls.json").write_text(
            json.dumps(controls, ensure_ascii=False, indent=2))
        with (output / "request-samples.csv").open("w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=SAMPLE_FIELDS)
            writer.writeheader()
            writer.writerows(samples)
        with (output / "measurements.csv").open("w", newline="") as fp:
            writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        manifest = {"date_utc": started_at, "network_label_user_declared": a.network,
                    "vps_ipv4": server_ip, "android_client": "Mihomo",
                    "client_version": subprocess.run([str(MIHOMO), "-v"], capture_output=True,
                                                     text=True, timeout=10).stdout.strip()[:130],
                    "protocols_expected": [label for _, label in PORTS],
                    "profiles_found": len(nodes), "levels": levels,
                    "seconds_per_level": a.seconds, "max_mib": a.max_mib,
                    "payload_bytes": payload_bytes, "ping_interval_seconds": 1,
                    "min_p95_samples": 20,
                    "min_p99_samples": 100,
                    "route_evidence": "VPS AF_PACKET ingress ports",
                    "negative_controls": controls,
                    "error": remote_error}
        (output / "environment.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        from generate_report import generate
        generate(output)
        print("Отчёт: " + str(output / "report.md"))
        print("CSV: " + str(output / "measurements.csv"))
        print("Сырые запросы: " + str(output / "request-samples.csv"))
        print("Контроли: " + str(output / "negative-controls.json"))
        print("CPU/RAM: " + str(output / "server-resources.csv"))
        print("Секретный private-mihomo.yaml остаётся ТОЛЬКО в приватном каталоге телефона.")
        if any(row.get("status") != "ok" for row in rows):
            print("ВНИМАНИЕ: часть уровней — пробные/недостаточные измерения. "
                  "Для сравнимого бенчмарка увеличьте --seconds и --max-mib; "
                  "см. статусы и фактическое время в отчёте.")
        if remote_error or not any(int(row.get("requests_ok", 0)) > 0 for row in rows):
            raise SystemExit("Прогон неполный либо нет успешной передачи данных. "
                             "Отчёт сохраняет ошибки; исправьте их и повторите.")


def main():
    p = argparse.ArgumentParser(description="Полевые измерения с Android без USB-модема")
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("configure", help="однократно сохранить URL подписки без эха и shell history")
    run_p = sub.add_parser("run", help="одним запуском проверить все профили")
    run_p.add_argument("--vps", required=True, help="IPv4 реального сервера")
    run_p.add_argument("--network", required=True, help="sbermobile или rostelecom; указывается вручную")
    run_p.add_argument("--ssh-user", default="root")
    run_p.add_argument("--ssh-port", type=int, default=22)
    run_p.add_argument("--levels", default="1,8,32,64")
    run_p.add_argument("--payload-kib", type=int, default=256, choices=(64, 128, 256, 512, 1024, 2048, 4096),
                       help="размер случайной HTTP-нагрузки одного запроса, КиБ (64..4096)")
    run_p.add_argument("--seconds", type=float, default=6)
    run_p.add_argument("--timeout", type=float, default=10)
    run_p.add_argument("--max-mib", type=int, default=256,
                       help="верхний предел полезной нагрузки в МиБ")
    run_p.add_argument("--output", help="приватный каталог для CSV/отчёта")
    a = p.parse_args()
    if a.action == "configure":
        configure()
    else:
        run(a)


if __name__ == "__main__":
    main()

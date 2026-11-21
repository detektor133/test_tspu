#!/usr/bin/env python3
"""Rostelecom-only external IP check: phone DIRECT / VPS / 7 Mihomo paths.

HTTPS echo observations only; not a system-wide DNS/WebRTC leak test.
"""
import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time

import yaml

from ip_leak_probe import SERVICES, all_probes, compare, visible
from phone import (MIHOMO, PORTS, ROOT, api, remote_scp, run_ssh,
                   subscription, validate_vps)
from youtube_test import BROKEN, LOCAL_PROXY, broken_proxy_fails, select_profiles

CSV_FIELDS = ("route", "service", "family", "observed_ip",
              "status", "error_category", "comparison_to_vps")


def read_vps_egress(vps, ssh_user, ssh_port, timeout):
    """Run same pure-stdlib public IP probes on VPS, clean remote temp files."""
    remote = "/tmp/test-tspu-ip-" + secrets.token_hex(6)
    dest = ssh_user + "@" + vps
    try:
        run_ssh(dest, ssh_port, "mkdir -m 700 -p " + remote)
        remote_scp(ssh_port, ROOT / "ip_leak_probe.py",
                   dest + ":" + remote + "/ip_leak_probe.py")
        response = run_ssh(
            dest, ssh_port,
            "python3 " + remote + "/ip_leak_probe.py --timeout " + str(timeout),
            timeout=3 * timeout + 25)
        records = json.loads(response)
        if not isinstance(records, dict):
            raise ValueError("Некорректный ответ VPS")
        for name, _, family in SERVICES:
            if name not in records or not isinstance(records[name], dict):
                raise ValueError("VPS не вернул все IP-сервисы")
            if not isinstance(records[name].get("ip", ""), str):
                raise ValueError("VPS вернул некорректный IP")
        return records
    finally:
        try:
            run_ssh(dest, ssh_port, "rm -rf -- " + remote, timeout=12)
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass


def write_report(folder, direct, vps, routes, comparisons, control_ok, vps_error):
    lines = [
        "# Проверка внешнего IP и возможных утечек: Ростелеком",
        "",
        "Проверены прямой HTTPS с телефона, HTTPS с VPS по SSH "
        "и HTTPS через каждый из семи маршрутов Mihomo. "
        "Два независимых IPv4-сервиса: ipify и icanhazip; "
        "дополнительно IPv6-only сервис ipify.",
        "",
        "Важно: здесь проверяется IP видимый конкретным HTTPS-сервисам "
        "через HTTP-прокси Termux. Не проверяются системные маршруты "
        "Android, WebRTC, DNS-утечки и трафик других приложений.",
        "",
        "В отчёте содержится реальный публичный IP Ростелекома "
        "(возможно, общий CGNAT). Не публикуйте без проверки.",
        "",
        "Контроль сломанного SOCKS5: " +
        ("отказ подтверждён" if control_ok else "НЕ ПОДТВЕРЖДЁН"),
        "VPS reference: " + ("доступен" if not vps_error else "недоступен: " + vps_error),
        "",
        "| Маршрут | Внешний IPv4 | Внешний IPv6 | Результат |",
        "|---|---|---|---|",
        "| DIRECT | %s | %s | прямое соединение |" %
        (visible(direct, 4), visible(direct, 6)),
        "| VPS | %s | %s | исходящий IP VPS |" %
        (visible(vps, 4), visible(vps, 6)),
    ]
    for name, observations in routes.items():
        lines.append("| %s | %s | %s | %s |" %
                     (name, visible(observations, 4), visible(observations, 6),
                      comparisons[name][0]))
    lines.extend([
        "",
        "vps_egress_confirmed: два независимых IPv4-сервиса видят "
        "адрес исходящего VPS; IPv6, если получен, также совпадает.",
        "possible_ip_leak: сервис увидел прямой IP Ростелекома "
        "вместо IP VPS. Это признак возможной утечки.",
        "unexpected_egress: внешний IP отличается и от ожидаемого "
        "IP VPS, и от установленного прямого IP.",
        "inconclusive: нет достаточных ответов или подтверждения VPS.",
        "",
        "Одна ошибка IPv6 не означает утечки, а совпадение IPv4 "
        "не доказывает отсутствие системных IPv6/WebRTC/DNS-утечек.",
        "Детальные ответы сервисов: ip-results.csv. "
        "Приватные настройки Mihomo в отчёт не попадают.",
        "",
    ])
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Автотест внешнего IP без ручных действий")
    parser.add_argument("--vps", required=True)
    parser.add_argument("--network", required=True, choices=("rostelecom",))
    parser.add_argument("--timeout", type=int, default=8)
    parser.add_argument("--ssh-user", default="root")
    parser.add_argument("--ssh-port", type=int, default=22)
    args = parser.parse_args()
    if not 3 <= args.timeout <= 20 or not 1 <= args.ssh_port <= 65535:
        parser.error("--timeout 3..20, --ssh-port 1..65535")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", args.ssh_user):
        parser.error("Некорректное имя SSH-пользователя")
    if not MIHOMO.is_file() or not os.access(MIHOMO, os.X_OK):
        raise SystemExit("Не установлен Mihomo: bench/setup_termux.sh")

    os.umask(0o077)
    vps_address = validate_vps(args.vps)
    subscription_cfg, sub_host = subscription()
    if sub_host != vps_address:
        raise SystemExit("IP подписки не совпадает с --vps")
    try:
        nodes = select_profiles(subscription_cfg, vps_address)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    now = dt.datetime.now(dt.timezone.utc)
    folder = Path.home() / "test-tspu-results" / (
        now.strftime("%Y%m%d-%H%M%S") + "-rostelecom-ip-" + secrets.token_hex(3))
    folder.mkdir(mode=0o700, parents=True)
    print("Только Wi-Fi Ростелекома. Отключи мобильные данные и системный VPN.")
    print("Проверяю исходящий IP VPS, DIRECT и семь VPN по 3 HTTPS-сервисам.")
    try:
        vps = read_vps_egress(vps_address, args.ssh_user, args.ssh_port, args.timeout)
        vps_error = ""
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        vps = {}
        vps_error = type(exc).__name__
        print("Не удалось подтвердить IP VPS: вывод по утечкам будет неопределённым.")
    direct = all_probes(proxy=None, timeout=args.timeout)
    print("DIRECT  IPv4:", visible(direct, 4), "| IPv6:", visible(direct, 6))
    print("VPS     IPv4:", visible(vps, 4), "| IPv6:", visible(vps, 6))
    proc = None
    control_ok = False
    routes = {}
    comparisons = {}
    try:
        with tempfile.TemporaryDirectory(prefix="private-ip-", dir=folder) as tmp:
            tmp = Path(tmp)
            home = tmp / "mihomo-home"
            home.mkdir(mode=0o700)
            secret = secrets.token_hex(24)
            selected = [nodes[label] for _, label in PORTS]
            config = {
                "mixed-port": 17890, "allow-lan": False,
                "mode": "rule", "external-controller": "127.0.0.1:19090",
                "secret": secret, "log-level": "error",
                "proxies": selected + [{
                    "name": BROKEN, "type": "socks5",
                    "server": "127.0.0.1", "port": 1,
                }],
                "proxy-groups": [{
                    "name": "FIELD", "type": "select",
                    "proxies": [x["name"] for x in selected] + ["DIRECT", BROKEN],
                }],
                "rules": ["MATCH,FIELD"],
            }
            conf = tmp / "private-mihomo.yaml"
            conf.write_text(yaml.safe_dump(
                config, allow_unicode=True, sort_keys=False), encoding="utf-8")
            conf.chmod(0o600)
            cmd = [str(MIHOMO), "-d", str(home), "-f", str(conf)]
            valid = subprocess.run(
                cmd[:1] + ["-t"] + cmd[1:], capture_output=True, timeout=45)
            if valid.returncode:
                raise RuntimeError("Mihomo не принял конфигурацию")
            proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(50):
                if proc.poll() is not None:
                    raise RuntimeError("Mihomo завершился при старте")
                try:
                    api("GET", "/proxies/FIELD", secret)
                    break
                except (OSError, ValueError):
                    time.sleep(.2)
            else:
                raise RuntimeError("API Mihomo не отвечает (возможно, порт занят)")

            def switch(name):
                api("PUT", "/proxies/FIELD", secret, {"name": name})
                if api("GET", "/proxies/FIELD", secret).get("now") != name:
                    raise RuntimeError("Не удалось подтвердить маршрут")
                time.sleep(.4)

            switch(BROKEN)
            control_ok = broken_proxy_fails(args.timeout)
            if not control_ok:
                raise RuntimeError("КРИТИЧНО: сломанный SOCKS5 вышел в Интернет")
            print("BROKEN SOCKS5: ожидаемый отказ подтверждён")
            for port, label in PORTS:
                switch(nodes[label]["name"])
                checks = all_probes(proxy=LOCAL_PROXY, timeout=args.timeout)
                routes[label] = checks
                comparisons[label] = compare(checks, direct, vps)
                print(label, "| IPv4", visible(checks, 4),
                      "| IPv6", visible(checks, 6),
                      "|", comparisons[label][0], flush=True)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if routes:
            rows = []
            for name, observations in (("DIRECT", direct), ("VPS", vps),
                                       *list(routes.items())):
                per_service = comparisons.get(name, ("", {}))[1]
                for service, _, family in SERVICES:
                    data = observations.get(service, {})
                    rows.append({
                        "route": name, "service": service,
                        "family": family, "observed_ip": data.get("ip", ""),
                        "status": data.get("status", "no_reference"),
                        "error_category": data.get("error", ""),
                        "comparison_to_vps": per_service.get(service, ""),
                    })
            with (folder / "ip-results.csv").open(
                    "w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=CSV_FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            (folder / "environment.json").write_text(json.dumps({
                "test_type": "termux_http_proxy_ip_egress",
                "network_label_user_declared": "rostelecom",
                "date_utc": now.isoformat(), "vps_ipv4": vps_address,
                "broken_control_ok": control_ok,
                "contains_network_public_ip": True,
                "excludes": ["Android system VPN", "WebRTC", "DNS leaks", "other apps"],
                "vps_reference_error": vps_error,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            write_report(folder, direct, vps, routes, comparisons, control_ok, vps_error)
            print("Отчёт: " + str(folder / "report.md"))
            print("CSV: " + str(folder / "ip-results.csv"))
            print("ВНИМАНИЕ: архив содержит публичный IP вашей сети.")
            print("Для скачивания: python bench/download_results.py " + str(folder))


if __name__ == "__main__":
    main()

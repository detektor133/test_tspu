#!/usr/bin/env python3
"""Rostelecom-only YouTube access experiment: DIRECT versus seven Mihomo outbounds.

Probes verified YouTube watch HTML and a small byte range from real
*.googlevideo.com media via yt-dlp. Does not automate Android playback.
"""
import argparse
import csv
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time

import yaml

from phone import MIHOMO, PORTS, TYPES, api, subscription, validate_vps
from youtube_probe import FIELDS, VIDEO_ID_RE, probe, utcnow, _opener
from urllib import request

LOCAL_PROXY = "http://127.0.0.1:17890"
BROKEN = "__YOUTUBE_BROKEN_SOCKS5__"


def select_profiles(cfg, server_ip):
    """Refuse missing, duplicate or mislabelled protocols."""
    profiles = {}
    for node in cfg["proxies"]:
        if not isinstance(node, dict) or node.get("server") != server_ip:
            continue
        for port, label in PORTS:
            if node.get("port") != port:
                continue
            if label in profiles:
                raise ValueError("Несколько профилей для " + label)
            if node.get("type") != TYPES[label]:
                raise ValueError("Неверный тип профиля " + label)
            if label == "VLESS REALITY" and (
                node.get("flow") != "xtls-rprx-vision"
                or not isinstance(node.get("reality-opts"), dict)
            ):
                raise ValueError("VLESS REALITY не содержит XTLS Vision/REALITY")
            profiles[label] = node
    missing = [label for _, label in PORTS if label not in profiles]
    if missing:
        raise ValueError("Не найдены профили: " + ", ".join(missing))
    names = [node["name"] for node in profiles.values()]
    if len(set(names)) != len(names) or BROKEN in names:
        raise ValueError("Повторяющиеся/зарезервированные имена прокси")
    return profiles


def broken_proxy_fails(timeout):
    """Must fail against a public TLS origin; not dependent on blocked YouTube."""
    req = request.Request("https://example.com/", headers={
        "User-Agent": "test-tspu-control/1", "Cache-Control": "no-cache"})
    try:
        with _opener(LOCAL_PROXY).open(req, timeout=timeout) as resp:
            resp.read(1024)
        return False
    except (OSError, ValueError):
        return True


def output_report(folder, rows, video_id, ctrl_ok, version):
    headings = ("| Маршрут | YouTube HTML | Извлечение видео | "
                "Googlevideo | Байты | Время передачи, мс | Причина |")
    lines = [
        "# Ростелеком: YouTube через DIRECT и 7 VPN-профилей",
        "",
        "Видео: https://www.youtube.com/watch?v=" + video_id,
        "",
        "Проверка: отдельный GET страницы youtube.com, затем получение "
        "прямой ссылки yt-dlp на фиксированный формат **itag 18** "
        "(прогрессивное видео, обычно MP4 360p), затем HTTPS Range GET "
        "до 1 МиБ с **.googlevideo.com**. Полученные видеобайты "
        "не сохраняются. Это **тест доставки настоящих видеоданных**, "
        "**не** автоматическое воспроизведение в приложении YouTube.",
        "",
        "Сеть указана вручную: Rostelecom (Wi-Fi). "
        "Отключите мобильные данные и системный VPN.",
        "Для VPN-маршрута использован HTTP proxy локального Mihomo "
        "с проверкой текущего выбранного имени и отрицательным "
        "контролем BROKEN_SOCKS5. Независимого захвата пакетов "
        "на сервере в этом режиме нет.",
        "Отрицательный контроль: " + ("отказ подтверждён" if ctrl_ok
                                     else "НЕ ПОДТВЕРЖДЁН"),
        "Версия yt-dlp: " + version,
        "",
        headings,
        "|---|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s |" % (
            row["protocol"], row["watch_status"], row["extract_status"],
            row["media_status"], row["media_bytes"], row["media_ms"],
            row["error_category"] or "—"))
    direct = rows[0]
    vpn_worked = [r["protocol"] for r in rows[1:]
                  if r["media_status"] == "media_bytes_ok"]
    lines.extend(["", "## Вывод", ""])
    if ctrl_ok and direct["media_status"] != "media_bytes_ok" and vpn_worked:
        lines.append("Наблюдение: DIRECT не получил видеобайты, а через "
                     "следующие VPN-маршруты они получены: " +
                     ", ".join(vpn_worked) + ". Это свидетельствует о "
                     "практической доступности видеоданных через эти "
                     "маршруты в момент эксперимента.")
    elif direct["media_status"] == "media_bytes_ok":
        lines.append("DIRECT тоже получил видеобайты. В этом прогоне "
                     "эффект обхода недоступности YouTube не подтверждён.")
    else:
        lines.append("Обход доставки видеоданных не подтверждён: "
                     "нет успешной VPN-пробы или неполны контроли.")
    lines.extend([
        "",
        "**Не является доказательством блокировки ТСПУ:** сбои "
        "yt-dlp могут быть вызваны антибот-проверками YouTube, "
        "PO Token/EJS, отсутствием itag 18, геоограничениями, "
        "TLS/сетевыми сбоями или изменением YouTube API. "
        "HTTPS 200 на странице не доказывает работу плеера.",
        "Для доказательства **воспроизведения** дополнительно "
        "зафиксируйте ручную проверку одного и того же видео "
        "в браузере/плеере через явно настроенный VPN-маршрут.",
        "",
        "Первичные данные: youtube-results.csv и environment.json. "
        "Секретные URL подписок, видео CDN и личные конфиги не экспортируются.",
        "",
    ])
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="YouTube: DIRECT vs 7 VPN на Wi-Fi Ростелекома")
    parser.add_argument("--vps", required=True)
    parser.add_argument("--network", required=True, choices=("rostelecom",))
    parser.add_argument("--video-id", default="jNQXAC9IVRw",
                        help="11-символьный ID публичного видео YouTube")
    parser.add_argument("--timeout", type=int, default=12)
    parser.add_argument("--max-bytes", type=int, default=1048576)
    a = parser.parse_args()
    if not VIDEO_ID_RE.fullmatch(a.video_id):
        parser.error("--video-id: ровно 11 символов A-Za-z0-9_-")
    if not 5 <= a.timeout <= 30 or not 65536 <= a.max_bytes <= 2097152:
        parser.error("--timeout 5..30; --max-bytes 65536..2097152")
    if importlib.util.find_spec("yt_dlp") is None:
        raise SystemExit("Не найден yt-dlp. Установите в Termux: python -m pip install -U 'yt-dlp[default]'")
    if not MIHOMO.is_file():
        raise SystemExit("Нет Mihomo. Сначала установите стенд bench/setup_termux.sh")
    os.umask(0o077)
    ip = validate_vps(a.vps)
    subscribed, host = subscription()
    if host != ip:
        raise SystemExit("IP подписки не совпадает с --vps")
    try:
        profiles = select_profiles(subscribed, ip)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    tag = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = (Path.home() / "test-tspu-results" /
              (tag + "-rostelecom-youtube-" + secrets.token_hex(3)))
    folder.mkdir(mode=0o700, parents=True)
    rows = []
    started = utcnow()
    proc = None
    control_ok = False
    yt_version = "not_available"
    print("Только Wi-Fi Ростелекома: отключи мобильные данные и системный VPN.")
    print("Тест: HTTPS страница YouTube + до %.1f МиБ googlevideo через каждый маршрут."
          % (a.max_bytes / 1048576))
    try:
        version = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--version"],
            capture_output=True, text=True, timeout=10)
        if version.returncode == 0:
            yt_version = version.stdout.strip()[:80]
        with tempfile.TemporaryDirectory(prefix="private-", dir=folder) as scratch:
            scratch = Path(scratch)
            home = scratch / "mihomo-home"
            home.mkdir(mode=0o700)
            ctrl_secret = secrets.token_hex(24)
            ordered = [profiles[label] for _, label in PORTS]
            cfg = {
                "mixed-port": 17890, "allow-lan": False, "mode": "rule",
                "external-controller": "127.0.0.1:19090",
                "secret": ctrl_secret, "log-level": "error",
                "proxies": ordered + [{
                    "name": BROKEN, "type": "socks5",
                    "server": "127.0.0.1", "port": 1,
                }],
                "proxy-groups": [{
                    "name": "FIELD", "type": "select",
                    "proxies": [x["name"] for x in ordered] + ["DIRECT", BROKEN],
                }],
                "rules": ["MATCH,FIELD"],
            }
            conf = scratch / "config.yaml"
            conf.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
            conf.chmod(0o600)
            cmd = [str(MIHOMO), "-d", str(home), "-f", str(conf)]
            validation = subprocess.run(cmd[:1] + ["-t"] + cmd[1:],
                                        capture_output=True, timeout=45)
            if validation.returncode:
                raise RuntimeError("Mihomo не принял конфигурацию")
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
            for attempt in range(50):
                if proc.poll() is not None:
                    raise RuntimeError("Mihomo завершился при запуске")
                try:
                    api("GET", "/proxies/FIELD", ctrl_secret)
                    break
                except (OSError, ValueError):
                    time.sleep(.2)
            else:
                raise RuntimeError("Mihomo API не отвечает: проверьте порт 19090")

            def select(name):
                api("PUT", "/proxies/FIELD", ctrl_secret, {"name": name})
                if api("GET", "/proxies/FIELD", ctrl_secret).get("now") != name:
                    raise RuntimeError("Mihomo не подтвердил маршрут")
                time.sleep(.5)

            select(BROKEN)
            control_ok = broken_proxy_fails(a.timeout)
            if not control_ok:
                raise RuntimeError("BROKEN SOCKS5 обошёл прокси: эксперимент недостоверен")
            print("BROKEN_SOCKS5: отказ подтверждён")
            select("DIRECT")
            # DIRECT explicitly bypasses the local proxy, independent of group state.
            direct = probe(a.video_id, None, a.timeout, a.max_bytes)
            rows.append({"network": a.network, "protocol": "DIRECT", "port": "",
                         **direct})
            print("DIRECT | страница %s | видео %s | %d байт" %
                  (direct["watch_status"], direct["media_status"], direct["media_bytes"]),
                  flush=True)
            for port, label in PORTS:
                select(profiles[label]["name"])
                values = probe(a.video_id, LOCAL_PROXY, a.timeout, a.max_bytes)
                rows.append({"network": a.network, "protocol": label,
                             "port": port, **values})
                print("%s | страница %s | видео %s | %d байт | %s" %
                      (label, values["watch_status"], values["media_status"],
                       values["media_bytes"], values["error_category"] or "—"),
                      flush=True)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if rows:
            with (folder / "youtube-results.csv").open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=FIELDS)
                writer.writeheader()
                writer.writerows(rows)
            meta = {
                "test_type": "youtube_media_bytes_not_player",
                "network_label_user_declared": "rostelecom",
                "date_utc": started,
                "vps_ipv4": ip,
                "video_id": a.video_id,
                "format_id": "18",
                "max_media_bytes_per_route": a.max_bytes,
                "yt_dlp_version": yt_version,
                "negative_broken_socks5": control_ok,
                "mode": "youtube",
                "routes": [r["protocol"] for r in rows],
            }
            (folder / "environment.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            output_report(folder, rows, a.video_id, control_ok, yt_version)
            print("Отчёт: " + str(folder / "report.md"))
            print("CSV: " + str(folder / "youtube-results.csv"))
            print("Экспорт: python bench/export_results.py " + str(folder))
    if not control_ok:
        raise SystemExit("Отрицательный контроль не подтверждён")


if __name__ == "__main__":
    main()

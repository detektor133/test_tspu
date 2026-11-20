#!/usr/bin/env python3
"""Detached loopback origin and CPU/RSS recorder on a VPS, controlled over SSH.

Run only for your own test server. No listener is exposed outside 127.0.0.1.
"""
import argparse
import json
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

SAFE_DIR = re.compile(r"^/tmp/test-tspu-field-[a-f0-9]{16,32}$")


def directory(raw):
    if not SAFE_DIR.fullmatch(raw):
        raise SystemExit("Небезопасный путь сеанса; нужен /tmp/test-tspu-field-<hex>")
    path = pathlib.Path(raw)
    if path.is_symlink():
        raise SystemExit("Каталог сеанса не должен быть символьной ссылкой")
    return path


def alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def terminate(pid):
    if pid and alive(pid):
        try:
            os.kill(int(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass


def read_state(root):
    p = root / "session.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def start(root, duration, size):
    if not (5 <= duration <= 7200 and 1024 <= size <= 16 * 1024 * 1024):
        raise SystemExit("Неверная длительность или размер данных")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    if (root / "session.json").exists():
        raise SystemExit("Этот сеанс уже создавался")
    here = pathlib.Path(__file__).resolve().parent
    origin_log = (root / "origin.log").open("w")
    origin = subprocess.Popen(
        [sys.executable, str(here / "origin.py"), "--port", "18180",
         "--size", str(size)],
        stdin=subprocess.DEVNULL, stdout=origin_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    origin_log.close()
    digest = None
    for _ in range(60):
        if origin.poll() is not None:
            break
        text = (root / "origin.log").read_text()
        match = re.search(r"sha256=([0-9a-f]{64})", text)
        if match:
            digest = match.group(1)
            break
        time.sleep(0.1)
    if not digest:
        terminate(origin.pid)
        raise SystemExit("Origin не запустился (возможно, порт 18180 занят)")
    monitor_log = (root / "monitor.log").open("w")
    monitor = subprocess.Popen(
        [sys.executable, str(here / "server_monitor.py"),
         "--duration", str(duration), "--interval", "1",
         "--output", str(root / "server-resources.csv")],
        stdin=subprocess.DEVNULL, stdout=monitor_log, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    monitor_log.close()
    state = {"origin_pid": origin.pid, "monitor_pid": monitor.pid,
             "sha256": digest, "size": size, "duration": duration}
    (root / "session.json").write_text(json.dumps(state))
    (root / "session.json").chmod(0o600)
    watchdog_log = (root / "watchdog.log").open("w")
    subprocess.Popen(
        [sys.executable, str(pathlib.Path(__file__).resolve()), "guard",
         "--dir", str(root), "--duration", str(duration)],
        stdin=subprocess.DEVNULL, stdout=watchdog_log,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    watchdog_log.close()
    print(json.dumps({"sha256": digest, "size": size,
                      "port": 18180}), flush=True)


def stop(root):
    state = read_state(root)
    # Достаточно SIGTERM: CSV монитор записывает и flush-ит на каждом шаге.
    terminate(state.get("origin_pid"))
    terminate(state.get("monitor_pid"))
    print(json.dumps({"stopped": True,
                      "monitor_available": (root / "server-resources.csv").exists()}))


def guard(root, duration):
    # Завершить origin даже при отключении телефона/обрыве SSH.
    time.sleep(max(1, duration))
    state = read_state(root)
    terminate(state.get("origin_pid"))
    terminate(state.get("monitor_pid"))


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=("start", "stop", "guard"))
    p.add_argument("--dir", required=True)
    p.add_argument("--duration", type=int, default=240)
    p.add_argument("--size", type=int, default=262144)
    a = p.parse_args()
    root = directory(a.dir)
    if a.action == "start":
        start(root, a.duration, a.size)
    elif a.action == "stop":
        stop(root)
    else:
        guard(root, a.duration)


if __name__ == "__main__":
    main()

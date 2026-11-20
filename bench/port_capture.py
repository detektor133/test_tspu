#!/usr/bin/env python3
"""Passive, payload-free VPS ingress evidence for the seven test transports.

Capture Linux AF_PACKET / SOCK_DGRAM IPv4 packets delivered to local sockets.
This runs on the user's own VPS. It NEVER records payloads, source addresses,
private profile credentials or individual packet timestamps. No sniffing on phone.
A port hit is independent evidence, not cryptographic proof of attribution if
other users share the same listener simultaneously.
"""
import argparse
import json
import os
from pathlib import Path
import select
import socket
import struct
import tempfile
import time

PORTS = {
    "443/tcp", "11443/tcp", "14443/udp", "8443/udp",
    "8388/tcp", "8388/udp", "9443/tcp", "10443/tcp",
}
ETH_P_IP = 0x0800


def parse_ingress_ipv4(data):
    """Return (port/protocol, IPv4 total bytes) for a TCP/UDP destination."""
    if len(data) < 20 or data[0] >> 4 != 4:
        return None
    header_length = (data[0] & 15) * 4
    if header_length < 20 or len(data) < header_length + 4:
        return None
    # Only the initial fragment contains the transport port.
    if struct.unpack_from("!H", data, 6)[0] & 0x1FFF:
        return None
    proto = {6: "tcp", 17: "udp"}.get(data[9])
    if not proto:
        return None
    port = struct.unpack_from("!H", data, header_length + 2)[0]
    key = f"{port}/{proto}"
    if key not in PORTS:
        return None
    total = struct.unpack_from("!H", data, 2)[0]
    if total < header_length:
        return None
    return key, total


def port_evidence(before, after, expected):
    """Independent ingress observation; fail closed when counters go backwards."""
    if not (before.get("available") and after.get("available")):
        raise RuntimeError("Счётчик пакетов VPS недоступен")
    a, b = before.get("counters", {}), after.get("counters", {})
    delta = {}
    for port in b:
        x, y = a.get(port, {}), b[port]
        p = int(y.get("packets", 0)) - int(x.get("packets", 0))
        z = int(y.get("ip_bytes", 0)) - int(x.get("ip_bytes", 0))
        if p < 0 or z < 0:
            raise RuntimeError("Счётчики VPS сбросились во время теста")
        delta[port] = (p, z)
    if expected not in delta:
        raise RuntimeError("Нет счётчика для " + expected)
    others = sum(p for key, (p, _) in delta.items() if key != expected)
    packets, ip_bytes = delta[expected]
    return {"route_status": ("verified" if packets and not others else
                             "ambiguous" if packets else "no_ingress"),
            "route_port": expected,
            "route_packets": packets, "route_ip_bytes": ip_bytes,
            "unexpected_port_packets": others}



def save_state(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".port-counter-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(payload, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def record(out, duration, interval):
    counters = {key: {"packets": 0, "ip_bytes": 0} for key in sorted(PORTS)}
    state = {"available": False, "reason": "initializing", "counters": counters}
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM,
                             socket.htons(ETH_P_IP))
        sock.setblocking(False)
    except (OSError, AttributeError) as exc:
        save_state(out, {**state, "reason": f"AF_PACKET: {type(exc).__name__}"})
        return 2
    state.update(available=True, reason="")
    deadline = time.monotonic() + duration
    next_write = time.monotonic()
    try:
        save_state(out, state)
        while time.monotonic() < deadline:
            wait = min(interval, max(0.0, deadline - time.monotonic()),
                       max(0.0, next_write - time.monotonic()))
            ready, _, _ = select.select([sock], [], [], wait)
            if ready:
                while True:
                    try:
                        data, address = sock.recvfrom(65535)
                    except BlockingIOError:
                        break
                    # Linux PACKET_HOST == 0; ignore outgoing and other-host packets.
                    if len(address) < 3 or address[2] != socket.PACKET_HOST:
                        continue
                    parsed = parse_ingress_ipv4(data)
                    if parsed:
                        key, size = parsed
                        counters[key]["packets"] += 1
                        counters[key]["ip_bytes"] += size
            if time.monotonic() >= next_write:
                save_state(out, state)
                next_write = time.monotonic() + interval
    finally:
        save_state(out, state)
        sock.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--interval", type=float, default=.2)
    a = ap.parse_args()
    if not (1 <= a.duration <= 7200 and .05 <= a.interval <= 5):
        ap.error("invalid duration or polling interval")
    return record(a.output, a.duration, a.interval)


if __name__ == "__main__":
    raise SystemExit(main())

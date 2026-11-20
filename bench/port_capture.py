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
# Keep the packet-evidence schema in one place. Android CSV headers reuse
# it, so a new counter cannot silently break result serialization.
EVIDENCE_FIELDS = (
    "route_status", "route_port", "route_packets", "route_ip_bytes",
    "unexpected_port_packets", "noise_threshold_packets",
)
ETH_P_IP = 0x0800


def parse_ingress_ipv4(data, source_bytes=None):
    """Return (port/protocol, IPv4 total bytes) for a TCP/UDP destination."""
    if len(data) < 20 or data[0] >> 4 != 4:
        return None
    if source_bytes is not None and data[12:16] != source_bytes:
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
    if not (before.get("available") and after.get("available")
            and before.get("active") and after.get("active")
            and before.get("source_filtered") and after.get("source_filtered")):
        raise RuntimeError("Счётчик пакетов VPS недоступен, завершён или не привязан к IP телефона")
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
    # Tail packets from the immediately preceding transport are normal:
    # a few TCP FIN/ACK or retransmissions do not imply a proxy fallback.
    # Significant simultaneous activity remains inconclusive.
    noise_limit = max(8, packets // 20)
    if not packets:
        status = "no_ingress"
    elif others == 0:
        status = "verified"
    elif others <= noise_limit:
        status = "verified_with_noise"
    else:
        status = "ambiguous"
    return {"route_status": status,
            "route_port": expected, "route_packets": packets,
            "route_ip_bytes": ip_bytes, "unexpected_port_packets": others,
            "noise_threshold_packets": noise_limit}



def save_state(path, payload):
    # VPS monotonic heartbeat lets independent SSH snapshots detect dead capture.
    payload["updated_monotonic"] = time.monotonic()
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


def record(out, duration, interval, source_ip):
    source_bytes = socket.inet_aton(source_ip)
    counters = {key: {"packets": 0, "ip_bytes": 0} for key in sorted(PORTS)}
    state = {"available": False, "active": False, "source_filtered": True,
             "reason": "initializing", "counters": counters}
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM,
                             socket.htons(ETH_P_IP))
        sock.setblocking(False)
    except (OSError, AttributeError) as exc:
        save_state(out, {**state, "reason": f"AF_PACKET: {type(exc).__name__}"})
        return 2
    state.update(available=True, active=True, reason="")
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
                    parsed = parse_ingress_ipv4(data, source_bytes)
                    if parsed:
                        key, size = parsed
                        counters[key]["packets"] += 1
                        counters[key]["ip_bytes"] += size
            if time.monotonic() >= next_write:
                save_state(out, state)
                next_write = time.monotonic() + interval
    finally:
        state["active"] = False
        save_state(out, state)
        sock.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--duration", type=float, required=True)
    ap.add_argument("--interval", type=float, default=.2)
    ap.add_argument("--source-ip", required=True, help="IPv4 SSH-клиента, не сохраняется")
    a = ap.parse_args()
    if not (1 <= a.duration <= 7200 and .05 <= a.interval <= 5):
        ap.error("invalid duration or polling interval")
    try:
        socket.inet_aton(a.source_ip)
        import ipaddress
        source = str(ipaddress.IPv4Address(a.source_ip))
    except (OSError, ValueError):
        ap.error("--source-ip должен быть IPv4 SSH-клиента")
    return record(a.output, a.duration, a.interval, source)


if __name__ == "__main__":
    raise SystemExit(main())

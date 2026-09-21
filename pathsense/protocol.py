"""
Level 0 — shared constants and wire-format helpers.
Every other module imports from here so the "protocol" (what bytes mean
what) lives in exactly one place. Nothing in this file talks to the
network itself.
"""
import json
import socket
import struct
import time
import uuid

DISCOVERY_MCAST_GROUP = "239.255.10.10"
DISCOVERY_MCAST_PORT = 50900
DISCOVERY_INTERVAL_SEC = 1.0
DISCOVERY_STALE_AFTER_SEC = 5.0

MEASUREMENT_PORT_BASE = 50910
TRANSFER_PORT_BASE = 50920
DASHBOARD_PORT_BASE = 8800

PROBE_TIMEOUT_SEC = 0.5
CHUNK_SIZE_DEFAULT = 64 * 1024  # 64 KiB


def new_device_id() -> str:
    return uuid.uuid4().hex[:12]


def now_ms() -> int:
    return int(time.time() * 1000)


def send_json(sock: socket.socket, obj: dict) -> None:
    payload = json.dumps(obj).encode("utf-8")
    sock.sendall(struct.pack(">I", len(payload)) + payload)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("peer closed connection while reading")
        buf.extend(chunk)
    return bytes(buf)


def recv_json(sock: socket.socket) -> dict:
    (length,) = struct.unpack(">I", recv_exact(sock, 4))
    return json.loads(recv_exact(sock, length).decode("utf-8"))

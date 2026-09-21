"""
Level 1 — Discovery Agent. Uses UDP multicast (verified working on loopback
and on any LAN segment that allows multicast). Each node broadcasts a HELLO
every DISCOVERY_INTERVAL_SEC, listens for others, and forgets a peer after
DISCOVERY_STALE_AFTER_SEC of silence.
"""
from __future__ import annotations

import dataclasses
import json
import socket
import struct
import threading
import time
from typing import Callable, Optional

from . import protocol


@dataclasses.dataclass
class PeerInfo:
    device_id: str
    name: str
    ip: str
    measurement_port: int
    transfer_port: int
    last_seen: float

    def is_stale(self, now: float) -> bool:
        return (now - self.last_seen) > protocol.DISCOVERY_STALE_AFTER_SEC

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class Discovery:
    def __init__(self, device_id: str, name: str, measurement_port: int, transfer_port: int,
                 mcast_group: str = protocol.DISCOVERY_MCAST_GROUP,
                 mcast_port: int = protocol.DISCOVERY_MCAST_PORT,
                 on_peer_update: Optional[Callable[[], None]] = None):
        self.device_id = device_id
        self.name = name
        self.measurement_port = measurement_port
        self.transfer_port = transfer_port
        self.mcast_group = mcast_group
        self.mcast_port = mcast_port
        self.on_peer_update = on_peer_update
        self._peers: dict[str, PeerInfo] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for target in (self._listen_loop, self._announce_loop, self._reap_loop):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()

    def get_peers(self) -> list[PeerInfo]:
        with self._lock:
            return list(self._peers.values())

    def _make_send_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        return s

    def _make_recv_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", self.mcast_port))
        mreq = struct.pack("4sl", socket.inet_aton(self.mcast_group), socket.INADDR_ANY)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        s.settimeout(0.5)
        return s

    def _announce_loop(self) -> None:
        s = self._make_send_socket()
        msg = {"type": "HELLO", "device_id": self.device_id, "name": self.name,
               "measurement_port": self.measurement_port, "transfer_port": self.transfer_port}
        payload = json.dumps(msg).encode("utf-8")
        while not self._stop.is_set():
            try:
                s.sendto(payload, (self.mcast_group, self.mcast_port))
            except OSError:
                pass
            time.sleep(protocol.DISCOVERY_INTERVAL_SEC)

    def _listen_loop(self) -> None:
        s = self._make_recv_socket()
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except ValueError:
                continue
            if msg.get("type") != "HELLO" or msg.get("device_id") == self.device_id:
                continue
            peer = PeerInfo(msg["device_id"], msg["name"], addr[0],
                             msg["measurement_port"], msg["transfer_port"], time.time())
            with self._lock:
                self._peers[peer.device_id] = peer
            if self.on_peer_update:
                self.on_peer_update()

    def _reap_loop(self) -> None:
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                stale = [pid for pid, p in self._peers.items() if p.is_stale(now)]
                for pid in stale:
                    del self._peers[pid]
            if stale and self.on_peer_update:
                self.on_peer_update()
            time.sleep(1.0)

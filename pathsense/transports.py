"""
Level 2c — Transports (Review 3).

The file can travel over more than one physical path. Each transport looks the
same to the transfer engine: connect() gives something with sendall/recv/close,
so secure_channel can wrap it and transfer.py does not care which radio is
underneath.

  * TcpTransport      — Wi-Fi (shared network, hotspot or Wi-Fi Direct). Fast.
  * RfcommTransport   — classic Bluetooth serial. Works where Python exposes
                        AF_BLUETOOTH (Linux, macOS). Slow but needs no network,
                        so it is the fallback when Wi-Fi disappears.
  * ThrottledTransport— any transport with a speed limit applied in software.
                        Used to stand in for the Bluetooth link on platforms
                        where Python cannot open an RFCOMM socket (Windows),
                        and to make the fallback visible in a demo. Clearly
                        labelled as emulated wherever it is shown.

The picker below is what the agent calls when Wi-Fi fails: it returns the best
transport that is actually usable on this machine, and says so in words.
"""
from __future__ import annotations

import dataclasses
import socket
import time
from typing import Optional

BLUETOOTH_CLASSIC_MBPS = 1.8      # realistic Bluetooth 4+ EDR throughput


class TransportUnavailable(RuntimeError):
    pass


@dataclasses.dataclass
class TransportInfo:
    kind: str                      # "wifi" | "bluetooth" | "bluetooth-emulated"
    detail: str
    expected_mbps: float
    emulated: bool = False

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class _SocketLike:
    """Minimal surface the channel layer uses."""

    def sendall(self, data: bytes) -> None: raise NotImplementedError
    def recv(self, n: int) -> bytes: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
    def settimeout(self, t: float) -> None: pass
    def setsockopt(self, *a) -> None: pass


class ThrottledSocket(_SocketLike):
    """Wraps a socket and limits throughput, emulating a slower radio."""

    def __init__(self, sock, mbps: float = BLUETOOTH_CLASSIC_MBPS):
        self._sock = sock
        self.bytes_per_sec = mbps * 1_000_000 / 8
        self._credit_time = time.perf_counter()

    def _pace(self, nbytes: int) -> None:
        need = nbytes / self.bytes_per_sec
        now = time.perf_counter()
        target = max(self._credit_time, now) + need
        self._credit_time = target
        delay = target - now - need
        if delay > 0:
            time.sleep(min(delay, 0.5))

    def sendall(self, data: bytes) -> None:
        self._pace(len(data))
        self._sock.sendall(data)

    def recv(self, n: int) -> bytes:
        return self._sock.recv(n)

    def close(self) -> None:
        self._sock.close()

    def settimeout(self, t: float) -> None:
        self._sock.settimeout(t)

    def setsockopt(self, *a) -> None:
        try:
            self._sock.setsockopt(*a)
        except OSError:
            pass


class TcpTransport:
    kind = "wifi"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    def info(self, detail: str = "TCP over Wi-Fi") -> TransportInfo:
        return TransportInfo(self.kind, detail, expected_mbps=0.0)

    def connect(self, address, port: int):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.connect((address, port))
        return s

    def listen(self, port: int, backlog: int = 4) -> socket.socket:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", port))
        srv.listen(backlog)
        return srv


class RfcommTransport:
    """Classic Bluetooth serial port. Needs AF_BLUETOOTH in this Python."""

    kind = "bluetooth"

    def __init__(self, channel: int = 3, timeout: float = 20.0):
        if not hasattr(socket, "AF_BLUETOOTH"):
            raise TransportUnavailable("this Python has no AF_BLUETOOTH (Windows)")
        self.channel = channel
        self.timeout = timeout

    def info(self) -> TransportInfo:
        return TransportInfo(self.kind, f"Bluetooth RFCOMM channel {self.channel}",
                             expected_mbps=BLUETOOTH_CLASSIC_MBPS)

    def connect(self, address, port: Optional[int] = None):   # pragma: no cover - needs a radio
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(self.timeout)
        s.connect((address, port or self.channel))
        return s

    def listen(self, port: Optional[int] = None, backlog: int = 1):   # pragma: no cover
        srv = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        srv.bind(("", port or self.channel))
        srv.listen(backlog)
        return srv


class EmulatedBluetoothTransport(TcpTransport):
    """TCP paced down to Bluetooth speed, for platforms without RFCOMM in Python.

    This is honest about itself: `emulated` is True and the dashboard shows
    "Bluetooth fallback (emulated)". It exists so the fallback path — the
    agent noticing Wi-Fi is gone, switching transport and finishing the file
    slowly — is real code that runs, not a diagram.
    """

    kind = "bluetooth-emulated"

    def __init__(self, mbps: float = BLUETOOTH_CLASSIC_MBPS, timeout: float = 20.0):
        super().__init__(timeout=timeout)
        self.mbps = mbps

    def info(self, detail: str = "") -> TransportInfo:
        return TransportInfo(self.kind, detail or f"Bluetooth-speed fallback ({self.mbps:.1f} Mbps, emulated)",
                             expected_mbps=self.mbps, emulated=True)

    def connect(self, address, port: int):
        return ThrottledSocket(super().connect(address, port), self.mbps)


def fallback_transport(bt_address: Optional[str] = None) -> tuple[object, TransportInfo]:
    """Pick the best non-Wi-Fi transport that can actually reach this peer.

    An RFCOMM socket needs the peer's BLUETOOTH address (and an RFCOMM service
    on the other side) — it cannot dial an IP address. Python on Windows does
    expose AF_BLUETOOTH, so an earlier version happily selected RFCOMM and then
    tried to connect to an IPv4 address, which failed instantly and produced an
    endless drop/retry loop. The real transport is therefore only used when a
    Bluetooth address is actually known; otherwise the paced TCP stand-in runs,
    which reaches the peer over IP at Bluetooth-like speed and is labelled as
    emulated everywhere it is shown.
    """
    if bt_address:
        try:
            t = RfcommTransport()
            return t, t.info()
        except TransportUnavailable:
            pass
    t = EmulatedBluetoothTransport()
    return t, t.info()

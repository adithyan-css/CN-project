"""
Level 1c — Bluetooth Low Energy discovery (Review 3).

Why BLE: UDP multicast only reaches devices already on the same Wi-Fi, and
many networks block it. BLE advertising is how AirDrop and Quick Share find
devices — it works with no network at all, costs almost no power, and the
radio is already in every laptop and phone.

What is advertised. A BLE advertisement carries at most 31 bytes, so the
beacon holds only what another device needs to reach us:

    byte  0      version (1)
    bytes 1-6    device id (6 bytes)
    bytes 7-10   IPv4 address, or 0.0.0.0 when we are not on a network
    bytes 11-12  measurement port      (big-endian)
    bytes 13-14  transfer port         (big-endian)
    bytes 15-18  first 4 bytes of the SHA-256 of our public key
    byte  19     flags (bit 0: paired-before, bit 1: hotspot available)

That is 20 bytes of manufacturer data; the device's friendly name goes in the
scan response, which the scanner reads separately. The full public key is too
big for an advertisement, so only its fingerprint is broadcast — the key
itself is exchanged when the two devices connect, and the 6-digit pairing code
(see pairing.py) is what proves it was not swapped on the way.

Radios and platforms. Scanning uses `bleak`, which works on Windows, macOS and
Linux. Advertising (acting as a peripheral) is not supported by bleak, so on
Windows it uses WinRT's BluetoothLEAdvertisementPublisher through whichever
WinRT binding is installed (`winrt-windows-devices-bluetooth-advertisement`,
which bleak pulls in, or the older `winsdk`), and on Linux BlueZ via
`bluetoothctl`/`hcitool`. Neither is available
in every environment, so every backend reports what it can do and the node
falls back to UDP multicast discovery with a clear message rather than
pretending the radio is there. The payload codec and the peer bookkeeping
below are plain Python and are unit-tested without any radio.
"""
from __future__ import annotations

import dataclasses
import hashlib
import ipaddress
import shutil
import struct
import subprocess
import threading
import time
from typing import Callable, Optional

from . import protocol
from .discovery import PeerInfo

MANUFACTURER_ID = 0xFFFF          # 0xFFFF is reserved for testing/experiments
PAYLOAD_VERSION = 1
PAYLOAD_LEN = 20
ADVERTISE_INTERVAL_SEC = 1.0
STALE_AFTER_SEC = 10.0            # BLE advertisements are missed more often than UDP

FLAG_PAIRED_BEFORE = 0x01
FLAG_HOTSPOT = 0x02


@dataclasses.dataclass
class BeaconPayload:
    device_id: str                 # 12 hex chars (6 bytes)
    ipv4: str
    measurement_port: int
    transfer_port: int
    key_fingerprint: str           # 8 hex chars (4 bytes)
    paired_before: bool = False
    hotspot: bool = False

    def encode(self) -> bytes:
        raw_id = bytes.fromhex(self.device_id)[:6].ljust(6, b"\x00")
        ip = ipaddress.IPv4Address(self.ipv4 or "0.0.0.0").packed
        fp = bytes.fromhex(self.key_fingerprint)[:4].ljust(4, b"\x00")
        flags = (FLAG_PAIRED_BEFORE if self.paired_before else 0) | (FLAG_HOTSPOT if self.hotspot else 0)
        return (bytes([PAYLOAD_VERSION]) + raw_id + ip
                + struct.pack(">HH", self.measurement_port, self.transfer_port)
                + fp + bytes([flags]))

    @classmethod
    def decode(cls, raw: bytes) -> "BeaconPayload":
        if len(raw) < PAYLOAD_LEN or raw[0] != PAYLOAD_VERSION:
            raise ValueError("not a NetShare beacon")
        m_port, t_port = struct.unpack(">HH", raw[11:15])
        return cls(device_id=raw[1:7].hex(), ipv4=str(ipaddress.IPv4Address(raw[7:11])),
                   measurement_port=m_port, transfer_port=t_port,
                   key_fingerprint=raw[15:19].hex(),
                   paired_before=bool(raw[19] & FLAG_PAIRED_BEFORE),
                   hotspot=bool(raw[19] & FLAG_HOTSPOT))

    @property
    def reachable_over_ip(self) -> bool:
        return self.ipv4 not in ("", "0.0.0.0")


def key_fingerprint(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()[:8]


# --------------------------------------------------------------------------
# Advertising backends
# --------------------------------------------------------------------------
class AdvertiserUnavailable(RuntimeError):
    pass


class _BaseAdvertiser:
    name = "none"

    def start(self, payload: bytes, local_name: str) -> None:
        raise NotImplementedError

    def update(self, payload: bytes) -> None:
        self.stop()
        self.start(payload, self._local_name)

    def stop(self) -> None:
        pass


class WindowsAdvertiser(_BaseAdvertiser):
    """WinRT BluetoothLEAdvertisementPublisher.

    The same Windows API ships under two package names: the newer, split
    `winrt-windows-devices-bluetooth-advertisement` (which `bleak` installs as
    a dependency, and which has wheels for current Python versions) and the
    older all-in-one `winsdk`. `winsdk` has no wheel for Python 3.13, so pip
    tries to compile it and fails without Visual Studio. Both are tried here,
    newest first, so installing `bleak` alone is enough.
    """

    name = "winrt"

    def __init__(self):
        self._advertisement_mod = None
        self._streams_mod = None
        errors = []
        for adv_mod, streams_mod, label in (
                ("winrt.windows.devices.bluetooth.advertisement", "winrt.windows.storage.streams", "winrt"),
                ("winsdk.windows.devices.bluetooth.advertisement", "winsdk.windows.storage.streams", "winsdk")):
            try:                                       # pragma: no cover - Windows only
                import importlib
                self._advertisement_mod = importlib.import_module(adv_mod)
                self._streams_mod = importlib.import_module(streams_mod)
                self.name = label
                break
            except Exception as e:                     # pragma: no cover
                errors.append(f"{label}: {e}")
        if self._advertisement_mod is None:            # pragma: no cover
            raise AdvertiserUnavailable(
                "no WinRT Bluetooth module (" + "; ".join(errors) + "). Install bleak: pip install bleak")
        self._publisher = None
        self._local_name = ""

    def start(self, payload: bytes, local_name: str) -> None:   # pragma: no cover
        # Windows refuses an advertisement that sets LocalName from a normal
        # (non-elevated, non-peripheral-role) app — it answers "The parameter
        # is incorrect". Only the manufacturer-data section is published; the
        # device id travels inside BeaconPayload either way.
        self._local_name = local_name
        writer = self._streams_mod.DataWriter()
        writer.write_bytes(payload)
        section = self._advertisement_mod.BluetoothLEManufacturerData()
        section.company_id = MANUFACTURER_ID
        section.data = writer.detach_buffer()
        publisher = self._advertisement_mod.BluetoothLEAdvertisementPublisher()
        publisher.advertisement.manufacturer_data.append(section)
        try:
            publisher.start()
        except OSError as e:
            raise AdvertiserUnavailable(
                f"Windows refused to start BLE advertising ({e}). This usually means the "
                f"adapter does not support peripheral mode, or Bluetooth is turned off.") from e
        self._publisher = publisher

    def stop(self) -> None:                                     # pragma: no cover
        if self._publisher is not None:
            try:
                self._publisher.stop()
            finally:
                self._publisher = None


class BluezAdvertiser(_BaseAdvertiser):
    """Linux/BlueZ advertising via `btmgmt`/`hcitool` (needs root on most systems)."""

    name = "bluez"

    def __init__(self):
        if not shutil.which("hcitool"):                # pragma: no cover - Linux only
            raise AdvertiserUnavailable("hcitool not found (install bluez)")
        self._local_name = ""

    def start(self, payload: bytes, local_name: str) -> None:   # pragma: no cover
        self._local_name = local_name
        data = struct.pack("<H", MANUFACTURER_ID) + payload
        adv = bytes([len(data) + 1, 0xFF]) + data
        args = ["hcitool", "-i", "hci0", "cmd", "0x08", "0x0008",
                f"{len(adv):02x}"] + [f"{b:02x}" for b in adv.ljust(31, b"\x00")]
        try:
            subprocess.run(args, check=True, capture_output=True)
            subprocess.run(["hcitool", "-i", "hci0", "cmd", "0x08", "0x000a", "01"],
                           check=True, capture_output=True)
        except (subprocess.CalledProcessError, OSError) as e:
            raise AdvertiserUnavailable(f"BlueZ refused to advertise ({e}); root is usually required") from e

    def stop(self) -> None:                                     # pragma: no cover
        subprocess.run(["hcitool", "-i", "hci0", "cmd", "0x08", "0x000a", "00"],
                       capture_output=True)


def make_advertiser() -> _BaseAdvertiser:
    errors = []
    for cls in (WindowsAdvertiser, BluezAdvertiser):
        try:
            return cls()
        except AdvertiserUnavailable as e:
            errors.append(str(e))
        except Exception as e:          # a radio backend must never crash the node
            errors.append(f"{cls.name}: {e!r}")
    raise AdvertiserUnavailable("; ".join(errors) or "no BLE advertiser on this platform")


# --------------------------------------------------------------------------
# Discovery over BLE
# --------------------------------------------------------------------------
class BleDiscovery:
    """Same interface as discovery.Discovery, but over the Bluetooth radio.

    Peers are kept in the same PeerInfo form the rest of the system already
    uses, so measurement, decision and transfer need no changes.
    """

    def __init__(self, device_id: str, name: str, measurement_port: int, transfer_port: int,
                 public_key: bytes = b"", local_ip: Callable[[], str] = lambda: "0.0.0.0",
                 on_peer_update: Optional[Callable[[], None]] = None,
                 on_event: Optional[Callable[[str], None]] = None):
        self.device_id = device_id
        self.name = name
        self.measurement_port = measurement_port
        self.transfer_port = transfer_port
        self.public_key = public_key
        self.local_ip = local_ip
        self.on_peer_update = on_peer_update
        self.on_event = on_event or (lambda msg: None)
        self._peers: dict[str, PeerInfo] = {}
        self._fingerprints: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._advertiser: Optional[_BaseAdvertiser] = None
        self.backend = "none"

    # ---- payload we broadcast -------------------------------------------
    def payload(self) -> BeaconPayload:
        return BeaconPayload(device_id=self.device_id, ipv4=self.local_ip(),
                             measurement_port=self.measurement_port,
                             transfer_port=self.transfer_port,
                             key_fingerprint=key_fingerprint(self.public_key) if self.public_key else "00000000")

    # ---- what the rest of the system calls -------------------------------
    def get_peers(self) -> list[PeerInfo]:
        now = time.time()
        with self._lock:
            fresh = [p for p in self._peers.values() if (now - p.last_seen) <= STALE_AFTER_SEC]
            return fresh

    def fingerprint_of(self, device_id: str) -> Optional[str]:
        with self._lock:
            return self._fingerprints.get(device_id)

    def observe(self, raw: bytes, local_name: str = "") -> Optional[PeerInfo]:
        """Feed one received advertisement in. Separated from the radio so the
        logic can be tested (and so a simulated backend can reuse it)."""
        try:
            beacon = BeaconPayload.decode(raw)
        except ValueError:
            return None
        if beacon.device_id == self.device_id:
            return None
        peer = PeerInfo(device_id=beacon.device_id, name=local_name or beacon.device_id[:6],
                        ip=beacon.ipv4, measurement_port=beacon.measurement_port,
                        transfer_port=beacon.transfer_port, last_seen=time.time())
        with self._lock:
            first_time = beacon.device_id not in self._peers
            if not first_time and not local_name:
                peer.name = self._peers[beacon.device_id].name
            self._peers[beacon.device_id] = peer
            self._fingerprints[beacon.device_id] = beacon.key_fingerprint
        if first_time:
            self.on_event(f"Found {peer.name} over Bluetooth"
                          + (f" (reachable at {peer.ip})" if beacon.reachable_over_ip else " (no shared network yet)"))
        if self.on_peer_update:
            self.on_peer_update()
        return peer

    # ---- radio -----------------------------------------------------------
    def start(self) -> bool:
        """Start advertising and scanning. Returns False if the radio is not
        usable here (the caller then falls back to multicast discovery).

        Nothing in here may raise: a missing library, a Bluetooth adapter that
        cannot advertise, or a radio switched off are all ordinary situations,
        and the node has to keep running over Wi-Fi when they happen.
        """
        try:
            self._advertiser = make_advertiser()
            self._advertiser.start(self.payload().encode(), self.name)
            self.backend = self._advertiser.name
        except AdvertiserUnavailable as e:
            self.on_event(f"Bluetooth advertising unavailable: {e}")
            return False
        except Exception as e:
            self.on_event(f"Bluetooth advertising failed to start ({e!r}); continuing without it")
            self._safe_stop_advertiser()
            return False
        try:                                           # pragma: no cover - needs a radio
            import bleak  # noqa: F401
        except Exception as e:
            self.on_event(f"Bluetooth scanning unavailable: {e} (pip install bleak)")
            self._safe_stop_advertiser()
            return False
        threading.Thread(target=self._scan_loop, daemon=True).start()   # pragma: no cover
        threading.Thread(target=self._readvertise_loop, daemon=True).start()
        return True

    def _safe_stop_advertiser(self) -> None:
        if self._advertiser is not None:
            try:
                self._advertiser.stop()
            except Exception:
                pass
            self._advertiser = None

    def _readvertise_loop(self) -> None:               # pragma: no cover - needs a radio
        last = self.payload().encode()
        while not self._stop.is_set():
            time.sleep(ADVERTISE_INTERVAL_SEC)
            current = self.payload().encode()
            if current != last and self._advertiser:
                self._advertiser.update(current)       # our IP changed (e.g. joined a hotspot)
                last = current

    def _scan_loop(self) -> None:                      # pragma: no cover - needs a radio
        import asyncio
        from bleak import BleakScanner

        async def run():
            def detected(device, adv):
                raw = (adv.manufacturer_data or {}).get(MANUFACTURER_ID)
                if raw:
                    self.observe(bytes(raw), adv.local_name or device.name or "")
            scanner = BleakScanner(detection_callback=detected)
            await scanner.start()
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
            await scanner.stop()

        asyncio.run(run())

    def stop(self) -> None:
        self._stop.set()
        self._safe_stop_advertiser()


class SimulatedBleDiscovery(BleDiscovery):
    """Exercises the exact BLE payload path without a radio.

    Beacons are carried over UDP multicast instead of the Bluetooth radio, so
    the encode/observe/stale logic that runs in the real thing is the code
    under test here, and the demo still works on machines with no usable BLE
    stack. The dashboard labels this mode so nobody mistakes it for real
    Bluetooth.
    """

    def __init__(self, *args, sim_port: int = protocol.DISCOVERY_MCAST_PORT + 1, **kwargs):
        super().__init__(*args, **kwargs)
        self.sim_port = sim_port
        self.backend = "simulated"

    def start(self) -> bool:
        import socket
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        self._recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._recv_sock.bind(("", self.sim_port))
        mreq = struct.pack("4sl", socket.inet_aton(protocol.DISCOVERY_MCAST_GROUP), socket.INADDR_ANY)
        self._recv_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self._recv_sock.settimeout(0.5)
        threading.Thread(target=self._sim_advertise_loop, daemon=True).start()
        threading.Thread(target=self._sim_scan_loop, daemon=True).start()
        return True

    def _sim_advertise_loop(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self.payload().encode() + self.name.encode("utf-8")[:24]
                self._send_sock.sendto(frame, (protocol.DISCOVERY_MCAST_GROUP, self.sim_port))
            except OSError:
                pass
            time.sleep(ADVERTISE_INTERVAL_SEC)

    def _sim_scan_loop(self) -> None:
        import socket as _s
        while not self._stop.is_set():
            try:
                data, _addr = self._recv_sock.recvfrom(256)
            except (_s.timeout, OSError):
                continue
            self.observe(data[:PAYLOAD_LEN], data[PAYLOAD_LEN:].decode("utf-8", "ignore"))

    def stop(self) -> None:
        self._stop.set()
        for sock in ("_send_sock", "_recv_sock"):
            s = getattr(self, sock, None)
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

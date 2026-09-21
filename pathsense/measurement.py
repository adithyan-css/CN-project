"""
Level 2 — Measurement Engine. MeasurementServer answers probes/throughput
tests from other nodes (run on every node). probe_latency/measure_throughput
are what the SENDER calls to actively measure a specific peer.
MetricsCollector continuously (re-)measures every discovered peer.
"""
from __future__ import annotations

import dataclasses
import socket
import statistics
import threading
import time
from typing import Optional

from . import protocol


@dataclasses.dataclass
class PeerMetricsSample:
    peer_id: str
    latency_ms: Optional[float]
    jitter_ms: Optional[float]
    loss_pct: float
    throughput_mbps: Optional[float]
    measured_at: float
    simulated: bool = False   # True only for demo-injected degradation samples

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class MeasurementServer:
    def __init__(self, port: int):
        self.port = port
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._udp_echo_loop, daemon=True).start()
        threading.Thread(target=self._tcp_throughput_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _udp_echo_loop(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", self.port))
        s.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = protocol.json.loads(data.decode("utf-8"))
            except Exception:
                continue
            if msg.get("type") != "PROBE":
                continue
            reply = {"type": "PROBE_ACK", "seq": msg["seq"], "orig_ts": msg["ts"], "echo_ts": protocol.now_ms()}
            try:
                s.sendto(protocol.json.dumps(reply).encode("utf-8"), addr)
            except OSError:
                pass

    def _tcp_throughput_loop(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(8)
        srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_throughput_conn, args=(conn,), daemon=True).start()

    @staticmethod
    def _handle_throughput_conn(conn: socket.socket) -> None:
        try:
            header = protocol.recv_json(conn)
            if header.get("type") != "THROUGHPUT_TEST":
                return
            n = header["bytes"]
            start = time.perf_counter()
            protocol.recv_exact(conn, n)
            duration = time.perf_counter() - start
            protocol.send_json(conn, {"type": "THROUGHPUT_RESULT", "duration_sec": duration})
        except (ConnectionError, OSError, KeyError):
            pass
        finally:
            conn.close()


def probe_latency(ip: str, port: int, count: int = 8,
                   timeout: float = protocol.PROBE_TIMEOUT_SEC) -> tuple[Optional[float], Optional[float], float]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    rtts: list[float] = []
    for seq in range(count):
        sent_ts = protocol.now_ms()
        msg = {"type": "PROBE", "seq": seq, "ts": sent_ts}
        try:
            s.sendto(protocol.json.dumps(msg).encode("utf-8"), (ip, port))
            data, _ = s.recvfrom(1024)
            reply = protocol.json.loads(data.decode("utf-8"))
            if reply.get("type") == "PROBE_ACK" and reply.get("seq") == seq:
                rtts.append(protocol.now_ms() - sent_ts)
        except (socket.timeout, OSError):
            pass
    s.close()
    loss_pct = 100.0 * (count - len(rtts)) / count if count else 0.0
    if not rtts:
        return None, None, loss_pct
    latency_ms = statistics.mean(rtts)
    jitter_ms = statistics.pstdev(rtts) if len(rtts) > 1 else 0.0
    return latency_ms, jitter_ms, loss_pct


def measure_throughput(ip: str, port: int, num_bytes: int = 2_000_000, timeout: float = 6.0) -> Optional[float]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        protocol.send_json(s, {"type": "THROUGHPUT_TEST", "bytes": num_bytes})
        payload = b"\x00" * 65536
        sent = 0
        start = time.perf_counter()
        while sent < num_bytes:
            chunk = payload[: min(len(payload), num_bytes - sent)]
            s.sendall(chunk)
            sent += len(chunk)
        result = protocol.recv_json(s)
        duration = result.get("duration_sec") or (time.perf_counter() - start)
        if duration <= 0:
            return None
        return (num_bytes * 8) / duration / 1_000_000
    except (OSError, ConnectionError):
        return None
    finally:
        s.close()


class MetricsCollector:
    HISTORY_LEN = 5

    def __init__(self, discovery, interval_sec: float = 2.0, throughput_bytes: int = 1_000_000):
        self.discovery = discovery
        self.interval_sec = interval_sec
        self.throughput_bytes = throughput_bytes
        self._history: dict[str, list[PeerMetricsSample]] = {}
        self._simulate_until: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def latest(self, peer_id: str) -> Optional[PeerMetricsSample]:
        with self._lock:
            hist = self._history.get(peer_id)
            return hist[-1] if hist else None

    def history(self, peer_id: str) -> list[PeerMetricsSample]:
        with self._lock:
            return list(self._history.get(peer_id, []))

    def all_latest(self) -> dict[str, PeerMetricsSample]:
        with self._lock:
            return {pid: hist[-1] for pid, hist in self._history.items() if hist}

    def _record(self, sample: PeerMetricsSample) -> None:
        with self._lock:
            hist = self._history.setdefault(sample.peer_id, [])
            hist.append(sample)
            if len(hist) > self.HISTORY_LEN:
                hist.pop(0)

    # ---- demo support ------------------------------------------------------
    # For a live demo on a healthy network, the dashboard can inject a clearly
    # labelled, time-limited degradation for one peer. Real measurements are
    # replaced by a synthetic bad sample until it expires; nothing else in the
    # system is faked (the transfer itself really pauses and resumes).
    def simulate_degradation(self, peer_id: str, seconds: float) -> None:
        with self._lock:
            self._simulate_until[peer_id] = time.time() + seconds
        self._record(self._synthetic_bad_sample(peer_id))

    def simulation_remaining(self, peer_id: str) -> float:
        with self._lock:
            return max(0.0, self._simulate_until.get(peer_id, 0.0) - time.time())

    @staticmethod
    def _synthetic_bad_sample(peer_id: str) -> PeerMetricsSample:
        return PeerMetricsSample(peer_id, 450.0, 120.0, 40.0, 0.2, time.time(), simulated=True)

    def measure_one(self, peer) -> PeerMetricsSample:
        if self.simulation_remaining(peer.device_id) > 0:
            sample = self._synthetic_bad_sample(peer.device_id)
            self._record(sample)
            return sample
        latency_ms, jitter_ms, loss_pct = probe_latency(peer.ip, peer.measurement_port)
        throughput = measure_throughput(peer.ip, peer.measurement_port, self.throughput_bytes)
        sample = PeerMetricsSample(peer.device_id, latency_ms, jitter_ms, loss_pct, throughput, time.time())
        self._record(sample)
        return sample

    def _loop(self) -> None:
        while not self._stop.is_set():
            for peer in self.discovery.get_peers():
                if self._stop.is_set():
                    break
                self.measure_one(peer)
            time.sleep(self.interval_sec)

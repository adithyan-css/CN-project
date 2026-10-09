"""
Level 7 — Link simulator (Review 3).

Two jobs, both needed because a college demo cannot summon a bad Wi-Fi link on
command and a laptop cannot be moved out of range inside a CI run:

  1. `LinkTrace` generates realistic sequences of link measurements — a healthy
     link that wanders, with fading events, congestion and outages mixed in.
     These traces train and test the prediction model, and they are what the
     evaluation replays so that every policy (fixed, rule-based, AI) faces the
     EXACT same conditions.

  2. `ImpairProxy` is a real TCP proxy that sits between sender and receiver
     and applies a bandwidth cap and added delay to the bytes going through it.
     That makes the evaluation an end-to-end measurement of the real transfer
     engine, not a paper model.

Honesty note: a proxy cannot drop packets the way `tc netem` or Clumsy can,
because TCP would simply retransmit beneath us. Packet loss is therefore
modelled where it is actually observable — in the measurements the agent sees,
and in the throughput cap (loss is converted to an effective-rate penalty using
the standard square-root throughput relation). For the final report, the same
experiment should also be run with netem/Clumsy on real hardware; the harness
takes those numbers in the same CSV format.
"""
from __future__ import annotations

import dataclasses
import math
import random
import socket
import threading
import time
from typing import Iterator, Optional

from .linklog import _Sample


@dataclasses.dataclass
class LinkState:
    latency_ms: float
    jitter_ms: float
    loss_pct: float
    throughput_mbps: float

    def effective_mbps(self) -> float:
        """Throughput after accounting for loss (square-root law, clamped)."""
        if self.loss_pct <= 0:
            return self.throughput_mbps
        if self.loss_pct >= 50:
            return 0.05
        return max(0.05, self.throughput_mbps / (1.0 + 8.0 * math.sqrt(self.loss_pct / 100.0)))


class LinkTrace:
    """Generates a sequence of LinkStates, one per measurement interval."""

    def __init__(self, seed: int = 0, interval_sec: float = 2.0,
                 base_mbps: float = 45.0, base_latency_ms: float = 12.0,
                 event_rate: float = 0.04):
        self.rng = random.Random(seed)
        self.interval_sec = interval_sec
        self.base_mbps = base_mbps
        self.base_latency_ms = base_latency_ms
        self.event_rate = event_rate
        self._event_left = 0
        self._event_kind = ""

    def _start_event(self) -> None:
        self._event_kind = self.rng.choice(["fade", "congestion", "outage"])
        self._event_left = {"fade": self.rng.randint(4, 10),
                            "congestion": self.rng.randint(5, 14),
                            "outage": self.rng.randint(3, 7)}[self._event_kind]

    def step(self) -> LinkState:
        if self._event_left <= 0 and self.rng.random() < self.event_rate:
            self._start_event()
        wobble = self.rng.gauss(1.0, 0.07)
        state = LinkState(latency_ms=max(1.0, self.base_latency_ms * self.rng.gauss(1.0, 0.25)),
                          jitter_ms=max(0.2, self.base_latency_ms * 0.2 * abs(self.rng.gauss(1.0, 0.5))),
                          loss_pct=max(0.0, self.rng.gauss(0.3, 0.4)),
                          throughput_mbps=max(0.2, self.base_mbps * wobble))
        if self._event_left > 0:
            progress = 1.0 - (self._event_left / 10.0)
            if self._event_kind == "fade":           # moving away from the router
                state.throughput_mbps *= max(0.05, 0.5 - 0.4 * progress)
                state.loss_pct += self.rng.uniform(3, 18)
                state.latency_ms *= self.rng.uniform(2, 6)
            elif self._event_kind == "congestion":   # someone else streaming
                state.throughput_mbps *= self.rng.uniform(0.15, 0.4)
                state.latency_ms *= self.rng.uniform(3, 9)
                state.jitter_ms *= self.rng.uniform(3, 8)
            else:                                    # radio gone
                state.throughput_mbps = 0.05
                state.loss_pct = 100.0
                state.latency_ms = 2000.0
            self._event_left -= 1
        return state

    def samples(self, count: int, start_ts: Optional[float] = None, peer_id: str = "B") -> list[_Sample]:
        ts = start_ts if start_ts is not None else time.time()
        out = []
        for _ in range(count):
            st = self.step()
            unusable = st.loss_pct >= 100.0
            out.append(_Sample(latency_ms=None if unusable else st.latency_ms,
                               jitter_ms=None if unusable else st.jitter_ms,
                               loss_pct=st.loss_pct,
                               throughput_mbps=None if unusable else st.throughput_mbps,
                               measured_at=ts))
            ts += self.interval_sec
        return out


class ImpairProxy:
    """TCP proxy applying a bandwidth cap and one-way delay, changeable live."""

    def __init__(self, listen_port: int, target_host: str, target_port: int,
                 mbps: float = 50.0, delay_ms: float = 5.0):
        self.listen_port = listen_port
        self.target = (target_host, target_port)
        self.mbps = mbps
        self.delay_ms = delay_ms
        self._stop = threading.Event()
        self._srv: Optional[socket.socket] = None
        self.bytes_forwarded = 0
        self._live: list[socket.socket] = []
        self._live_lock = threading.Lock()
        self.cuts = 0

    def set_conditions(self, mbps: float, delay_ms: float) -> None:
        self.mbps, self.delay_ms = max(0.05, mbps), max(0.0, delay_ms)

    def cut(self) -> None:
        """Drop every live connection — what actually happens when the radio
        goes away. A throttle to near-zero is not the same thing: TCP survives
        it, so a transfer would merely crawl instead of breaking, and the
        recovery behaviour under test would never run."""
        with self._live_lock:
            live, self._live = self._live, []
        for sock in live:
            try:
                sock.close()
            except OSError:
                pass
        if live:
            self.cuts += 1

    def start(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self.listen_port))
        self._srv.listen(8)
        self._srv.settimeout(0.5)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._srv:
            try:
                self._srv.close()
            except OSError:
                pass

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._srv.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        try:
            upstream = socket.create_connection(self.target, timeout=5)
        except OSError:
            client.close()
            return
        with self._live_lock:
            self._live += [client, upstream]
        for a, b, pace in ((client, upstream, True), (upstream, client, True)):
            threading.Thread(target=self._pipe, args=(a, b, pace), daemon=True).start()

    def _pipe(self, src: socket.socket, dst: socket.socket, pace: bool) -> None:
        src.settimeout(0.5)
        credit = time.perf_counter()
        try:
            while not self._stop.is_set():
                try:
                    data = src.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                if pace:
                    rate = self.mbps * 1e6 / 8
                    need = len(data) / rate
                    now = time.perf_counter()
                    credit = max(credit, now) + need
                    sleep_for = credit - now - need + self.delay_ms / 1000.0
                    if sleep_for > 0:
                        time.sleep(min(sleep_for, 1.0))
                try:
                    dst.sendall(data)
                except OSError:
                    break          # the other end closed; normal at end of transfer
                self.bytes_forwarded += len(data)
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except OSError:
                    pass

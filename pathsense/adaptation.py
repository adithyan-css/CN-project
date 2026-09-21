"""
Level 5 — Adaptation Engine (Review 2 redesign).

The agent watches the link to the ONE receiver the user chose. It never
sends the file to a different device. When that link degrades it pauses
the transfer (keeping every acknowledged chunk) and, once the link has
recovered, resumes to the same receiver from the first missing chunk.

LinkWatchdog is pure state (no sockets, no threads) and adds hysteresis in
both directions: it needs N consecutive unhealthy ticks before pausing and
M consecutive healthy ticks before resuming, so one noisy sample never
causes a pause and a link that is flickering never causes rapid
pause/resume cycling.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from typing import Callable, Optional

from .decision import assess_link, LinkHealth
from .measurement import MetricsCollector
from .transfer import TransferSender

PAUSE = "PAUSE"
RESUME = "RESUME"


@dataclasses.dataclass
class LinkWatchdog:
    pause_after: int = 3     # consecutive unhealthy ticks before pausing
    resume_after: int = 2    # consecutive healthy ticks before resuming
    _bad: int = 0
    _good: int = 0

    def observe(self, healthy: bool, paused: bool) -> Optional[str]:
        if not paused:
            self._good = 0
            self._bad = self._bad + 1 if not healthy else 0
            if self._bad >= self.pause_after:
                self.reset()
                return PAUSE
        else:
            self._bad = 0
            self._good = self._good + 1 if healthy else 0
            if self._good >= self.resume_after:
                self.reset()
                return RESUME
        return None

    def reset(self) -> None:
        self._bad = 0
        self._good = 0


class AdaptationLoop:
    def __init__(self, collector: MetricsCollector, discovery, sender: TransferSender, peer_names: dict,
                 on_event: Optional[Callable[[str], None]] = None, tick_sec: float = 0.5,
                 watchdog: Optional[LinkWatchdog] = None, clock: Callable[[], float] = time.time):
        self.collector = collector
        self.discovery = discovery
        self.sender = sender
        self.peer_names = peer_names
        self.on_event = on_event or (lambda msg: None)
        self.tick_sec = tick_sec
        self.watchdog = watchdog or LinkWatchdog()
        self.clock = clock
        self.last_health: Optional[LinkHealth] = None
        self._pause_requested = False
        self._announced_drop = False
        self._waiting_announced = False
        self._resume_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.tick_sec)
            status = self.sender.state.status
            if status == "COMPLETE":
                st = self.sender.state
                extra = f" after {st.pauses} pause(s) and {st.resumes} resume(s)" if st.resumes else ""
                self.on_event(f"Transfer complete: all {st.total_chunks} chunks verified{extra}.")
                break
            if status == "FAILED":
                self.on_event("Transfer failed: the receiver declined the file.")
                break
            self._tick()

    # ---- one decision step -----------------------------------------------
    def _name(self, pid: str) -> str:
        return self.peer_names.get(pid, pid)

    def _progress(self) -> str:
        st = self.sender.state
        return f"{st.progress_pct:.0f}% (chunk {self.sender.next_chunk + 1}/{st.total_chunks})"

    def _tick(self) -> None:
        st = self.sender.state
        pid = st.peer_id
        if not pid or st.status in ("IDLE", "COMPLETE", "FAILED"):
            return
        name = self._name(pid)
        health = assess_link(self.collector.latest(pid), self.clock())
        self.last_health = health

        if st.status == "DEGRADED" and not self._announced_drop:
            self._announced_drop = True
            self._pause_requested = True
            self.on_event(f"Connection to {name} dropped at {self._progress()}. "
                          f"Progress is kept; the agent will resume when the link recovers.")

        resuming = self._resume_thread is not None and self._resume_thread.is_alive()
        paused = self._pause_requested or st.status in ("PAUSED", "DEGRADED")
        action = self.watchdog.observe(health.healthy, paused)

        if action == PAUSE and st.status == "TRANSFERRING" and not resuming:
            self._pause_requested = True
            self.on_event(f"Link to {name} degraded: {health.reason}. "
                          f"Pausing at {self._progress()}; progress kept.")
            self.sender.pause()
        elif action == RESUME and paused and not resuming:
            peer = next((p for p in self.discovery.get_peers() if p.device_id == pid), None)
            if peer is None:
                if not self._waiting_announced:
                    self._waiting_announced = True
                    self.on_event(f"Waiting for {name} to reappear before resuming.")
                return
            self._pause_requested = False
            self._announced_drop = False
            self._waiting_announced = False
            self.on_event(f"Link to {name} recovered ({health.reason}). Resuming from "
                          f"{self._progress()}; only missing chunks are sent.")
            self._resume_thread = threading.Thread(
                target=self.sender.send_to, args=(pid, peer.ip, peer.transfer_port), daemon=True)
            self._resume_thread.start()

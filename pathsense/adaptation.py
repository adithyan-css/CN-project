"""
Level 5 — Adaptation Engine (the hysteresis controller). HysteresisController
is pure state: feed it (current_score, best_alternative_score) each tick; it
tells you whether to switch, requiring N CONSECUTIVE breaches (not one noisy
sample) to avoid flapping. AdaptationLoop ties MetricsCollector +
decision.score_peers + HysteresisController + TransferSender together during
a live transfer and performs the reroute when the controller says to.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from typing import Callable, Optional

from .decision import score_peers, TransferProfile
from .measurement import MetricsCollector
from .transfer import TransferSender


@dataclasses.dataclass
class HysteresisController:
    switch_margin: float = 1.3
    required_consecutive_breaches: int = 3
    _consecutive_breaches: int = 0

    def evaluate(self, current_score: float, best_alternative_score: float) -> bool:
        breached = (current_score <= 0) or (best_alternative_score >= current_score * self.switch_margin)
        if breached:
            self._consecutive_breaches += 1
        else:
            self._consecutive_breaches = 0
        return self._consecutive_breaches >= self.required_consecutive_breaches

    def reset(self) -> None:
        self._consecutive_breaches = 0


class AdaptationLoop:
    def __init__(self, collector: MetricsCollector, discovery, sender: TransferSender, peer_names: dict,
                 on_event: Optional[Callable[[str], None]] = None, tick_sec: float = 0.5,
                 profile: TransferProfile = TransferProfile.BALANCED,
                 controller: Optional[HysteresisController] = None):
        self.collector = collector
        self.discovery = discovery
        self.sender = sender
        self.peer_names = peer_names
        self.on_event = on_event or (lambda msg: None)
        self.tick_sec = tick_sec
        self.profile = profile
        self.controller = controller or HysteresisController()
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.tick_sec)
            if self.sender.state.status != "TRANSFERRING":
                continue
            self._tick()

    def _tick(self) -> None:
        latest = self.collector.all_latest()
        history = {pid: self.collector.history(pid) for pid in latest}
        ranked = score_peers(latest, history, profile=self.profile)
        if not ranked:
            return

        current_peer_id = self.sender.state.peer_id
        current = next((r for r in ranked if r.peer_id == current_peer_id), None)
        alternatives = [r for r in ranked if r.peer_id != current_peer_id and not r.excluded]
        if current is None or not alternatives:
            return

        best_alt = alternatives[0]
        if not self.controller.evaluate(current.score, best_alt.score):
            return

        current_name = self.peer_names.get(current_peer_id, current_peer_id)
        alt_name = self.peer_names.get(best_alt.peer_id, best_alt.peer_id)
        improvement = (best_alt.score / current.score) if current.score > 0 else float("inf")
        self.on_event(f"Route degraded (current score {current.score:.2f}). Searching for better transfer option...")

        peer = next((p for p in self.discovery.get_peers() if p.device_id == best_alt.peer_id), None)
        if peer is None:
            return

        self.sender.cancel_current()
        self.controller.reset()
        self.on_event(f"Switched to {alt_name} — {improvement:.1f}x better than {current_name}")

        threading.Thread(target=self.sender.send_to,
                          args=(best_alt.peer_id, peer.ip, peer.transfer_port), daemon=True).start()

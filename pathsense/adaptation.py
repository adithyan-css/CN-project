"""
Level 5 — Adaptation Engine (Review 3): the agent's sense-predict-act loop.

Every tick (0.5 s) the agent:

  1. SENSE    — reads the latest measurements for the receiver it is sending to
                and judges the link with absolute thresholds (decision.py).
  2. PREDICT  — asks the trained model what the link will look like 5 seconds
                from now (predictor.py). This is what makes the agent act
                BEFORE a transfer stalls instead of after.
  3. ACT      — four kinds of action, in priority order:
                  a. pause, if the link is unusable now (3 consecutive bad
                     ticks) or predicted to become unusable (risk above
                     PREDICTIVE_RISK for PREDICTIVE_TICKS in a row);
                  b. resume, once the link has been healthy for 2 ticks —
                     to the SAME receiver, sending only missing chunks;
                  c. switch transport, if Wi-Fi keeps failing but the peer is
                     still there: the rest of the file goes over the Bluetooth
                     fallback rather than waiting forever;
                  d. tune, every TUNE_INTERVAL seconds: the contextual bandit
                     (tuner.py) picks the window and compression, and is then
                     rewarded with the goodput that choice actually produced.
  4. EXPLAIN  — every decision is logged as a plain sentence, and the numbers
                behind it are kept for the assistant (assistant.py) to answer
                questions about later.

Hysteresis is still what keeps it stable: N consecutive bad ticks before
pausing, M consecutive good ticks before resuming, so one noisy sample never
flips the transfer. The model only ever makes the agent act EARLIER; if no
model is loaded, the Review 2 rules run unchanged.
"""
from __future__ import annotations

import dataclasses
import threading
import time
from typing import Callable, Optional

from .decision import assess_link, LinkHealth
from .linklog import LinkLogger, LogRow, features_from_samples
from .measurement import MetricsCollector
from .predictor import Forecast, Predictor
from .transfer import TransferSender
from .transports import TransportInfo, fallback_transport
from .tuner import Context, LinUCBTuner, rule_based

PAUSE = "PAUSE"
RESUME = "RESUME"
# A pause costs a few seconds (the link has to prove it is healthy again), so a
# prediction must be confident before it is worth acting on. The trained model's
# test AUC is ~0.72: good enough to catch real fades early, not good enough to
# justify pausing on a 65% hunch — evaluation runs with the lower threshold made
# the AI agent SLOWER than the plain rules because of false alarms. Below 0.80
# the forecast is still used, but only to tune the window down, which is cheap
# and reversible.
PREDICTIVE_RISK = 0.80
PREDICTIVE_TICKS = 3
CAUTION_RISK = 0.55
TUNE_INTERVAL_SEC = 4.0
WIFI_FAILURES_BEFORE_FALLBACK = 2


@dataclasses.dataclass
class LinkWatchdog:
    pause_after: int = 3
    resume_after: int = 2
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


@dataclasses.dataclass
class AgentDecision:
    """One decision, kept so the assistant can explain it afterwards."""
    at: float
    kind: str              # pause | resume | tune | transport | predict
    sentence: str
    health: Optional[dict] = None
    forecast: Optional[dict] = None
    progress_pct: float = 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class AdaptationLoop:
    def __init__(self, collector: MetricsCollector, discovery, sender: TransferSender, peer_names: dict,
                 on_event: Optional[Callable[[str], None]] = None, tick_sec: float = 0.5,
                 watchdog: Optional[LinkWatchdog] = None, clock: Callable[[], float] = time.time,
                 predictor: Optional[Predictor] = None, tuner: Optional[LinUCBTuner] = None,
                 logger: Optional[LinkLogger] = None, enable_fallback: bool = True):
        self.collector = collector
        self.discovery = discovery
        self.sender = sender
        self.peer_names = peer_names
        self.on_event = on_event or (lambda msg: None)
        self.tick_sec = tick_sec
        self.watchdog = watchdog or LinkWatchdog()
        self.clock = clock
        self.predictor = predictor
        self.tuner = tuner
        self.logger = logger
        self.enable_fallback = enable_fallback

        self.last_health: Optional[LinkHealth] = None
        self.last_forecast: Optional[Forecast] = None
        self.last_decision: Optional[AgentDecision] = None
        self.decisions: list[AgentDecision] = []
        self.transport_info: Optional[TransportInfo] = None
        self._risk_streak = 0
        self._pause_requested = False
        self._announced_drop = False
        self._waiting_announced = False
        self._resume_thread: Optional[threading.Thread] = None
        self._wifi_failures = 0
        self._fallback_transport = None
        self._last_tune = 0.0
        self._rule_reference_mbps = 0.0
        self._stop = threading.Event()

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self.tuner:
            self.tuner.save()

    def _loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(self.tick_sec)
            status = self.sender.state.status
            if status == "COMPLETE":
                st = self.sender.state
                extra = f" after {st.pauses} pause(s) and {st.resumes} resume(s)" if st.resumes else ""
                self.on_event(f"Transfer complete: all {st.total_chunks} chunks verified{extra}.")
                if self.tuner:
                    self.tuner.save()
                break
            if status == "FAILED":
                self.on_event("Transfer failed: the receiver declined the file or the pairing was refused.")
                break
            self._tick()

    # ---- helpers ---------------------------------------------------------
    def _name(self, pid: str) -> str:
        return self.peer_names.get(pid, pid)

    def _progress(self) -> str:
        st = self.sender.state
        return f"{st.progress_pct:.0f}% (chunk {self.sender.next_chunk + 1}/{st.total_chunks})"

    def _record(self, kind: str, sentence: str, health=None, forecast=None) -> None:
        d = AgentDecision(at=self.clock(), kind=kind, sentence=sentence,
                          health=health.to_dict() if health else None,
                          forecast=forecast.to_dict() if forecast else None,
                          progress_pct=self.sender.state.progress_pct)
        self.decisions.append(d)
        del self.decisions[:-200]
        self.last_decision = d
        self.on_event(sentence)

    # ---- one decision step ------------------------------------------------
    def _tick(self) -> None:
        st = self.sender.state
        pid = st.peer_id
        if not pid or st.status in ("IDLE", "PAIRING", "COMPLETE", "FAILED"):
            return
        name = self._name(pid)
        now = self.clock()
        samples = self.collector.history(pid)
        health = assess_link(self.collector.latest(pid), now)
        self.last_health = health

        forecast = self.predictor.forecast(samples, now=now) if self.predictor else None
        self.last_forecast = forecast
        if self.logger:
            latest = self.collector.latest(pid)
            if latest is not None:
                self.logger.log(LogRow(ts=now, peer_id=pid, latency_ms=latest.latency_ms,
                                       jitter_ms=latest.jitter_ms, loss_pct=latest.loss_pct,
                                       throughput_mbps=latest.throughput_mbps,
                                       simulated=getattr(latest, "simulated", False),
                                       transport=st.transport, window=st.window,
                                       compression=st.compression,
                                       goodput_mbps=st.bytes_per_sec_ema * 8 / 1e6,
                                       status=st.status))

        if st.status == "DEGRADED" and not self._announced_drop:
            self._announced_drop = True
            self._pause_requested = True
            self._wifi_failures += 1
            self._record("pause", f"Connection to {name} dropped at {self._progress()}. "
                                  f"Progress is kept; the agent will resume when the link recovers.",
                         health, forecast)

        resuming = self._resume_thread is not None and self._resume_thread.is_alive()
        paused = self._pause_requested or st.status in ("PAUSED", "DEGRADED")

        # --- predictive pause: act before the stall, not after ---
        if forecast and not paused and st.status == "TRANSFERRING":
            if forecast.risk >= PREDICTIVE_RISK:
                self._risk_streak += 1
            else:
                self._risk_streak = 0
            if self._risk_streak >= PREDICTIVE_TICKS and not resuming:
                self._risk_streak = 0
                self._pause_requested = True
                self.watchdog.reset()
                self._record("predict",
                             f"Predicted degradation on {name}'s link: {forecast.risk:.0%} risk within "
                             f"{forecast.horizon_sec:.0f}s (forecast {forecast.throughput_mbps:.1f} Mbps). "
                             f"Pausing early at {self._progress()} instead of waiting for it to stall.",
                             health, forecast)
                self.sender.pause()
                return

        action = self.watchdog.observe(health.healthy, paused)

        if action == PAUSE and st.status == "TRANSFERRING" and not resuming:
            self._pause_requested = True
            self._record("pause", f"Link to {name} degraded: {health.reason}. "
                                  f"Pausing at {self._progress()}; progress kept.", health, forecast)
            self.sender.pause()
            return

        if action == RESUME and paused and not resuming:
            peer = next((p for p in self.discovery.get_peers() if p.device_id == pid), None)
            if peer is None:
                if not self._waiting_announced:
                    self._waiting_announced = True
                    self._record("pause", f"Waiting for {name} to reappear before resuming.", health, forecast)
                return
            transport, label = self._pick_transport(name)
            self._pause_requested = False
            self._announced_drop = False
            self._waiting_announced = False
            self._record("resume", f"Link to {name} recovered ({health.reason}). Resuming from "
                                   f"{self._progress()}{label}; only missing chunks are sent.",
                         health, forecast)
            self._resume_thread = threading.Thread(
                target=self.sender.send_to, args=(pid, peer.ip, peer.transfer_port, transport), daemon=True)
            self._resume_thread.start()
            return

        if st.status == "TRANSFERRING" and self.tuner and (now - self._last_tune) >= TUNE_INTERVAL_SEC:
            self._tune(name, forecast, samples, now)
        elif (st.status == "TRANSFERRING" and forecast and not self.tuner
                and forecast.risk >= CAUTION_RISK and st.window > 4):
            # No learning tuner: still take the cheap, reversible precaution.
            self.sender.set_window(4)

    # ---- transport choice -------------------------------------------------
    def _pick_transport(self, name: str):
        if not self.enable_fallback or self._wifi_failures < WIFI_FAILURES_BEFORE_FALLBACK:
            return None, ""
        if self._fallback_transport is None:
            self._fallback_transport, info = fallback_transport()
            self.transport_info = info
            self._record("transport",
                         f"Wi-Fi to {name} has failed {self._wifi_failures} times; switching the rest of "
                         f"the file to {info.detail}. It is slower but it does not depend on the network.",
                         None, None)
        return self._fallback_transport, " over the Bluetooth fallback"

    # ---- learning tuner ---------------------------------------------------
    def _tune(self, name: str, forecast: Optional[Forecast], samples, now: float) -> None:
        st = self.sender.state
        latest = self.collector.latest(st.peer_id)
        if latest is None:
            return
        feats = features_from_samples(samples, now=now)
        instability = feats[8] if feats else 0.0
        observed = st.bytes_per_sec_ema * 8 / 1e6
        compressibility = max(0.0, min(1.0, st.to_dict()["compression_saved_pct"] / 100.0)) if st.compression else 0.3
        if forecast and forecast.risk >= CAUTION_RISK and st.window > 4:
            self.sender.set_window(4)   # cheap precaution while the risk is raised
        ctx = Context(predicted_mbps=forecast.throughput_mbps if forecast else (latest.throughput_mbps or 1.0),
                      latency_ms=latest.latency_ms or 50.0, loss_pct=latest.loss_pct,
                      instability=instability, compressibility=compressibility,
                      slow_transport=st.transport != "wifi")
        if self._last_tune:                      # reward the previous choice
            self.tuner.reward(observed)
            if self._rule_reference_mbps == 0.0:
                self._rule_reference_mbps = observed
        self._last_tune = now
        decision = self.tuner.decide(ctx, rule_fallback_mbps=self._rule_reference_mbps,
                                     observed_mbps=observed)
        changed = (decision.window != st.window) or (decision.compression != st.compression)
        self.sender.set_window(decision.window)
        self.sender.set_compression(decision.compression)
        if changed:
            self._record("tune", f"Tuning transfer to {name}: {decision.reason} "
                                 f"(measured {observed:.1f} Mbps just now)", None, forecast)

    # ---- for the dashboard / assistant ------------------------------------
    def snapshot(self) -> dict:
        return {"health": self.last_health.to_dict() if self.last_health else None,
                "forecast": self.last_forecast.to_dict() if self.last_forecast else None,
                "transport": self.transport_info.to_dict() if self.transport_info else None,
                "tuner": self.tuner.summary() if self.tuner else None,
                "decisions": [d.to_dict() for d in self.decisions[-25:]]}

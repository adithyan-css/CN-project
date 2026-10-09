"""
Level 3b — Link logging and feature extraction (Review 3).

Everything the agent measures is appended to a CSV so the prediction model can
be trained on it later. The same feature extraction runs in two places:

  * offline, over a logged CSV, to build the training set;
  * online, inside the running agent, to build the feature vector handed to
    the model.

Keeping one implementation for both is what stops the classic machine-learning
bug where the model is trained on features computed slightly differently from
the ones it sees in production.

Features (per peer, from the last few samples):
    latency_ms, jitter_ms, loss_pct, throughput_mbps      — the newest sample
    latency_mean, throughput_mean                          — short rolling mean
    latency_slope, throughput_slope                        — trend per second
    throughput_cv                                          — how unstable it is
    sample_age_s                                           — staleness

The label for training is what actually happened HORIZON_SEC later: the
throughput then, and whether the link was unusable then.
"""
from __future__ import annotations

import csv
import dataclasses
import os
import statistics
import threading
import time
from typing import Iterable, Optional, Sequence

HORIZON_SEC = 5.0
FEATURE_NAMES = ["latency_ms", "jitter_ms", "loss_pct", "throughput_mbps",
                 "latency_mean", "throughput_mean", "latency_slope",
                 "throughput_slope", "throughput_cv", "sample_age_s"]
CSV_COLUMNS = ["ts", "peer_id", "latency_ms", "jitter_ms", "loss_pct", "throughput_mbps",
               "simulated", "transport", "window", "compression", "goodput_mbps", "status"]

BAD_LATENCY_MS = 300.0
BAD_LOSS_PCT = 15.0
BAD_THROUGHPUT_MBPS = 0.5


def _slope(values: Sequence[float], times: Sequence[float]) -> float:
    """Least-squares slope per second; 0 when there is nothing to fit."""
    n = len(values)
    if n < 2:
        return 0.0
    t0 = times[0]
    xs = [t - t0 for t in times]
    mean_x = sum(xs) / n
    mean_y = sum(values) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return 0.0
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values)) / denom


def features_from_samples(samples: Sequence, now: Optional[float] = None) -> Optional[list[float]]:
    """`samples` are PeerMetricsSample-like objects, oldest first."""
    usable = [s for s in samples if s.latency_ms is not None and s.throughput_mbps is not None]
    if not usable:
        return None
    now = now if now is not None else time.time()
    latest = usable[-1]
    lat = [s.latency_ms for s in usable]
    tp = [s.throughput_mbps for s in usable]
    ts = [s.measured_at for s in usable]
    tp_mean = sum(tp) / len(tp)
    cv = (statistics.pstdev(tp) / tp_mean) if (len(tp) > 1 and tp_mean > 0) else 0.0
    return [float(latest.latency_ms), float(latest.jitter_ms or 0.0), float(latest.loss_pct),
            float(latest.throughput_mbps), sum(lat) / len(lat), tp_mean,
            _slope(lat, ts), _slope(tp, ts), cv, max(0.0, now - latest.measured_at)]


def is_bad(latency_ms: Optional[float], loss_pct: float, throughput_mbps: Optional[float]) -> bool:
    if latency_ms is None or throughput_mbps is None:
        return True
    return (loss_pct > BAD_LOSS_PCT or latency_ms > BAD_LATENCY_MS
            or throughput_mbps < BAD_THROUGHPUT_MBPS)


@dataclasses.dataclass
class LogRow:
    ts: float
    peer_id: str
    latency_ms: Optional[float]
    jitter_ms: Optional[float]
    loss_pct: float
    throughput_mbps: Optional[float]
    simulated: bool = False
    transport: str = "wifi"
    window: int = 0
    compression: bool = False
    goodput_mbps: float = 0.0
    status: str = ""

    def as_csv(self) -> list:
        return [f"{self.ts:.3f}", self.peer_id,
                "" if self.latency_ms is None else f"{self.latency_ms:.3f}",
                "" if self.jitter_ms is None else f"{self.jitter_ms:.3f}",
                f"{self.loss_pct:.2f}",
                "" if self.throughput_mbps is None else f"{self.throughput_mbps:.3f}",
                int(self.simulated), self.transport, self.window, int(self.compression),
                f"{self.goodput_mbps:.3f}", self.status]


class LinkLogger:
    """Appends one CSV row per measurement; safe to call from any thread."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self._lock = threading.Lock()
        self.rows_written = 0
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerow(CSV_COLUMNS)

    def log(self, row: LogRow) -> None:
        if not self.path:
            return
        with self._lock:
            with open(self.path, "a", newline="") as f:
                csv.writer(f).writerow(row.as_csv())
            self.rows_written += 1


# --------------------------------------------------------------------------
# Turning a log into a training set
# --------------------------------------------------------------------------
@dataclasses.dataclass
class _Sample:
    latency_ms: Optional[float]
    jitter_ms: Optional[float]
    loss_pct: float
    throughput_mbps: Optional[float]
    measured_at: float


def read_log(path: str) -> list[_Sample]:
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out.append(_Sample(
                latency_ms=float(row["latency_ms"]) if row["latency_ms"] else None,
                jitter_ms=float(row["jitter_ms"]) if row["jitter_ms"] else None,
                loss_pct=float(row["loss_pct"] or 0.0),
                throughput_mbps=float(row["throughput_mbps"]) if row["throughput_mbps"] else None,
                measured_at=float(row["ts"])))
    return out


def build_dataset(samples: Sequence[_Sample], window: int = 5,
                  horizon_sec: float = HORIZON_SEC) -> tuple[list[list[float]], list[float], list[int]]:
    """Return (X, y_throughput, y_bad) aligned on the same rows."""
    X: list[list[float]] = []
    y_tp: list[float] = []
    y_bad: list[int] = []
    for i in range(len(samples)):
        hist = samples[max(0, i - window + 1): i + 1]
        now = samples[i].measured_at
        feats = features_from_samples(hist, now=now)
        if feats is None:
            continue
        future = None
        for j in range(i + 1, len(samples)):
            if samples[j].measured_at - now >= horizon_sec:
                future = samples[j]
                break
        if future is None:
            continue
        X.append(feats)
        y_tp.append(float(future.throughput_mbps or 0.0))
        y_bad.append(int(is_bad(future.latency_ms, future.loss_pct, future.throughput_mbps)))
    return X, y_tp, y_bad

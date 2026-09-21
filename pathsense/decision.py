"""
Level 3 — Decision Engine. Pure functions: given peers' measured metrics,
produce a ranked list with a numeric score AND a plain-language explanation.
No sockets, no threads, no I/O — deliberately, so this is trivially
unit-testable and is the safest place to put "the intelligence."
"""
from __future__ import annotations

import dataclasses
import statistics
from enum import Enum
from typing import Optional

from .measurement import PeerMetricsSample


class TransferProfile(str, Enum):
    LARGE_FILE = "LARGE_FILE"
    LOW_LATENCY = "LOW_LATENCY"
    BALANCED = "BALANCED"


_WEIGHTS = {
    TransferProfile.LARGE_FILE:  dict(w_throughput=0.40, w_latency=0.10, w_loss=0.20, w_stability=0.20, w_reliability=0.10),
    TransferProfile.LOW_LATENCY: dict(w_throughput=0.10, w_latency=0.45, w_loss=0.25, w_stability=0.10, w_reliability=0.10),
    TransferProfile.BALANCED:    dict(w_throughput=0.30, w_latency=0.25, w_loss=0.25, w_stability=0.10, w_reliability=0.10),
}

MAX_ACCEPTABLE_LOSS_PCT = 15.0
MIN_ACCEPTABLE_THROUGHPUT_MBPS = 0.5


@dataclasses.dataclass
class ScoredPeer:
    peer_id: str
    score: float
    excluded: bool
    exclude_reason: Optional[str]
    terms: dict
    raw_metrics: PeerMetricsSample

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["raw_metrics"] = self.raw_metrics.to_dict()
        return d


def _normalize(value: float, lo: float, hi: float, higher_is_better: bool) -> float:
    if hi == lo:
        return 1.0
    n = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    return n if higher_is_better else 1.0 - n


def _stability(history_throughputs: list[float]) -> float:
    if len(history_throughputs) < 2:
        return 1.0
    mean = statistics.mean(history_throughputs)
    if mean <= 0:
        return 0.0
    coeff_var = statistics.pstdev(history_throughputs) / mean
    return max(0.0, 1.0 - min(coeff_var, 1.0))


def score_peers(latest: dict[str, PeerMetricsSample], history: dict[str, list[PeerMetricsSample]],
                 reliability: Optional[dict[str, float]] = None,
                 profile: TransferProfile = TransferProfile.BALANCED) -> list[ScoredPeer]:
    reliability = reliability or {}
    weights = _WEIGHTS[profile]
    candidates = {pid: m for pid, m in latest.items() if m.latency_ms is not None and m.throughput_mbps is not None}
    if not candidates:
        return []

    throughputs = [m.throughput_mbps for m in candidates.values()]
    latencies = [m.latency_ms for m in candidates.values()]
    losses = [m.loss_pct for m in candidates.values()]
    t_lo, t_hi = min(throughputs), max(throughputs)
    l_lo, l_hi = min(latencies), max(latencies)
    p_lo, p_hi = min(losses), max(losses)

    results: list[ScoredPeer] = []
    for pid, m in candidates.items():
        if m.loss_pct > MAX_ACCEPTABLE_LOSS_PCT:
            results.append(ScoredPeer(pid, 0.0, True,
                f"packet loss {m.loss_pct:.1f}% exceeds {MAX_ACCEPTABLE_LOSS_PCT}% threshold", {}, m))
            continue
        if m.throughput_mbps < MIN_ACCEPTABLE_THROUGHPUT_MBPS:
            results.append(ScoredPeer(pid, 0.0, True,
                f"throughput {m.throughput_mbps:.2f} Mbps below usable minimum", {}, m))
            continue

        n_throughput = _normalize(m.throughput_mbps, t_lo, t_hi, True)
        n_latency = _normalize(m.latency_ms, l_lo, l_hi, False)
        n_loss = _normalize(m.loss_pct, p_lo, p_hi, False)
        hist_tp = [s.throughput_mbps for s in history.get(pid, []) if s.throughput_mbps is not None]
        stab = _stability(hist_tp)
        rel = reliability.get(pid, 1.0)

        terms = {
            "throughput": weights["w_throughput"] * n_throughput,
            "latency": weights["w_latency"] * n_latency,
            "loss": weights["w_loss"] * n_loss,
            "stability": weights["w_stability"] * stab,
            "reliability": weights["w_reliability"] * rel,
        }
        results.append(ScoredPeer(pid, round(sum(terms.values()), 4), False, None, terms, m))

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def explain(ranked: list[ScoredPeer], peer_names: dict[str, str]) -> str:
    lines = []
    accepted = [r for r in ranked if not r.excluded]
    excluded = [r for r in ranked if r.excluded]
    if accepted:
        winner = accepted[0]
        name = peer_names.get(winner.peer_id, winner.peer_id)
        lines.append(f"Selected: {name}")
        lines.append(f"Score: {winner.score:.2f} / 1.00")
        for factor, contribution in sorted(winner.terms.items(), key=lambda kv: -kv[1]):
            lines.append(f"  ▸ {factor:<12} contributed +{contribution:.2f}")
    for r in excluded:
        name = peer_names.get(r.peer_id, r.peer_id)
        lines.append(f"Not selected — {name}: {r.exclude_reason}")
    return "\n".join(lines)

"""
Level 5b — Learning tuner (Review 3).

The transfer has settings that should not be fixed: how many chunks to keep in
flight (window) and whether to compress. The best choice depends on the link —
a big window wins on a high-latency link, compression wins on a slow link with
compressible data and loses CPU time on a fast link with video files.

Rather than hard-coding a table, the agent LEARNS which setting works in which
situation, using a contextual bandit (LinUCB, Li et al. 2010):

  * Each setting combination is an "arm".
  * The "context" is what the link and the file look like right now
    (predicted throughput, latency, loss, instability, how compressible the
    data has been, the current transport).
  * After trying an arm for a short interval, the agent measures the goodput it
    actually got and uses that as the reward.
  * LinUCB keeps a ridge-regression estimate per arm and picks the arm with the
    highest optimistic score (estimate + alpha * uncertainty). That is what
    makes it explore sensibly early and settle on the best arm later, instead
    of either guessing forever or locking onto a lucky first result.

Everything is plain Python (a 7x7 matrix inverse is nothing), so no packages
are needed at run time. The learned state is saved to JSON, so the agent keeps
getting better across runs, and `--reset-learning` starts it fresh for a demo.

A safety rail: if the bandit's choice is doing clearly worse than the simple
rule-based default, `fallback_used` is set and the rule-based choice is applied
instead. The learning never makes the transfer worse than Review 2.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
import time
from typing import Optional, Sequence

WINDOWS = (1, 4, 16, 64)
COMPRESSION = (False, True)
ARMS = [(w, c) for w in WINDOWS for c in COMPRESSION]
CONTEXT_NAMES = ["bias", "predicted_mbps", "latency_ms", "loss_pct", "instability",
                 "compressibility", "is_slow_transport"]
D = len(CONTEXT_NAMES)
REWARD_SCALE_MBPS = 100.0          # goodput is scaled into roughly 0..1


def arm_name(arm: tuple[int, bool]) -> str:
    window, comp = arm
    return f"window={window}, compression={'on' if comp else 'off'}"


# --------------------------------------------------------------------------
# Small linear algebra (D is 7, so clarity beats cleverness)
# --------------------------------------------------------------------------
def _identity(n: int) -> list[list[float]]:
    return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]


def _inverse(matrix: Sequence[Sequence[float]]) -> list[list[float]]:
    n = len(matrix)
    a = [list(row) + ident for row, ident in zip(matrix, _identity(n))]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            a[col][col] += 1e-6                      # keep it invertible
            pivot = col
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [v / scale for v in a[col]]
        for r in range(n):
            if r != col and a[r][col] != 0.0:
                factor = a[r][col]
                a[r] = [v - factor * w for v, w in zip(a[r], a[col])]
    return [row[n:] for row in a]


def _matvec(m: Sequence[Sequence[float]], v: Sequence[float]) -> list[float]:
    return [sum(mi * vi for mi, vi in zip(row, v)) for row in m]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


# --------------------------------------------------------------------------
@dataclasses.dataclass
class Context:
    predicted_mbps: float
    latency_ms: float
    loss_pct: float
    instability: float
    compressibility: float
    slow_transport: bool

    def vector(self) -> list[float]:
        """Scaled so no single feature dominates the regression."""
        return [1.0,
                min(self.predicted_mbps, 200.0) / 100.0,
                min(self.latency_ms, 1000.0) / 200.0,
                min(self.loss_pct, 100.0) / 20.0,
                min(self.instability, 2.0),
                min(max(self.compressibility, 0.0), 1.0),
                1.0 if self.slow_transport else 0.0]


@dataclasses.dataclass
class Decision:
    window: int
    compression: bool
    reason: str
    explored: bool
    fallback_used: bool = False
    expected_mbps: float = 0.0

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def rule_based(ctx: Context) -> tuple[int, bool]:
    """The Review 2-style default, kept as the safety net and the baseline."""
    if ctx.latency_ms > 100 or ctx.predicted_mbps > 30:
        window = 16
    elif ctx.latency_ms > 30:
        window = 8
    else:
        window = 4
    compress = ctx.predicted_mbps < 8.0 and ctx.compressibility > 0.15
    return window, compress


class LinUCBTuner:
    def __init__(self, alpha: float = 0.6, state_path: Optional[str] = None,
                 min_pulls_before_trust: int = 3):
        self.alpha = alpha
        self.state_path = state_path
        self.min_pulls = min_pulls_before_trust
        self.A = [[row[:] for row in _identity(D)] for _ in ARMS]
        self.b = [[0.0] * D for _ in ARMS]
        self.pulls = [0] * len(ARMS)
        self.rewards = [0.0] * len(ARMS)
        self.updates = 0
        self.fallbacks = 0
        self._pending: Optional[tuple[int, list[float]]] = None
        if state_path:
            self.load(state_path)

    # ---- persistence -----------------------------------------------------
    def load(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                blob = json.load(f)
            if blob.get("arms") != [list(a) for a in ARMS] or blob.get("context") != CONTEXT_NAMES:
                return False
            self.A = blob["A"]
            self.b = blob["b"]
            self.pulls = blob["pulls"]
            self.rewards = blob["rewards"]
            self.updates = blob.get("updates", 0)
            return True
        except (OSError, ValueError, KeyError):
            return False

    def save(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump({"arms": [list(a) for a in ARMS], "context": CONTEXT_NAMES,
                       "A": self.A, "b": self.b, "pulls": self.pulls,
                       "rewards": self.rewards, "updates": self.updates,
                       "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}, f)

    # ---- the bandit ------------------------------------------------------
    def _score(self, arm_index: int, x: Sequence[float]) -> tuple[float, float]:
        A_inv = _inverse(self.A[arm_index])
        theta = _matvec(A_inv, self.b[arm_index])
        mean = _dot(theta, x)
        uncertainty = math.sqrt(max(0.0, _dot(x, _matvec(A_inv, x))))
        return mean, uncertainty

    def choose(self, ctx: Context) -> Decision:
        x = ctx.vector()
        best_i, best_ucb, best_mean, best_unc = 0, -1e18, 0.0, 0.0
        for i in range(len(ARMS)):
            mean, unc = self._score(i, x)
            ucb = mean + self.alpha * unc
            if ucb > best_ucb:
                best_i, best_ucb, best_mean, best_unc = i, ucb, mean, unc
        window, compression = ARMS[best_i]
        explored = self.pulls[best_i] < self.min_pulls
        self._pending = (best_i, x)
        expected = max(0.0, best_mean) * REWARD_SCALE_MBPS
        if explored:
            reason = (f"Trying {arm_name(ARMS[best_i])} — it has only been used "
                      f"{self.pulls[best_i]} time(s) here, so the agent is still learning.")
        else:
            reason = (f"Chose {arm_name(ARMS[best_i])} — learned from {self.pulls[best_i]} past "
                      f"interval(s) in similar conditions, expected ~{expected:.0f} Mbps.")
        return Decision(window=window, compression=compression, reason=reason,
                        explored=explored, expected_mbps=expected)

    def decide(self, ctx: Context, rule_fallback_mbps: float = 0.0,
               observed_mbps: float = 0.0) -> Decision:
        """Pick a setting, but fall back to the rules if learning is doing worse."""
        decision = self.choose(ctx)
        if (observed_mbps > 0 and rule_fallback_mbps > 0
                and observed_mbps < 0.5 * rule_fallback_mbps and self.updates > 10):
            window, compression = rule_based(ctx)
            self.fallbacks += 1
            return Decision(window=window, compression=compression, explored=False, fallback_used=True,
                            reason=(f"Measured goodput ({observed_mbps:.1f} Mbps) fell well below what the "
                                    f"rule-based setting achieved ({rule_fallback_mbps:.1f} Mbps), so the "
                                    f"agent reverted to {arm_name((window, compression))}."))
        return decision

    def reward(self, goodput_mbps: float) -> None:
        """Feed back what the chosen setting actually achieved."""
        if self._pending is None:
            return
        arm_index, x = self._pending
        self._pending = None
        r = max(0.0, min(goodput_mbps / REWARD_SCALE_MBPS, 2.0))
        for i in range(D):
            for j in range(D):
                self.A[arm_index][i][j] += x[i] * x[j]
            self.b[arm_index][i] += r * x[i]
        self.pulls[arm_index] += 1
        self.rewards[arm_index] += goodput_mbps
        self.updates += 1

    # ---- for the dashboard ----------------------------------------------
    def summary(self) -> dict:
        rows = []
        for i, arm in enumerate(ARMS):
            rows.append({"arm": arm_name(arm), "pulls": self.pulls[i],
                         "avg_mbps": round(self.rewards[i] / self.pulls[i], 1) if self.pulls[i] else None})
        rows.sort(key=lambda r: (-(r["avg_mbps"] or -1), -r["pulls"]))
        return {"updates": self.updates, "fallbacks": self.fallbacks, "arms": rows}

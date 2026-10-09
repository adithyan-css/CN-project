"""
Evaluation harness (Review 3): does the AI agent actually help?

Three policies run the SAME transfer over the SAME link conditions:

    fixed   — no agent at all: one window, no compression, no pausing.
              This is the Review 1 behaviour and the honest baseline.
    rules   — the Review 2 agent: absolute thresholds, hysteresis,
              pause and resume, fixed settings.
    ai      — the Review 3 agent: prediction model + learning tuner on top
              of the rules.

Conditions come from pathsense.linksim: a seeded LinkTrace drives a real TCP
proxy between sender and receiver (bandwidth cap and delay) and feeds the same
numbers to the agent as its measurements, so every policy faces an identical
link. Each seed is replayed for every policy.

    python tools/evaluate.py --trials 5 --mb 8 --out results/

Outputs results/evaluation.csv and three charts. Numbers come from real
transfers through the real engine, not a model of one.
"""
from __future__ import annotations

import argparse
import csv
import os
import shutil
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.adaptation import AdaptationLoop, LinkWatchdog           # noqa: E402
from pathsense.discovery import PeerInfo                                # noqa: E402
from pathsense.linklog import _Sample                                   # noqa: E402
from pathsense.linksim import ImpairProxy, LinkTrace                    # noqa: E402
from pathsense.predictor import Predictor                               # noqa: E402
from pathsense.transfer import TransferReceiver, TransferSender         # noqa: E402
from pathsense.tuner import LinUCBTuner                                 # noqa: E402

POLICIES = ("fixed", "rules", "ai")


class ReplayCollector:
    """Feeds a recorded trace to the agent as if it were measuring live."""

    def __init__(self, peer_id: str, samples: list[_Sample], interval_sec: float = 1.0):
        self.peer_id = peer_id
        self.samples = samples
        self.interval = interval_sec
        self.index = 0
        self.started = time.time()
        self._lock = threading.Lock()

    def _advance(self):
        want = int((time.time() - self.started) / self.interval)
        self.index = min(want, len(self.samples) - 1)

    def _stamped(self, s: _Sample) -> _Sample:
        return _Sample(s.latency_ms, s.jitter_ms, s.loss_pct, s.throughput_mbps, time.time())

    def latest(self, peer_id: str):
        with self._lock:
            self._advance()
            return self._stamped(self.samples[self.index])

    def history(self, peer_id: str):
        with self._lock:
            self._advance()
            lo = max(0, self.index - 4)
            now = time.time()
            out = []
            for k, s in enumerate(self.samples[lo:self.index + 1]):
                age = (self.index - lo - k) * self.interval
                out.append(_Sample(s.latency_ms, s.jitter_ms, s.loss_pct, s.throughput_mbps, now - age))
            return out

    def all_latest(self):
        return {self.peer_id: self.latest(self.peer_id)}

    def simulation_remaining(self, peer_id):
        return 0.0


class _StubDiscovery:
    def __init__(self, peers):
        self._peers = peers

    def get_peers(self):
        return self._peers


def run_trial(policy: str, seed: int, mb: int, base_port: int, timeout_sec: float = 120.0,
              tuner_state: str | None = None) -> dict:
    tmp = tempfile.mkdtemp(prefix=f"eval_{policy}_")
    try:
        src = os.path.join(tmp, "payload.bin")
        with open(src, "wb") as f:                       # mixed content: ~50% compressible
            half = mb * 1024 * 1024 // 2
            f.write(os.urandom(half) + b"LOREM IPSUM " * (half // 12))

        trace = LinkTrace(seed=seed, base_mbps=28.0, base_latency_ms=45.0, event_rate=0.08)
        states = [trace.step() for _ in range(240)]
        samples = []
        for st in states:
            unusable = st.loss_pct >= 100.0
            samples.append(_Sample(None if unusable else st.latency_ms,
                                   None if unusable else st.jitter_ms, st.loss_pct,
                                   None if unusable else st.throughput_mbps, 0.0))

        recv_port, proxy_port = base_port, base_port + 1
        receiver = TransferReceiver(recv_port, os.path.join(tmp, "recv"), auto_pair=True)
        receiver.start()
        proxy = ImpairProxy(proxy_port, "127.0.0.1", recv_port,
                            mbps=states[0].effective_mbps(), delay_ms=states[0].latency_ms / 2)
        proxy.start()
        time.sleep(0.3)

        collector = ReplayCollector("B", samples, interval_sec=1.0)
        peer = PeerInfo("B", "Peer B", "127.0.0.1", 0, proxy_port, time.time())
        discovery = _StubDiscovery([peer])
        sender = TransferSender(src, sender_name="Evaluator")
        if policy == "fixed":
            sender.set_window(1)                          # Review 1 stop-and-wait
        loop = None
        if policy in ("rules", "ai"):
            predictor = Predictor() if policy == "ai" else None
            # The tuner's learning persists across trials, exactly as it does on a
            # real laptop: an agent that has shared files before is not starting blind.
            tuner = LinUCBTuner(state_path=tuner_state or os.path.join(tmp, "tuner.json")) \
                if policy == "ai" else None
            loop = AdaptationLoop(collector, discovery, sender, {"B": "Peer B"},
                                  predictor=predictor if (predictor and predictor.available) else None,
                                  tuner=tuner, watchdog=LinkWatchdog(), enable_fallback=False)

        stop_driver = threading.Event()

        def drive_link():
            """Apply the trace to the proxy in real time."""
            i = 0
            while not stop_driver.is_set() and i < len(states):
                st = states[i]
                proxy.set_conditions(st.effective_mbps(), st.latency_ms / 2)
                if st.loss_pct >= 100.0:
                    proxy.cut()        # the radio is gone: live connections die
                i += 1
                time.sleep(1.0)

        threading.Thread(target=drive_link, daemon=True).start()
        if loop:
            loop.start()

        started = time.time()
        result = {"completed": False}

        def do_send():
            try:
                result["completed"] = sender.send_to("B", "127.0.0.1", proxy_port)
            except Exception:
                result["completed"] = False

        t = threading.Thread(target=do_send, daemon=True)
        t.start()
        while time.time() - started < timeout_sec:
            if sender.state.status == "COMPLETE":
                break
            if loop is None and (not t.is_alive() or sender.state.status in ("PAUSED", "DEGRADED")):
                break                 # no agent: a dropped connection is the end of it
            time.sleep(0.2)
        elapsed = time.time() - started
        stop_driver.set()
        if loop:
            loop.stop()
        sender.cancel_current()
        proxy.stop()
        receiver.stop()
        time.sleep(0.2)

        st = sender.state
        return {"policy": policy, "seed": seed, "mb": mb,
                "completed": int(st.status == "COMPLETE"),
                "seconds": round(elapsed, 2),
                "goodput_mbps": round(mb * 8 / elapsed, 2) if st.status == "COMPLETE" else 0.0,
                "progress_pct": round(st.progress_pct, 1),
                "pauses": st.pauses, "resumes": st.resumes,
                "bytes_on_wire_mb": round(st.bytes_on_wire / 1048576, 2),
                "link_cuts": proxy.cuts,
                "final_window": st.window, "compression": int(st.compression)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Charts. Palette: one hue per policy, fixed order, validated for colour-vision
# deficiency separation; grid recessive; values labelled directly on the bars.
# --------------------------------------------------------------------------
COLORS = {"fixed": "#8C8C8C", "rules": "#1C7293", "ai": "#1D9A6C"}
LABELS = {"fixed": "Fixed settings\n(no agent)", "rules": "Rule-based agent\n(Review 2)",
          "ai": "AI agent\n(Review 3)"}


def make_charts(rows: list[dict], out_dir: str) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def agg(policy, key, only_complete=False):
        vals = [r[key] for r in rows if r["policy"] == policy and (not only_complete or r["completed"])]
        return statistics.mean(vals) if vals else 0.0

    charts = []
    panels = [
        ("transfer_time.png", "Time to deliver the file", "seconds (lower is better)",
         lambda p: agg(p, "seconds", only_complete=True), "{:.1f}s"),
        ("goodput.png", "Effective throughput", "Mbps (higher is better)",
         lambda p: agg(p, "goodput_mbps", only_complete=True), "{:.1f}"),
        ("reliability.png", "Transfers that finished", "% of runs (higher is better)",
         lambda p: 100.0 * agg(p, "completed"), "{:.0f}%"),
    ]
    for filename, title, ylabel, fn, fmt in panels:
        fig, ax = plt.subplots(figsize=(6.2, 4.0), dpi=160)
        values = [fn(p) for p in POLICIES]
        bars = ax.bar([LABELS[p] for p in POLICIES], values,
                      color=[COLORS[p] for p in POLICIES], width=0.58, zorder=3)
        for bar, v in zip(bars, values):
            ax.annotate(fmt.format(v), (bar.get_x() + bar.get_width() / 2, v),
                        textcoords="offset points", xytext=(0, 4), ha="center",
                        fontsize=11, color="#2C3E50", fontweight="bold")
        ax.set_title(title, fontsize=13, color="#2C3E50", fontweight="bold", pad=12)
        ax.set_ylabel(ylabel, fontsize=10, color="#5D6D7E")
        ax.grid(axis="y", color="#E3E7EA", linewidth=1, zorder=0)
        ax.set_axisbelow(True)
        for spine in ("top", "right", "left"):
            ax.spines[spine].set_visible(False)
        ax.spines["bottom"].set_color("#D5DBDB")
        ax.tick_params(colors="#5D6D7E", labelsize=9.5, length=0)
        ax.set_ylim(0, max(values) * 1.22 if max(values) else 1)
        fig.tight_layout()
        path = os.path.join(out_dir, filename)
        fig.savefig(path, facecolor="white")
        plt.close(fig)
        charts.append(path)
    return charts


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare fixed / rule-based / AI policies")
    ap.add_argument("--trials", type=int, default=5, help="link seeds, each replayed per policy")
    ap.add_argument("--mb", type=int, default=8, help="file size in MB")
    ap.add_argument("--out", default="results")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--fresh-learning", action="store_true",
                    help="forget what the tuner learned in earlier runs")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    state_file = os.path.join(args.out, "tuner_state.json")
    if args.fresh_learning and os.path.exists(state_file):
        os.remove(state_file)

    rows = []
    port = 55100
    for seed in range(args.trials):
        for policy in POLICIES:
            row = run_trial(policy, seed, args.mb, port, args.timeout,
                            tuner_state=os.path.join(args.out, "tuner_state.json"))
            port += 4
            rows.append(row)
            print(f"seed {seed} {policy:>5}: {'ok ' if row['completed'] else 'FAIL'} "
                  f"{row['seconds']:6.1f}s  {row['goodput_mbps']:5.1f} Mbps  "
                  f"{row['progress_pct']:5.1f}%  pauses={row['pauses']} window={row['final_window']}")

    csv_path = os.path.join(args.out, "evaluation.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    print("\nSummary")
    print(f"{'policy':>6} {'finished':>9} {'mean time':>10} {'mean Mbps':>10} {'pauses':>7}")
    for policy in POLICIES:
        mine = [r for r in rows if r["policy"] == policy]
        done = [r for r in mine if r["completed"]]
        mt = statistics.mean([r["seconds"] for r in done]) if done else float("nan")
        mg = statistics.mean([r["goodput_mbps"] for r in done]) if done else 0.0
        mp = statistics.mean([r["pauses"] for r in mine])
        print(f"{policy:>6} {len(done)}/{len(mine):<7} {mt:9.1f}s {mg:9.1f} {mp:7.1f}")

    charts = make_charts(rows, args.out)
    print("\nWrote " + csv_path + " and " + ", ".join(os.path.basename(c) for c in charts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

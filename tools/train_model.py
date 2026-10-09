"""
Train the link-degradation models and export them as plain JSON.

    python tools/train_model.py                        # train on simulated traces
    python tools/train_model.py --log data/links.csv   # train on a real capture

Training needs scikit-learn (a one-off step on a development machine); the
exported JSON is read by pathsense/predictor.py with no dependencies at all, so
the laptops running the demo need nothing installed.

The script always prints a comparison against the obvious baseline — "assume
the link stays exactly as it is now" (persistence). A model that cannot beat
persistence is not worth shipping, and saying so is part of the result.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.linklog import FEATURE_NAMES, build_dataset, read_log, is_bad   # noqa: E402
from pathsense.linksim import LinkTrace                                        # noqa: E402
from pathsense.predictor import DEFAULT_MODEL_PATH                             # noqa: E402


def export_tree(tree) -> dict:
    t = tree.tree_
    return {"feature": [int(f) for f in t.feature],
            "threshold": [float(x) for x in t.threshold],
            "left": [int(x) for x in t.children_left],
            "right": [int(x) for x in t.children_right],
            "value": [float(v[0][0]) for v in t.value]}


def export_gbdt(model) -> dict:
    return {"init": float(model.init_.constant_[0][0]) if hasattr(model.init_, "constant_")
            else float(model._raw_predict_init(__import__("numpy").zeros((1, len(FEATURE_NAMES))))[0][0]),
            "lr": float(model.learning_rate),
            "trees": [export_tree(est[0]) for est in model.estimators_]}


def simulated_samples(traces: int, per_trace: int):
    out = []
    for seed in range(traces):
        out.append(LinkTrace(seed=seed, event_rate=0.05).samples(per_trace, start_ts=seed * 100000.0))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Train the NetShare link models")
    ap.add_argument("--log", action="append", default=[], help="CSV written by LinkLogger (repeatable)")
    ap.add_argument("--traces", type=int, default=120, help="simulated traces when no log is given")
    ap.add_argument("--per-trace", type=int, default=400)
    ap.add_argument("--out", default=DEFAULT_MODEL_PATH)
    args = ap.parse_args()

    try:
        import numpy as np
        from sklearn.ensemble import GradientBoostingClassifier, GradientBoostingRegressor
        from sklearn.metrics import mean_absolute_error, roc_auc_score
    except ImportError:
        print("Training needs scikit-learn:  pip install scikit-learn", file=sys.stderr)
        return 2

    if args.log:
        groups = [read_log(p) for p in args.log]
        source = f"{len(groups)} log file(s): " + ", ".join(args.log)
    else:
        groups = simulated_samples(args.traces, args.per_trace)
        source = f"{args.traces} simulated traces x {args.per_trace} samples"

    X, y_tp, y_bad = [], [], []
    for samples in groups:
        gx, gtp, gbad = build_dataset(samples)
        X += gx
        y_tp += gtp
        y_bad += gbad
    if len(X) < 200:
        print(f"Only {len(X)} usable rows — need more data", file=sys.stderr)
        return 1

    X = np.array(X, dtype=float)
    y_tp = np.array(y_tp, dtype=float)
    y_bad = np.array(y_bad, dtype=int)
    cut = int(len(X) * 0.75)                       # time-ordered split, never shuffled
    Xtr, Xte, ytr, yte = X[:cut], X[cut:], y_tp[:cut], y_tp[cut:]
    btr, bte = y_bad[:cut], y_bad[cut:]
    print(f"Rows: {len(X)} (train {len(Xtr)}, test {len(Xte)}) from {source}")
    print(f"Link unusable in {100 * y_bad.mean():.1f}% of rows (horizon 5 s)")

    tp_model = GradientBoostingRegressor(n_estimators=120, max_depth=3, learning_rate=0.08,
                                         subsample=0.9, random_state=0).fit(Xtr, ytr)
    risk_model = GradientBoostingClassifier(n_estimators=120, max_depth=3, learning_rate=0.08,
                                            subsample=0.9, random_state=0).fit(Xtr, btr)

    tp_pred = tp_model.predict(Xte)
    persistence = Xte[:, FEATURE_NAMES.index("throughput_mbps")]
    mae_model = mean_absolute_error(yte, tp_pred)
    mae_base = mean_absolute_error(yte, persistence)
    risk_pred = risk_model.predict_proba(Xte)[:, 1]
    auc = roc_auc_score(bte, risk_pred) if len(set(bte)) > 1 else float("nan")
    base_bad = np.array([is_bad(x[0], x[2], x[3]) for x in Xte], dtype=int)
    auc_base = roc_auc_score(bte, base_bad) if len(set(bte)) > 1 else float("nan")
    print(f"Throughput MAE:  model {mae_model:.2f} Mbps vs persistence {mae_base:.2f} Mbps "
          f"({100 * (mae_base - mae_model) / mae_base:+.1f}% better)")
    print(f"Degradation AUC: model {auc:.3f} vs 'it is bad right now' rule {auc_base:.3f}")

    blob = {"features": FEATURE_NAMES,
            "throughput": export_gbdt(tp_model),
            "risk": export_gbdt(risk_model),
            "metadata": {"trained_at": time.strftime("%Y-%m-%d %H:%M:%S"), "source": source,
                         "rows": int(len(X)), "horizon_sec": 5.0,
                         "test_mae_mbps": round(float(mae_model), 3),
                         "baseline_mae_mbps": round(float(mae_base), 3),
                         "test_auc": round(float(auc), 4),
                         "baseline_auc": round(float(auc_base), 4)}}
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(blob, f)
    size_kb = os.path.getsize(args.out) / 1024
    print(f"Wrote {args.out} ({size_kb:.0f} KB, {len(blob['throughput']['trees'])} + "
          f"{len(blob['risk']['trees'])} trees)")

    # Cross-check: the hand-written inference must match scikit-learn exactly.
    from pathsense.predictor import Predictor
    p = Predictor(args.out)
    worst = max(abs(p.predict(list(x)).throughput_mbps - max(0.0, float(m)))
                for x, m in zip(Xte[:200], tp_model.predict(Xte[:200])))
    print(f"Pure-Python inference matches scikit-learn to {worst:.2e} Mbps")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

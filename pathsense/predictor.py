"""
Level 3c — Link prediction (Review 3). The AI the agent acts on.

Two gradient-boosted tree models, trained offline by tools/train_model.py:

  * a regressor predicting the link's throughput HORIZON_SEC (5 s) from now;
  * a classifier predicting the probability that the link will be unusable
    then (too lossy, too slow, or gone).

Why trees rather than a neural network: the dataset is small and tabular,
boosted trees are the strongest model class there, they train in seconds on a
laptop, and a tree is inspectable, which matters when the agent has to explain
itself.

Why inference is written out by hand here: the exported model is a plain JSON
file of thresholds, so the agent needs no scikit-learn, numpy or any other
package at run time. The laptops in the demo keep running pure-standard-library
Python, and the prediction costs microseconds.

If no model file is present, `Predictor.available` is False and the agent falls
back to the Review 2 rules. The system never depends on the model being there.
"""
from __future__ import annotations

import dataclasses
import json
import math
import os
from typing import Optional, Sequence

from .linklog import FEATURE_NAMES, HORIZON_SEC, features_from_samples

DEFAULT_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "link_model.json")


def _tree_value(tree: dict, x: Sequence[float]) -> float:
    node = 0
    feature, threshold = tree["feature"], tree["threshold"]
    left, right, value = tree["left"], tree["right"], tree["value"]
    while feature[node] >= 0:
        node = left[node] if x[feature[node]] <= threshold[node] else right[node]
    return value[node]


def _gbdt(model: dict, x: Sequence[float]) -> float:
    return model["init"] + model["lr"] * sum(_tree_value(t, x) for t in model["trees"])


@dataclasses.dataclass
class Forecast:
    throughput_mbps: float          # predicted throughput in HORIZON_SEC seconds
    risk: float                     # probability the link will be unusable then
    horizon_sec: float = HORIZON_SEC
    source: str = "model"           # "model" | "rules"

    @property
    def will_degrade(self) -> bool:
        return self.risk >= 0.5

    def to_dict(self) -> dict:
        return {"throughput_mbps": round(self.throughput_mbps, 2), "risk": round(self.risk, 3),
                "horizon_sec": self.horizon_sec, "source": self.source,
                "will_degrade": self.will_degrade}

    def sentence(self, peer_name: str) -> str:
        if self.source == "rules":
            return f"No trained model loaded; using the rule-based check for {peer_name}."
        if self.will_degrade:
            return (f"Predicted: {peer_name}'s link is likely to be unusable within "
                    f"{self.horizon_sec:.0f}s (risk {self.risk:.0%}, forecast "
                    f"{self.throughput_mbps:.1f} Mbps).")
        return (f"Predicted: {peer_name}'s link stays usable for the next "
                f"{self.horizon_sec:.0f}s (risk {self.risk:.0%}, forecast "
                f"{self.throughput_mbps:.1f} Mbps).")


class Predictor:
    def __init__(self, path: str = DEFAULT_MODEL_PATH):
        self.path = path
        self.throughput_model: Optional[dict] = None
        self.risk_model: Optional[dict] = None
        self.metadata: dict = {}
        self.load(path)

    @property
    def available(self) -> bool:
        return self.throughput_model is not None and self.risk_model is not None

    def load(self, path: str) -> bool:
        if not path or not os.path.exists(path):
            return False
        try:
            with open(path) as f:
                blob = json.load(f)
            if blob.get("features") != FEATURE_NAMES:
                return False
            self.throughput_model = blob["throughput"]
            self.risk_model = blob["risk"]
            self.metadata = blob.get("metadata", {})
            return True
        except (OSError, ValueError, KeyError):
            self.throughput_model = self.risk_model = None
            return False

    def predict(self, features: Sequence[float]) -> Forecast:
        if not self.available:
            raise RuntimeError("no model loaded")
        tp = max(0.0, _gbdt(self.throughput_model, features))
        logit = _gbdt(self.risk_model, features)
        risk = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, logit))))
        return Forecast(throughput_mbps=tp, risk=risk)

    def forecast(self, samples: Sequence, now: Optional[float] = None) -> Optional[Forecast]:
        """Convenience: history of PeerMetricsSample -> Forecast (None if impossible)."""
        if not self.available:
            return None
        feats = features_from_samples(samples, now=now)
        if feats is None:
            return None
        return self.predict(feats)

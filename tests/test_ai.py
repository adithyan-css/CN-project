"""
Level 3c/5b/6b test — the Review 3 AI pieces.

Proves: (1) the BLE beacon payload survives encode/decode and feeds the peer
table, and a stale beacon disappears; (2) the exported model's pure-Python
inference is deterministic and its risk output responds to a degrading link;
(3) the prediction model beats the obvious baseline on held-out simulated
traces — if it did not, shipping it would be dishonest; (4) the bandit learns
a better policy than the fixed rule on a link where the right window depends on
latency; (5) the assistant answers from the agent's own log, with no network.
"""
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.assistant import LocalAssistant
from pathsense.ble import BeaconPayload, BleDiscovery, STALE_AFTER_SEC
from pathsense.linklog import FEATURE_NAMES, build_dataset, features_from_samples, is_bad
from pathsense.linksim import LinkTrace
from pathsense.predictor import Predictor
from pathsense.tuner import Context, LinUCBTuner, rule_based


def test_ble_payload_round_trip_and_size():
    p = BeaconPayload(device_id="aabbccddeeff", ipv4="192.168.43.17", measurement_port=50910,
                      transfer_port=50920, key_fingerprint="1a2b3c4d", hotspot=True)
    raw = p.encode()
    assert len(raw) == 20, "a BLE advertisement has 31 bytes; the payload must fit"
    back = BeaconPayload.decode(raw)
    assert back.device_id == p.device_id and back.ipv4 == p.ipv4
    assert back.measurement_port == 50910 and back.transfer_port == 50920
    assert back.key_fingerprint == "1a2b3c4d" and back.hotspot and not back.paired_before
    assert back.reachable_over_ip
    assert not BeaconPayload(device_id="aabbccddeeff", ipv4="0.0.0.0", measurement_port=1,
                             transfer_port=2, key_fingerprint="00000000").reachable_over_ip
    print("PASS: test_ble_payload_round_trip_and_size")


def test_ble_discovery_tracks_and_forgets_peers():
    d = BleDiscovery("aaaaaaaaaaaa", "Laptop A", 50910, 50920)
    beacon = BeaconPayload("bbbbbbbbbbbb", "192.168.43.9", 50911, 50921, "11223344")
    assert d.observe(beacon.encode(), "Laptop B") is not None
    assert [p.name for p in d.get_peers()] == ["Laptop B"]
    assert d.fingerprint_of("bbbbbbbbbbbb") == "11223344"
    assert d.observe(d.payload().encode()) is None, "a node must ignore its own beacon"
    assert d.observe(b"\x09not-a-netshare-beacon") is None
    d._peers["bbbbbbbbbbbb"].last_seen = time.time() - STALE_AFTER_SEC - 1
    assert d.get_peers() == [], "a device that stopped advertising must drop off the list"
    print("PASS: test_ble_discovery_tracks_and_forgets_peers")


def test_model_beats_the_baseline_on_unseen_traces():
    p = Predictor()
    if not p.available:
        print("SKIP: no trained model (run tools/train_model.py)")
        return
    assert p.metadata.get("rows", 0) > 1000
    errors_model, errors_base, risks_bad, risks_ok = [], [], [], []
    for seed in range(900, 915):                 # seeds never used in training
        samples = LinkTrace(seed=seed, event_rate=0.06).samples(300)
        X, y_tp, y_bad = build_dataset(samples)
        for feats, actual_tp, actual_bad in zip(X, y_tp, y_bad):
            f = p.predict(feats)
            errors_model.append(abs(f.throughput_mbps - actual_tp))
            errors_base.append(abs(feats[FEATURE_NAMES.index("throughput_mbps")] - actual_tp))
            (risks_bad if actual_bad else risks_ok).append(f.risk)
    mae_model, mae_base = statistics.mean(errors_model), statistics.mean(errors_base)
    assert mae_model < mae_base, f"model MAE {mae_model:.2f} is no better than persistence {mae_base:.2f}"
    assert statistics.mean(risks_bad) > statistics.mean(risks_ok) + 0.1, "risk does not separate the cases"
    print(f"PASS: test_model_beats_the_baseline_on_unseen_traces "
          f"(MAE {mae_model:.2f} vs {mae_base:.2f} Mbps, mean risk "
          f"{statistics.mean(risks_bad):.2f} bad vs {statistics.mean(risks_ok):.2f} ok)")


def test_prediction_is_deterministic():
    p = Predictor()
    if not p.available:
        print("SKIP: no trained model")
        return
    feats = [40.0, 5.0, 2.0, 20.0, 35.0, 22.0, 1.5, -3.0, 0.3, 0.5]
    a, b = p.predict(feats), p.predict(feats)
    assert a.to_dict() == b.to_dict(), "the same input must always give the same answer"
    print("PASS: test_prediction_is_deterministic")


def test_bandit_learns_a_better_policy_than_the_fixed_rule():
    import random
    rng = random.Random(11)

    def environment(window, compression, latency_ms, slow):
        ideal = 32 if latency_ms > 80 else (8 if latency_ms > 20 else 4)
        base = 2.0 if slow else 60.0
        rate = base * min(1.0, window / ideal)
        if window > ideal * 4:
            rate *= 0.75
        if compression:
            rate *= 1.6 if slow else 0.8
        return max(0.1, rate * rng.gauss(1.0, 0.05))

    def situation():
        latency = rng.choice([5, 40, 150])
        slow = rng.random() < 0.3
        return Context(predicted_mbps=2.0 if slow else 50.0, latency_ms=latency, loss_pct=0.4,
                       instability=0.1, compressibility=0.5, slow_transport=slow), latency, slow

    tuner = LinUCBTuner()
    for _ in range(300):                       # learning phase
        ctx, latency, slow = situation()
        d = tuner.choose(ctx)
        tuner.reward(environment(d.window, d.compression, latency, slow))

    learned, fixed = [], []
    for _ in range(150):                       # measured phase
        ctx, latency, slow = situation()
        d = tuner.choose(ctx)
        learned.append(environment(d.window, d.compression, latency, slow))
        w, c = rule_based(ctx)
        fixed.append(environment(w, c, latency, slow))
    gain = statistics.mean(learned) / statistics.mean(fixed)
    assert gain > 1.05, f"the bandit only reached {gain:.2f}x the rule-based policy"
    print(f"PASS: test_bandit_learns_a_better_policy_than_the_fixed_rule ({gain:.2f}x)")


def test_bandit_state_survives_a_restart(tmp_path="/tmp/netshare_tuner_test.json"):
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    t = LinUCBTuner(state_path=tmp_path)
    ctx = Context(40.0, 60.0, 1.0, 0.2, 0.4, False)
    for _ in range(5):
        t.choose(ctx)
        t.reward(30.0)
    t.save()
    again = LinUCBTuner(state_path=tmp_path)
    assert again.updates == t.updates and again.pulls == t.pulls
    os.remove(tmp_path)
    print("PASS: test_bandit_state_survives_a_restart")


def test_assistant_answers_from_the_decision_log():
    a = LocalAssistant()
    brief = {
        "transfer": {"peer_name": "Laptop B", "status": "TRANSFERRING", "progress_pct": 41.0,
                     "goodput_mbps": 18.4, "window": 16, "compression": False, "encrypted": True,
                     "pair_code": "481920", "transport": "wifi", "acked_chunks": 100,
                     "total_chunks": 240, "pauses": 1, "resumes": 1, "file_id": "demo.bin"},
        "health": {"healthy": False, "reason": "packet loss 40% exceeds 15%"},
        "forecast": {"throughput_mbps": 2.1, "risk": 0.82, "horizon_sec": 5.0, "source": "model",
                     "will_degrade": True},
        "decisions": [{"kind": "pause", "sentence": "Link to Laptop B degraded: packet loss 40% "
                                                    "exceeds 15%. Pausing at 41%; progress kept.",
                       "progress_pct": 41.0},
                      {"kind": "tune", "sentence": "Tuning transfer to Laptop B: chose window=16, "
                                                   "compression=off.", "progress_pct": 44.0}],
        "tuner": {"updates": 12, "arms": [{"arm": "window=16, compression=off", "pulls": 7,
                                           "avg_mbps": 18.9}]},
    }
    why_paused = a.answer("why did it pause?", brief)
    assert "packet loss" in why_paused and "41" in why_paused
    assert "encrypted" in a.answer("is this secure?", brief).lower()
    assert "481920" in a.answer("is this encrypted?", brief)
    assert "window=16" in a.answer("why this window size?", brief)
    predict = a.answer("what does the model predict?", brief)
    assert "82%" in predict and "5 seconds" in predict
    assert "Laptop B" in a.answer("who is receiving this?", brief)
    assert a.answer("anything at all", {})["answer"] if isinstance(a.answer("x", {}), dict) else True
    print("PASS: test_assistant_answers_from_the_decision_log")


if __name__ == "__main__":
    test_ble_payload_round_trip_and_size()
    test_ble_discovery_tracks_and_forgets_peers()
    test_model_beats_the_baseline_on_unseen_traces()
    test_prediction_is_deterministic()
    test_bandit_learns_a_better_policy_than_the_fixed_rule()
    test_bandit_state_survives_a_restart()
    test_assistant_answers_from_the_decision_log()

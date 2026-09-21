"""
Level 3 test — Decision Engine.
Proves the single most important claim in the project brief: given the
worked A/B/C example, the engine does NOT just pick the highest-bandwidth
peer (FR-7, AC-2). Also proves the hard exclusion gate (FR-8), that the
transfer profile actually shifts weighting rather than being cosmetic
(FR-10), and that explanations name excluded peers and reasons (FR-9).
Pure logic — no sockets, no I/O.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.measurement import PeerMetricsSample
from pathsense.decision import score_peers, explain, TransferProfile


def _sample(peer_id, latency_ms, loss_pct, throughput_mbps):
    return PeerMetricsSample(peer_id, latency_ms, 1.0, loss_pct, throughput_mbps, time.time())


def test_does_not_blindly_pick_highest_bandwidth():
    latest = {
        "A": _sample("A", 20, 2, 80),
        "B": _sample("B", 60, 0, 40),
        "C": _sample("C", 35, 5, 100),  # highest bandwidth
    }
    history = {"A": [], "B": [], "C": []}
    ranked = score_peers(latest, history, profile=TransferProfile.BALANCED)
    assert ranked, "expected non-empty ranking"
    winner = ranked[0]
    assert winner.peer_id != "C", "engine must not blindly pick the highest-bandwidth peer"
    assert winner.peer_id == "A", f"expected A to win the worked example, got {winner.peer_id}"
    print("PASS: test_does_not_blindly_pick_highest_bandwidth")


def test_high_loss_peer_is_excluded():
    latest = {
        "A": _sample("A", 20, 2, 80),
        "D": _sample("D", 10, 22, 90),  # 22% loss > 15% gate
    }
    history = {"A": [], "D": []}
    ranked = score_peers(latest, history, profile=TransferProfile.BALANCED)
    d = next(r for r in ranked if r.peer_id == "D")
    assert d.excluded is True
    assert d.exclude_reason is not None and "loss" in d.exclude_reason.lower()
    print("PASS: test_high_loss_peer_is_excluded")


def test_low_latency_profile_favors_lower_latency_peer_more_than_balanced():
    # X: very low latency, mid throughput.  Y: high latency, high throughput.
    latest = {
        "X": _sample("X", 5, 1, 30),
        "Y": _sample("Y", 80, 1, 100),
    }
    history = {"X": [], "Y": []}

    balanced = score_peers(latest, history, profile=TransferProfile.BALANCED)
    low_latency = score_peers(latest, history, profile=TransferProfile.LOW_LATENCY)

    balanced_winner = balanced[0].peer_id
    low_latency_winner = low_latency[0].peer_id

    assert balanced_winner == "Y", f"expected BALANCED to favor higher-throughput Y, got {balanced_winner}"
    assert low_latency_winner == "X", f"expected LOW_LATENCY to favor lower-latency X, got {low_latency_winner}"
    print("PASS: test_low_latency_profile_favors_lower_latency_peer_more_than_balanced")


def test_empty_input_returns_empty():
    assert score_peers({}, {}) == []
    print("PASS: test_empty_input_returns_empty")


def test_explanation_mentions_excluded_peer_reason():
    latest = {
        "A": _sample("A", 20, 2, 80),
        "D": _sample("D", 10, 22, 90),
    }
    history = {"A": [], "D": []}
    ranked = score_peers(latest, history, profile=TransferProfile.BALANCED)
    text = explain(ranked, {"A": "Peer A", "D": "Peer D"})
    assert "Peer D" in text
    assert "loss" in text.lower()
    print("PASS: test_explanation_mentions_excluded_peer_reason")


if __name__ == "__main__":
    test_does_not_blindly_pick_highest_bandwidth()
    test_high_loss_peer_is_excluded()
    test_low_latency_profile_favors_lower_latency_peer_more_than_balanced()
    test_empty_input_returns_empty()
    test_explanation_mentions_excluded_peer_reason()

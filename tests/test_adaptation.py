"""
Level 5 test — Adaptation Engine (hysteresis controller).
Proves: (1) pure hysteresis logic requires N CONSECUTIVE breaches, not one
noisy sample, before recommending a switch (FR-15, AC-5), and that a
required_consecutive_breaches=1 controller has no hysteresis at all; (2) a
full end-to-end scenario — real sockets, real transfer, real receivers for
two peers — actually reroutes a live in-progress transfer when synthetic
metrics show sustained degradation, and the file arrives correct on the new
peer (FR-14, FR-16).
"""
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.adaptation import HysteresisController, AdaptationLoop
from pathsense.decision import TransferProfile
from pathsense.discovery import PeerInfo
from pathsense.measurement import MetricsCollector, PeerMetricsSample
from pathsense.transfer import TransferReceiver, TransferSender

HOST = "127.0.0.1"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def test_hysteresis_requires_consecutive_breaches_not_one_spike():
    c = HysteresisController(switch_margin=1.3, required_consecutive_breaches=3)
    assert c.evaluate(current_score=0.5, best_alternative_score=0.9) is False  # breach #1
    assert c.evaluate(current_score=0.5, best_alternative_score=0.3) is False  # healthy sample -> resets
    assert c.evaluate(current_score=0.5, best_alternative_score=0.9) is False  # breach #1 again
    assert c.evaluate(current_score=0.5, best_alternative_score=0.9) is False  # breach #2
    assert c.evaluate(current_score=0.5, best_alternative_score=0.9) is True   # breach #3 -> switch
    print("PASS: test_hysteresis_requires_consecutive_breaches_not_one_spike")


def test_hysteresis_switches_immediately_when_current_peer_dead():
    c = HysteresisController(required_consecutive_breaches=1)
    assert c.evaluate(current_score=0.0, best_alternative_score=0.1) is True
    print("PASS: test_hysteresis_switches_immediately_when_current_peer_dead")


class _StubDiscovery:
    def __init__(self, peers):
        self._peers = peers

    def get_peers(self):
        return self._peers


def test_end_to_end_reroute_on_synthetic_degradation():
    tmp = tempfile.mkdtemp(prefix="ps_adapt_")
    try:
        recv_dir_b = os.path.join(tmp, "recv_b")
        recv_dir_c = os.path.join(tmp, "recv_c")
        chunk_size = 512
        total_chunks = 1500  # big enough that the transfer stays in-flight while we tick
        src_path = os.path.join(tmp, "video.bin")
        with open(src_path, "wb") as f:
            f.write(os.urandom(total_chunks * chunk_size))

        port_b, port_c = 53201, 53202
        receiver_b = TransferReceiver(port_b, recv_dir_b)
        receiver_c = TransferReceiver(port_c, recv_dir_c)
        receiver_b.start()
        receiver_c.start()
        time.sleep(0.3)

        try:
            peer_b = PeerInfo("B", "Peer B", HOST, 0, port_b, time.time())
            peer_c = PeerInfo("C", "Peer C", HOST, 0, port_c, time.time())
            discovery = _StubDiscovery([peer_b, peer_c])

            collector = MetricsCollector(discovery)
            now = time.time()
            # B starts good, then degrades badly. C starts mediocre, then becomes clearly better.
            collector._history["B"] = [
                PeerMetricsSample("B", 15.0, 2.0, 0.0, 55.0, now - 2),
                PeerMetricsSample("B", 400.0, 50.0, 10.0, 2.0, now),   # degraded
            ]
            collector._history["C"] = [
                PeerMetricsSample("C", 50.0, 5.0, 1.0, 20.0, now - 2),
                PeerMetricsSample("C", 20.0, 2.0, 0.0, 60.0, now),     # now clearly better
            ]

            sender = TransferSender(src_path, chunk_size=chunk_size)
            events = []
            controller = HysteresisController(switch_margin=1.3, required_consecutive_breaches=2)
            loop = AdaptationLoop(collector, discovery, sender, {"B": "Peer B", "C": "Peer C"},
                                   on_event=events.append, profile=TransferProfile.BALANCED,
                                   controller=controller)

            t = threading.Thread(target=sender.send_to, args=("B", HOST, port_b), daemon=True)
            t.start()

            deadline = time.time() + 3.0
            while time.time() < deadline and sender.state.status != "TRANSFERRING":
                time.sleep(0.01)
            assert sender.state.status == "TRANSFERRING", "transfer to B never reached TRANSFERRING state"

            loop._tick()  # breach #1 -> no switch yet
            loop._tick()  # breach #2 -> triggers reroute

            assert any("degraded" in e.lower() for e in events), f"expected a 'Route degraded' event, got {events}"
            assert any("switched to peer c" in e.lower() for e in events), f"expected a 'Switched to Peer C' event, got {events}"

            t.join(timeout=10.0)

            deadline = time.time() + 15.0
            while time.time() < deadline and not (sender.state.peer_id == "C" and sender.state.status == "COMPLETE"):
                time.sleep(0.05)
            assert sender.state.peer_id == "C", f"sender never rerouted to C (peer_id={sender.state.peer_id})"
            assert sender.state.status == "COMPLETE", f"rerouted transfer to C never completed (status={sender.state.status})"

            dest_c = os.path.join(recv_dir_c, "video.bin")
            assert os.path.getsize(dest_c) == os.path.getsize(src_path)
            assert _sha256_file(dest_c) == _sha256_file(src_path), "rerouted file on C is not checksum-correct"
            print("PASS: test_end_to_end_reroute_on_synthetic_degradation")
        finally:
            receiver_b.stop()
            receiver_c.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_hysteresis_requires_consecutive_breaches_not_one_spike()
    test_hysteresis_switches_immediately_when_current_peer_dead()
    test_end_to_end_reroute_on_synthetic_degradation()

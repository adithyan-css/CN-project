"""
Level 5 test — Adaptation Engine (Review 2: same-receiver pause/resume).
Proves: (1) LinkWatchdog needs N CONSECUTIVE unhealthy ticks before pausing
and M consecutive healthy ticks before resuming, so one noisy sample never
triggers either; (2) a full end-to-end scenario with real sockets: a live
transfer to receiver B is PAUSED when B's link degrades, keeps its progress,
RESUMES to the SAME receiver B when the link recovers, sends only the missing
chunks, and the file arrives checksum-correct. The file is never sent to a
different device.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.adaptation import LinkWatchdog, AdaptationLoop, PAUSE, RESUME
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


def test_watchdog_needs_consecutive_bad_ticks_to_pause():
    w = LinkWatchdog(pause_after=3, resume_after=2)
    assert w.observe(healthy=False, paused=False) is None
    assert w.observe(healthy=True, paused=False) is None    # one good sample resets the count
    assert w.observe(healthy=False, paused=False) is None
    assert w.observe(healthy=False, paused=False) is None
    assert w.observe(healthy=False, paused=False) == PAUSE
    print("PASS: test_watchdog_needs_consecutive_bad_ticks_to_pause")


def test_watchdog_needs_consecutive_good_ticks_to_resume():
    w = LinkWatchdog(pause_after=3, resume_after=2)
    assert w.observe(healthy=True, paused=True) is None
    assert w.observe(healthy=False, paused=True) is None     # flicker resets the count
    assert w.observe(healthy=True, paused=True) is None
    assert w.observe(healthy=True, paused=True) == RESUME
    print("PASS: test_watchdog_needs_consecutive_good_ticks_to_resume")


class _StubDiscovery:
    def __init__(self, peers):
        self._peers = peers

    def get_peers(self):
        return self._peers


def _set_link(collector, pid, healthy: bool):
    now = time.time()
    if healthy:
        s = PeerMetricsSample(pid, 5.0, 1.0, 0.0, 80.0, now)
    else:
        s = PeerMetricsSample(pid, 450.0, 120.0, 40.0, 0.2, now)
    collector._history[pid] = [s]


def test_end_to_end_pause_and_resume_on_same_receiver():
    tmp = tempfile.mkdtemp(prefix="ps_adapt_")
    try:
        recv_dir_b = os.path.join(tmp, "recv_b")
        recv_dir_c = os.path.join(tmp, "recv_c")
        chunk_size = 512
        total_chunks = 1500
        src_path = os.path.join(tmp, "video.bin")
        with open(src_path, "wb") as f:
            f.write(os.urandom(total_chunks * chunk_size))

        port_b, port_c = 53201, 53202
        receiver_b = TransferReceiver(port_b, recv_dir_b)
        receiver_c = TransferReceiver(port_c, recv_dir_c)   # a better device that must NOT get the file
        receiver_b.start()
        receiver_c.start()
        time.sleep(0.3)
        try:
            peer_b = PeerInfo("B", "Peer B", HOST, 0, port_b, time.time())
            peer_c = PeerInfo("C", "Peer C", HOST, 0, port_c, time.time())
            discovery = _StubDiscovery([peer_b, peer_c])
            collector = MetricsCollector(discovery)
            _set_link(collector, "B", healthy=True)
            _set_link(collector, "C", healthy=True)

            acked_log = []
            sender = TransferSender(src_path, chunk_size=chunk_size)

            def on_change(state):
                acked_log.append(len(state.acked_chunks))
                time.sleep(0.002)   # keep the transfer in flight long enough to tick

            sender.on_state_change = on_change
            events = []
            loop = AdaptationLoop(collector, discovery, sender, {"B": "Peer B", "C": "Peer C"},
                                  on_event=events.append, watchdog=LinkWatchdog(pause_after=2, resume_after=2))

            t = threading.Thread(target=sender.send_to, args=("B", HOST, port_b), daemon=True)
            t.start()
            deadline = time.time() + 3.0
            while time.time() < deadline and len(sender.state.acked_chunks) < 50:
                time.sleep(0.01)
            assert sender.state.status == "TRANSFERRING"

            # B degrades (C stays healthy — the agent must still not switch to C).
            _set_link(collector, "B", healthy=False)
            loop._tick()
            assert sender.state.status == "TRANSFERRING", "one bad tick must not pause"
            loop._tick()
            t.join(timeout=5.0)
            assert sender.state.status == "PAUSED", f"expected PAUSED, got {sender.state.status}"
            assert any("degraded" in e.lower() and "pausing" in e.lower() for e in events), events
            acked_at_pause = len(sender.state.acked_chunks)
            assert 0 < acked_at_pause < total_chunks

            # While still degraded, ticking must not resume.
            loop._tick(); loop._tick()
            assert sender.state.status == "PAUSED"

            # B recovers -> resume to B.
            _set_link(collector, "B", healthy=True)
            loop._tick()
            loop._tick()
            assert any("recovered" in e.lower() and "resuming" in e.lower() for e in events), events
            deadline = time.time() + 20.0
            while time.time() < deadline and sender.state.status != "COMPLETE":
                time.sleep(0.05)
            assert sender.state.status == "COMPLETE", f"resumed transfer never completed ({sender.state.status})"
            assert sender.state.peer_id == "B", "the file must stay with the chosen receiver"
            assert sender.state.pauses == 1 and sender.state.resumes == 1

            # Resume continued from where it stopped (progress never went backwards).
            assert all(b >= a for a, b in zip(acked_log, acked_log[1:])), "progress reset on resume"

            dest_b = os.path.join(recv_dir_b, "video.bin")
            assert _sha256_file(dest_b) == _sha256_file(src_path), "resumed file on B is not checksum-correct"
            assert not os.path.exists(os.path.join(recv_dir_c, "video.bin")), "file leaked to a different device"
            print("PASS: test_end_to_end_pause_and_resume_on_same_receiver")
        finally:
            receiver_b.stop()
            receiver_c.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_dropped_connection_resumes_automatically():
    """Receiver goes away mid-transfer (socket error -> DEGRADED); when it is
    reachable again the agent resumes on its own."""
    tmp = tempfile.mkdtemp(prefix="ps_drop_")
    try:
        recv_dir = os.path.join(tmp, "recv")
        src_path = os.path.join(tmp, "doc.bin")
        with open(src_path, "wb") as f:
            f.write(os.urandom(200 * 1024))
        port = 53205
        discovery = _StubDiscovery([PeerInfo("B", "Peer B", HOST, 0, port, time.time())])
        collector = MetricsCollector(discovery)
        _set_link(collector, "B", healthy=False)

        sender = TransferSender(src_path, chunk_size=4096)
        ok = sender.send_to("B", HOST, port)    # nobody listening yet -> connection refused
        assert ok is False and sender.state.status == "DEGRADED"

        events = []
        loop = AdaptationLoop(collector, discovery, sender, {"B": "Peer B"}, on_event=events.append,
                              watchdog=LinkWatchdog(pause_after=2, resume_after=2))
        loop._tick()
        assert any("dropped" in e.lower() for e in events), events

        receiver = TransferReceiver(port, recv_dir)
        receiver.start()
        time.sleep(0.3)
        try:
            _set_link(collector, "B", healthy=True)
            loop._tick(); loop._tick()
            deadline = time.time() + 10.0
            while time.time() < deadline and sender.state.status != "COMPLETE":
                time.sleep(0.05)
            assert sender.state.status == "COMPLETE"
            assert _sha256_file(os.path.join(recv_dir, "doc.bin")) == _sha256_file(src_path)
            print("PASS: test_dropped_connection_resumes_automatically")
        finally:
            receiver.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_watchdog_needs_consecutive_bad_ticks_to_pause()
    test_watchdog_needs_consecutive_good_ticks_to_resume()
    test_end_to_end_pause_and_resume_on_same_receiver()
    test_dropped_connection_resumes_automatically()

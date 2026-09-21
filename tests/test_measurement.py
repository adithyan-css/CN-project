"""
Level 2 test — Measurement Engine.
Proves: latency/jitter/loss probing produces real numbers against a live
server and 100% loss against a dead one; throughput measurement produces a
real positive Mbps figure against a live server and None against a dead
one (FR-4). All against real sockets on localhost — nothing mocked.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.measurement import MeasurementServer, probe_latency, measure_throughput

HOST = "127.0.0.1"


def test_latency_probe_against_live_server():
    port = 52001
    srv = MeasurementServer(port)
    srv.start()
    time.sleep(0.3)  # let the server threads bind
    try:
        latency_ms, jitter_ms, loss_pct = probe_latency(HOST, port, count=8)
        assert latency_ms is not None, "expected a real latency reading against a live server"
        assert 0 <= latency_ms < 200, f"loopback latency out of sane range: {latency_ms}"
        assert jitter_ms is not None and jitter_ms >= 0
        assert loss_pct < 50.0, f"unexpectedly high loss against a live local server: {loss_pct}"
        print("PASS: test_latency_probe_against_live_server")
    finally:
        srv.stop()


def test_latency_probe_against_dead_peer_is_100pct_loss():
    dead_port = 52099  # nothing listening here
    latency_ms, jitter_ms, loss_pct = probe_latency(HOST, dead_port, count=4, timeout=0.2)
    assert latency_ms is None
    assert jitter_ms is None
    assert loss_pct == 100.0
    print("PASS: test_latency_probe_against_dead_peer_is_100pct_loss")


def test_throughput_measurement_against_live_server():
    port = 52002
    srv = MeasurementServer(port)
    srv.start()
    time.sleep(0.3)
    try:
        mbps = measure_throughput(HOST, port, num_bytes=1_000_000)
        assert mbps is not None, "expected a real throughput measurement against a live server"
        assert mbps > 0, f"throughput should be a real positive number, got {mbps}"
        print("PASS: test_throughput_measurement_against_live_server")
    finally:
        srv.stop()


def test_throughput_against_dead_peer_returns_none():
    dead_port = 52098
    mbps = measure_throughput(HOST, dead_port, num_bytes=100_000, timeout=1.0)
    assert mbps is None
    print("PASS: test_throughput_against_dead_peer_returns_none")


if __name__ == "__main__":
    test_latency_probe_against_live_server()
    test_latency_probe_against_dead_peer_is_100pct_loss()
    test_throughput_measurement_against_live_server()
    test_throughput_against_dead_peer_returns_none()

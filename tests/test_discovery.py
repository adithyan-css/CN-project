"""
Level 1 test — Discovery Agent.
Proves: two independent nodes find each other via real UDP multicast with
zero manually-entered IP/port (FR-1..FR-3, AC-1), and a peer that stops
announcing is eventually forgotten (FR-2).
Uses real sockets on loopback — no mocking.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense import protocol
from pathsense.discovery import Discovery


def test_two_nodes_discover_each_other():
    a = Discovery("device-a-id", "Laptop A", 51001, 51002)
    b = Discovery("device-b-id", "Laptop B", 51003, 51004)
    a.start()
    b.start()
    try:
        deadline = time.time() + 6.0
        found_a_sees_b = False
        found_b_sees_a = False
        while time.time() < deadline and not (found_a_sees_b and found_b_sees_a):
            found_a_sees_b = any(p.device_id == "device-b-id" for p in a.get_peers())
            found_b_sees_a = any(p.device_id == "device-a-id" for p in b.get_peers())
            time.sleep(0.2)
        assert found_a_sees_b, "Node A never discovered Node B within 6s"
        assert found_b_sees_a, "Node B never discovered Node A within 6s"
        print("PASS: test_two_nodes_discover_each_other")
    finally:
        a.stop()
        b.stop()


def test_stale_peer_is_forgotten():
    original_stale = protocol.DISCOVERY_STALE_AFTER_SEC
    protocol.DISCOVERY_STALE_AFTER_SEC = 1.5
    try:
        a = Discovery("device-a2-id", "Laptop A2", 51005, 51006)
        b = Discovery("device-b2-id", "Laptop B2", 51007, 51008)
        a.start()
        b.start()
        try:
            deadline = time.time() + 6.0
            while time.time() < deadline and not any(p.device_id == "device-b2-id" for p in a.get_peers()):
                time.sleep(0.2)
            assert any(p.device_id == "device-b2-id" for p in a.get_peers()), "setup failed: A never saw B"

            b.stop()  # B stops announcing entirely

            deadline = time.time() + 6.0
            gone = False
            while time.time() < deadline:
                if not any(p.device_id == "device-b2-id" for p in a.get_peers()):
                    gone = True
                    break
                time.sleep(0.3)
            assert gone, "stale peer B was never removed from A's peer table"
            print("PASS: test_stale_peer_is_forgotten")
        finally:
            a.stop()
            b.stop()
    finally:
        protocol.DISCOVERY_STALE_AFTER_SEC = original_stale


if __name__ == "__main__":
    test_two_nodes_discover_each_other()
    test_stale_peer_is_forgotten()

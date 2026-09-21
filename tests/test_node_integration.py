"""
Level 6 test — full Node integration.
Proves the fully wired system — discovery, measurement, decision, transfer,
adaptation, and the HTTP dashboard API together — supports two real nodes
discovering each other and completing a real file transfer purely through
the same GET /api/state and POST /api/send calls the dashboard's own JS
uses (FR-17, FR-18, AC-6). Real sockets, real HTTP server, real file.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.node import Node

HOST = "127.0.0.1"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _get_state(dashboard_port: int) -> dict:
    with urllib.request.urlopen(f"http://{HOST}:{dashboard_port}/api/state", timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_send(dashboard_port: int, peer_id: str, file_path: str) -> dict:
    body = json.dumps({"peer_id": peer_id, "file_path": file_path}).encode("utf-8")
    req = urllib.request.Request(
        f"http://{HOST}:{dashboard_port}/api/send", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_two_nodes_discover_and_transfer_via_http_api():
    tmp = tempfile.mkdtemp(prefix="ps_node_")
    try:
        save_dir_a = os.path.join(tmp, "recv_a")
        save_dir_b = os.path.join(tmp, "recv_b")
        src_path = os.path.join(tmp, "shared.bin")
        with open(src_path, "wb") as f:
            f.write(os.urandom(300_000))

        node_a = Node("Node A", measurement_port=53301, transfer_port=53302,
                       dashboard_port=53380, save_dir=save_dir_a)
        node_b = Node("Node B", measurement_port=53303, transfer_port=53304,
                       dashboard_port=53381, save_dir=save_dir_b)
        node_a.start()
        node_b.start()
        try:
            # Poll node A's dashboard until it has discovered B AND has a
            # metrics reading for B (not just a bare discovery entry).
            deadline = time.time() + 15.0
            state = None
            b_id = None
            while time.time() < deadline:
                state = _get_state(53380)
                b_peer = next((p for p in state["peers"] if p["name"] == "Node B"), None)
                if b_peer is not None:
                    b_id = b_peer["device_id"]
                    if b_id in state.get("metrics", {}):
                        break
                time.sleep(0.3)
            assert b_id is not None, "Node A never discovered Node B via /api/state"
            assert b_id in state["metrics"], "Node A never got a metrics reading for Node B via /api/state"
            assert b_id in state["scores"], "Node A never computed a score for Node B"
            print("Discovery + measurement via HTTP API confirmed:", state["metrics"][b_id])

            result = _post_send(53380, b_id, src_path)
            assert "file_id" in result, f"send did not start: {result}"

            deadline = time.time() + 20.0
            transfer = None
            while time.time() < deadline:
                state = _get_state(53380)
                transfer = state.get("transfer")
                if transfer and transfer.get("status") == "COMPLETE":
                    break
                time.sleep(0.3)
            assert transfer is not None and transfer.get("status") == "COMPLETE", f"transfer never completed: {transfer}"

            dest = os.path.join(save_dir_b, "shared.bin")
            assert os.path.exists(dest), "receiving node never wrote the file"
            assert _sha256_file(dest) == _sha256_file(src_path), "transferred file checksum mismatch"
            print("PASS: test_two_nodes_discover_and_transfer_via_http_api")
        finally:
            node_a.discovery.stop()
            node_b.discovery.stop()
            node_a.measurement_server.stop()
            node_b.measurement_server.stop()
            node_a.collector.stop()
            node_b.collector.stop()
            node_a.receiver.stop()
            node_b.receiver.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_two_nodes_discover_and_transfer_via_http_api()

"""
Level 6 — Node process: wires every engine together into one runnable peer.
Run this on each of the 3 laptops:
    python3 -m pathsense.node --name "Laptop A" --dashboard-port 8801
"""
from __future__ import annotations

import argparse
import collections
import os
import threading
import time

from . import protocol
from .discovery import Discovery
from .measurement import MeasurementServer, MetricsCollector
from .decision import score_peers, assess_link, TransferProfile
from .transfer import TransferReceiver, TransferSender
from .adaptation import AdaptationLoop
from .dashboard import make_dashboard_server, start_dashboard_thread, DASHBOARD_HTML


class Node:
    def __init__(self, name: str, measurement_port: int, transfer_port: int,
                 dashboard_port: int, save_dir: str = "./received"):
        self.device_id = protocol.new_device_id()
        self.name = name
        self.events = collections.deque(maxlen=200)
        self.current_sender: TransferSender | None = None
        self.current_loop: AdaptationLoop | None = None

        self.discovery = Discovery(self.device_id, name, measurement_port, transfer_port)
        self.measurement_server = MeasurementServer(measurement_port)
        self.collector = MetricsCollector(self.discovery)
        self.receiver = TransferReceiver(transfer_port, save_dir)
        self.dashboard_port = dashboard_port
        # Root for the dashboard's file browser. Clamped so the (unauthenticated,
        # LAN-reachable) /api/browse endpoint can't be walked outside the user's
        # home directory — this is a demo convenience, not an access-controlled
        # file server (see TRD §12: no auth/trust model in the MVP).
        self._browse_root = os.path.realpath(os.path.expanduser("~"))

    def log(self, msg: str) -> None:
        self.events.append(f"{time.strftime('%H:%M:%S')} — {msg}")

    def start(self) -> None:
        self.discovery.start()
        self.measurement_server.start()
        self.collector.start()
        self.receiver.start()
        self.log(f"Node '{self.name}' online (id={self.device_id})")
        srv = make_dashboard_server(self.dashboard_port, self._state, self._handle_send,
                                     DASHBOARD_HTML, self._browse, self._handle_simulate)
        start_dashboard_thread(srv)
        self.log(f"Dashboard on http://0.0.0.0:{self.dashboard_port}")

    def _peer_names(self) -> dict:
        return {p.device_id: p.name for p in self.discovery.get_peers()}

    def _state(self) -> dict:
        latest = self.collector.all_latest()
        history = {pid: self.collector.history(pid) for pid in latest}
        ranked = score_peers(latest, history, profile=TransferProfile.BALANCED)
        return {
            "device_id": self.device_id,
            "name": self.name,
            "peers": [p.to_dict() for p in self.discovery.get_peers()],
            "metrics": {pid: m.to_dict() for pid, m in latest.items()},
            "scores": {r.peer_id: r.to_dict() for r in ranked},
            "transfer": self._transfer_state(),
            "events": list(self.events),
        }

    def _transfer_state(self) -> dict | None:
        if not self.current_sender:
            return None
        d = self.current_sender.state.to_dict()
        pid = self.current_sender.state.peer_id
        d["peer_name"] = self._peer_names().get(pid, pid)
        health = assess_link(self.collector.latest(pid), time.time()) if pid else None
        d["link_health"] = health.to_dict() if health else None
        d["simulation_remaining_sec"] = round(self.collector.simulation_remaining(pid), 1) if pid else 0.0
        return d

    def _handle_simulate(self, payload: dict) -> dict:
        """Demo aid: inject a labelled, time-limited link degradation for the
        current receiver so pause/resume can be shown on a healthy network."""
        if not self.current_sender or not self.current_sender.state.peer_id:
            return {"error": "start a transfer first"}
        seconds = float(payload.get("seconds", 8))
        seconds = max(1.0, min(seconds, 60.0))
        pid = self.current_sender.state.peer_id
        self.collector.simulate_degradation(pid, seconds)
        self.log(f"[DEMO] Simulating link degradation to {self._peer_names().get(pid, pid)} for {seconds:.0f}s")
        return {"status": "simulating", "seconds": seconds}

    def _handle_send(self, payload: dict) -> dict:
        peer_id = payload.get("peer_id")
        file_path = payload.get("file_path")
        peer = next((p for p in self.discovery.get_peers() if p.device_id == peer_id), None)
        if peer is None:
            return {"error": f"peer {peer_id} not currently discovered"}

        if self.current_sender and self.current_sender.state.status in ("TRANSFERRING", "PAUSED", "DEGRADED"):
            return {"error": "a transfer is already in progress"}
        if self.current_loop:
            self.current_loop.stop()
        names = self._peer_names()
        self.current_sender = TransferSender(file_path, on_state_change=lambda st: None)
        self.current_loop = AdaptationLoop(self.collector, self.discovery, self.current_sender,
                                            names, on_event=self.log)

        def run():
            latest = self.collector.all_latest()
            history = {pid: self.collector.history(pid) for pid in latest}
            ranked = score_peers(latest, history)
            if ranked:
                parts = [f"{names.get(r.peer_id, r.peer_id)} "
                         + (f"excluded ({r.exclude_reason})" if r.excluded else f"{r.score:.2f}")
                         for r in ranked]
                self.log("Link scores of nearby devices: " + "; ".join(parts))
            health = assess_link(self.collector.latest(peer.device_id), time.time())
            state = "healthy" if health.healthy else "degraded"
            self.log(f"Sending {os.path.basename(file_path)} to {peer.name}: link {state} ({health.reason}). "
                     f"The agent will pause and resume on this receiver if the link degrades.")
            self.current_loop.start()
            self.current_sender.send_to(peer.device_id, peer.ip, peer.transfer_port)

        threading.Thread(target=run, daemon=True).start()
        return {"status": "started", "file_id": self.current_sender.state.file_id}

    def _browse(self, requested_path: str | None) -> dict:
        target = os.path.realpath(requested_path) if requested_path else self._browse_root
        root = self._browse_root
        # Clamp: never resolve outside the browse root, even via a crafted
        # ?path= (e.g. "..\\..\\Windows") — walk up to root instead.
        target_cmp, root_cmp = os.path.normcase(target), os.path.normcase(root)
        if target_cmp != root_cmp and not target_cmp.startswith(root_cmp + os.sep):
            target = root
        if not os.path.isdir(target):
            target = root

        entries = []
        try:
            names = os.listdir(target)
        except OSError as e:
            return {"path": target, "parent": None, "entries": [], "error": str(e)}
        for name in names:
            full = os.path.join(target, name)
            try:
                is_dir = os.path.isdir(full)
            except OSError:
                continue
            entries.append({"name": name, "path": full, "is_dir": is_dir})
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))

        parent = None
        if os.path.normcase(target) != os.path.normcase(root):
            parent = os.path.dirname(target)
        return {"path": target, "parent": parent, "entries": entries}


def main():
    ap = argparse.ArgumentParser(description="Run one PathSense node")
    ap.add_argument("--name", required=True)
    ap.add_argument("--measurement-port", type=int, default=protocol.MEASUREMENT_PORT_BASE)
    ap.add_argument("--transfer-port", type=int, default=protocol.TRANSFER_PORT_BASE)
    ap.add_argument("--dashboard-port", type=int, default=protocol.DASHBOARD_PORT_BASE)
    ap.add_argument("--save-dir", default="./received")
    args = ap.parse_args()

    node = Node(args.name, args.measurement_port, args.transfer_port, args.dashboard_port, args.save_dir)
    node.start()
    print(f"PathSense node '{args.name}' running. Dashboard: http://localhost:{args.dashboard_port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

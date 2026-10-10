"""
Level 6 — Node process: wires every engine together into one runnable peer.

    python -m pathsense.node --name "Laptop A" --dashboard-port 8801

Useful switches:
    --discovery ble|multicast|auto   how devices find each other (default auto:
                                     try the Bluetooth radio, fall back to UDP
                                     multicast and say so)
    --no-encryption                  turn off pairing/encryption (comparison runs)
    --log-metrics data/links.csv     record every measurement for training
    --model path/to/link_model.json  use a different trained model
    --no-learning                    freeze the tuner (use the rule-based settings)
    --reset-learning                 forget what the tuner has learned
"""
from __future__ import annotations

import argparse
import collections
import os
import socket
import threading
import time

from . import protocol
from .adaptation import AdaptationLoop
from .assistant import Assistant
from .ble import BleDiscovery, SimulatedBleDiscovery
from .dashboard import make_dashboard_server, start_dashboard_thread, DASHBOARD_HTML
from .decision import score_peers, assess_link, TransferProfile
from .discovery import Discovery
from .linklog import LinkLogger
from .measurement import MeasurementServer, MetricsCollector
from .pairing import PairingAgent
from .predictor import DEFAULT_MODEL_PATH, Predictor
from .secure_channel import status as encryption_status
from .transfer import TransferReceiver, TransferSender
from .tuner import LinUCBTuner

STATE_DIR = os.path.join(os.path.expanduser("~"), ".netshare")


def local_ip() -> str:
    """Best guess at this machine's address on the current network."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))         # no packet is sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


class Node:
    def __init__(self, name: str, measurement_port: int, transfer_port: int,
                 dashboard_port: int, save_dir: str = "./received",
                 discovery_mode: str = "auto", encryption: bool = True,
                 model_path: str = DEFAULT_MODEL_PATH, learning: bool = True,
                 reset_learning: bool = False, metrics_log: str | None = None,
                 state_dir: str = STATE_DIR, auto_pair: bool = False):
        self.device_id = protocol.new_device_id()
        self.name = name
        self.events = collections.deque(maxlen=400)
        self.current_sender: TransferSender | None = None
        self.current_loop: AdaptationLoop | None = None
        self.dashboard_port = dashboard_port
        self.state_dir = state_dir

        # --- Review 3 pieces -------------------------------------------------
        self.pairing = PairingAgent(self.device_id, os.path.join(state_dir, "device.key")) if encryption else None
        self.predictor = Predictor(model_path)
        tuner_state = os.path.join(state_dir, "tuner.json")
        if reset_learning and os.path.exists(tuner_state):
            os.remove(tuner_state)
        self.tuner = LinUCBTuner(state_path=tuner_state) if learning else None
        self.logger = LinkLogger(metrics_log)
        self.assistant = Assistant()

        # --- discovery -------------------------------------------------------
        self.discovery_mode = discovery_mode
        self.multicast_discovery = Discovery(self.device_id, name, measurement_port, transfer_port)
        self.ble_discovery = None
        if discovery_mode in ("ble", "auto", "ble-sim"):
            cls = SimulatedBleDiscovery if discovery_mode == "ble-sim" else BleDiscovery
            self.ble_discovery = cls(self.device_id, name, measurement_port, transfer_port,
                                     public_key=self.pairing.public_key if self.pairing else b"",
                                     local_ip=local_ip, on_event=self.log)
        self.discovery = self.multicast_discovery          # swapped in start() if BLE works

        self.measurement_server = MeasurementServer(measurement_port)
        self.collector = MetricsCollector(self.discovery)
        self.receiver = TransferReceiver(transfer_port, save_dir, on_event=self.log,
                                         pairing_agent=self.pairing, device_name=name,
                                         auto_pair=auto_pair)
        # Root for the dashboard's file browser, clamped to the home directory:
        # /api/browse is unauthenticated and LAN-reachable, so it must never be
        # able to walk the whole disk (TRD §12).
        self._browse_root = os.path.realpath(os.path.expanduser("~"))

    # ---- logging -----------------------------------------------------------
    def log(self, msg: str) -> None:
        self.events.append(f"{time.strftime('%H:%M:%S')} — {msg}")

    # ---- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._start_discovery()
        self.measurement_server.start()
        self.collector.discovery = self.discovery
        self.collector.start()
        self.receiver.start()
        self.log(f"Node '{self.name}' online (id={self.device_id})")
        if self.pairing:
            self.log(f"Device key fingerprint {self.pairing.public_key.hex()[:12]} — "
                     f"transfers use {encryption_status(b'x')}")
        else:
            self.log("Encryption disabled by --no-encryption: transfers are plaintext")
        if self.predictor.available:
            meta = self.predictor.metadata
            self.log(f"Prediction model loaded ({meta.get('rows', '?')} training rows, "
                     f"test AUC {meta.get('test_auc', '?')}); forecasting 5 s ahead")
        else:
            self.log("No prediction model found — using rule-based adaptation only "
                     "(run tools/train_model.py to build one)")
        if self.tuner:
            self.log(f"Learning tuner active ({self.tuner.updates} updates remembered)")
        srv = make_dashboard_server(self.dashboard_port, self._state, self._handle_send,
                                    DASHBOARD_HTML, self._browse, self._handle_simulate,
                                    self._handle_pair, self._handle_ask, self._handle_cancel)
        start_dashboard_thread(srv)
        self.log(f"Dashboard on http://0.0.0.0:{self.dashboard_port}")

    def _start_discovery(self) -> None:
        if self.ble_discovery is not None:
            if self.ble_discovery.start():
                self.discovery = self.ble_discovery
                self.log(f"Discovery over Bluetooth LE ({self.ble_discovery.backend}) — "
                         f"no shared network needed")
                if self.discovery_mode == "ble":
                    return
                # In auto mode keep multicast running too: it finds devices that
                # are on this network but whose Bluetooth is off.
                self.multicast_discovery.start()
                self.discovery = _CombinedDiscovery(self.ble_discovery, self.multicast_discovery)
                return
            if self.discovery_mode == "ble":
                self.log("Bluetooth discovery was requested but is not usable here; "
                         "falling back to UDP multicast")
        self.multicast_discovery.start()
        self.discovery = self.multicast_discovery
        self.log("Discovery over UDP multicast (devices must share a network)")

    # ---- state for the dashboard ------------------------------------------
    def _peer_names(self) -> dict:
        return {p.device_id: p.name for p in self.discovery.get_peers()}

    def _state(self) -> dict:
        latest = self.collector.all_latest()
        history = {pid: self.collector.history(pid) for pid in latest}
        ranked = score_peers(latest, history, profile=TransferProfile.BALANCED)
        agent = self.current_loop.snapshot() if self.current_loop else {}
        return {
            "device_id": self.device_id,
            "name": self.name,
            "peers": [p.to_dict() for p in self.discovery.get_peers()],
            "metrics": {pid: m.to_dict() for pid, m in latest.items()},
            "scores": {r.peer_id: r.to_dict() for r in ranked},
            "transfer": self._transfer_state(),
            "incoming": self.receiver.incoming().to_dict() if self.receiver.incoming() else None,
            "pending_pair": self.receiver.pending_pair,
            "agent": agent,
            "capabilities": {
                "discovery": getattr(self.discovery, "backend", "multicast"),
                "encryption": encryption_status(b"x" if self.pairing else None),
                "model": self.predictor.metadata if self.predictor.available else None,
                "learning": bool(self.tuner),
                "assistant": self.assistant.backend,
                "metrics_log": self.logger.path,
            },
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

    # ---- dashboard actions -------------------------------------------------
    def _handle_simulate(self, payload: dict) -> dict:
        """Demo aid: inject a labelled, time-limited link degradation for the
        current receiver so pause/resume can be shown on a healthy network."""
        if not self.current_sender or not self.current_sender.state.peer_id:
            return {"error": "start a transfer first"}
        seconds = max(1.0, min(float(payload.get("seconds", 8)), 60.0))
        pid = self.current_sender.state.peer_id
        self.collector.simulate_degradation(pid, seconds)
        self.log(f"[DEMO] Simulating link degradation to {self._peer_names().get(pid, pid)} for {seconds:.0f}s")
        return {"status": "simulating", "seconds": seconds}

    def _handle_cancel(self, payload: dict) -> dict:
        """Stop the current transfer for good (the Cancel button)."""
        if not self.current_sender or self.current_sender.state.status in ("COMPLETE", "CANCELLED"):
            return {"error": "no transfer to cancel"}
        name = self._peer_names().get(self.current_sender.state.peer_id, "the receiver")
        progress = self.current_sender.state.progress_pct
        if self.current_loop:
            self.current_loop.stop()
        self.current_sender.abort()
        self.log(f"Transfer to {name} cancelled by the user at {progress:.0f}%.")
        return {"status": "cancelled"}

    def _handle_pair(self, payload: dict) -> dict:
        """The receiving user pressed 'the codes match' (or 'they don't')."""
        peer_id = payload.get("peer_id")
        accepted = bool(payload.get("accepted"))
        if not peer_id:
            return {"error": "peer_id required"}
        self.receiver.answer_pairing(peer_id, accepted)
        return {"status": "confirmed" if accepted else "rejected"}

    def _handle_ask(self, payload: dict) -> dict:
        brief = {"transfer": self._transfer_state(), "incoming": None,
                 "health": (self.current_loop.last_health.to_dict()
                            if self.current_loop and self.current_loop.last_health else None),
                 "forecast": (self.current_loop.last_forecast.to_dict()
                              if self.current_loop and self.current_loop.last_forecast else None),
                 "decisions": [d.to_dict() for d in (self.current_loop.decisions[-15:] if self.current_loop else [])],
                 "tuner": self.tuner.summary() if self.tuner else None,
                 "capabilities": {"model": bool(self.predictor.available)}}
        answer = self.assistant.answer(payload.get("question", ""), brief)
        self.log(f"Q: {payload.get('question', '')[:80]} → {answer['answer'][:160]}")
        return answer

    def _handle_send(self, payload: dict) -> dict:
        peer_id = payload.get("peer_id")
        file_path = payload.get("file_path")
        peer = next((p for p in self.discovery.get_peers() if p.device_id == peer_id), None)
        if peer is None:
            return {"error": f"peer {peer_id} not currently discovered"}
        if not peer.ip or peer.ip == "0.0.0.0":
            return {"error": f"{peer.name} was found over Bluetooth but is not on a reachable network yet; "
                             f"join the same Wi-Fi or hotspot"}
        if self.current_sender and self.current_sender.state.status in ("TRANSFERRING", "PAUSED",
                                                                       "DEGRADED", "PAIRING"):
            return {"error": "a transfer is already in progress — cancel it first"}
        if self.current_loop:
            self.current_loop.stop()
        names = self._peer_names()
        self.current_sender = TransferSender(file_path, on_state_change=lambda st: None,
                                             sender_name=self.name, pairing_agent=self.pairing,
                                             on_event=self.log)
        self.current_loop = AdaptationLoop(self.collector, self.discovery, self.current_sender, names,
                                           on_event=self.log, predictor=self.predictor,
                                           tuner=self.tuner, logger=self.logger)

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
            self.log(f"Sending {os.path.basename(file_path)} to {peer.name}: link "
                     f"{'healthy' if health.healthy else 'degraded'} ({health.reason}).")
            forecast = self.predictor.forecast(self.collector.history(peer.device_id))
            if forecast:
                self.log(forecast.sentence(peer.name))
            self.current_loop.start()
            try:
                self.current_sender.send_to(peer.device_id, peer.ip, peer.transfer_port)
            except ValueError as e:
                self.log(str(e))

        threading.Thread(target=run, daemon=True).start()
        return {"status": "started", "file_id": self.current_sender.state.file_id}

    # ---- file browser ------------------------------------------------------
    def _browse(self, requested_path: str | None) -> dict:
        target = os.path.realpath(requested_path) if requested_path else self._browse_root
        root = self._browse_root
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


class _CombinedDiscovery:
    """Bluetooth and multicast at once; a device seen by both appears once."""

    def __init__(self, primary, secondary):
        self.primary = primary
        self.secondary = secondary
        self.backend = f"{getattr(primary, 'backend', 'ble')}+multicast"

    def get_peers(self):
        seen: dict[str, object] = {}
        for peer in self.secondary.get_peers() + self.primary.get_peers():
            known = seen.get(peer.device_id)
            # prefer the entry that has a usable IP address
            if known is None or (peer.ip not in ("", "0.0.0.0")):
                seen[peer.device_id] = peer
        return list(seen.values())

    def stop(self):
        self.primary.stop()
        self.secondary.stop()


def main():
    ap = argparse.ArgumentParser(description="Run one NetShare node")
    ap.add_argument("--name", required=True)
    ap.add_argument("--measurement-port", type=int, default=protocol.MEASUREMENT_PORT_BASE)
    ap.add_argument("--transfer-port", type=int, default=protocol.TRANSFER_PORT_BASE)
    ap.add_argument("--dashboard-port", type=int, default=protocol.DASHBOARD_PORT_BASE)
    ap.add_argument("--save-dir", default="./received")
    ap.add_argument("--discovery", choices=["auto", "ble", "ble-sim", "multicast"], default="auto")
    ap.add_argument("--no-encryption", action="store_true")
    ap.add_argument("--auto-pair", action="store_true",
                    help="confirm pairing codes automatically (unattended runs; not for the demo)")
    ap.add_argument("--model", default=DEFAULT_MODEL_PATH)
    ap.add_argument("--no-learning", action="store_true")
    ap.add_argument("--reset-learning", action="store_true")
    ap.add_argument("--log-metrics", default=None, metavar="CSV")
    ap.add_argument("--state-dir", default=STATE_DIR)
    args = ap.parse_args()

    node = Node(args.name, args.measurement_port, args.transfer_port, args.dashboard_port,
                args.save_dir, discovery_mode=args.discovery, encryption=not args.no_encryption,
                model_path=args.model, learning=not args.no_learning,
                reset_learning=args.reset_learning, metrics_log=args.log_metrics,
                state_dir=args.state_dir, auto_pair=args.auto_pair)
    node.start()
    print(f"NetShare node '{args.name}' running. Dashboard: http://localhost:{args.dashboard_port}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""
Level 4 — Transfer Engine (Review 3).

What a transfer looks like on the wire now:

  1. HELLO / HELLO_ACK  — the two devices swap X25519 public keys.
  2. Both derive the same 6-digit code (pairing.py). If this pair has been
     confirmed before, the transfer continues straight away; otherwise both
     screens show the code and the receiving user confirms it matches. Nothing
     is sent until that happens.
  3. Every frame after that is AES-256-GCM encrypted with the paired session
     key (secure_channel.py). Without the `cryptography` package the channel
     falls back to plaintext and says so — it never pretends to be encrypted.
  4. OFFER / ACCEPT — file name, size, chunk size, chunk count.
  5. Chunks, pipelined: the sender keeps up to `window` chunks in flight
     instead of waiting for each ACK (Review 2 was stop-and-wait, which wastes
     a whole round-trip per chunk). Each chunk carries its index, a SHA-256
     checksum of the bytes as sent, and a flag saying whether it was
     zlib-compressed. The receiver verifies, writes at the right offset, and
     ACKs. `window` and `compression` can be changed by the agent mid-transfer
     (tuner.py decides); the change takes effect on the next chunk.
  6. COMPLETE.

A sender is bound to ONE receiver for life (Review 2 decision): when the link
degrades the transfer pauses and later resumes to the same device, sending only
the chunks that were never acknowledged.

On "forward error correction": the Review 3 plan listed FEC as a tuning knob.
It was dropped deliberately — this data path is TCP, which already retransmits
lost segments, so adding parity chunks would only add overhead. FEC belongs
with a UDP data path, which is listed as future work in TRD.md.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import socket
import threading
import time
import zlib
from typing import Callable, Optional

from . import protocol
from . import secure_channel
from .pairing import PairingAgent, PairingResult
from .transports import TcpTransport, TransportInfo

PAIR_CONFIRM_TIMEOUT_SEC = 90.0


class PairingRejected(ConnectionError):
    """The user did not confirm the 6-digit code (or confirmed it is wrong)."""

DEFAULT_WINDOW = 8
MIN_COMPRESSIBLE = 4096


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _send_json(channel, obj: dict) -> None:
    channel.send(json.dumps(obj).encode("utf-8"))


def _recv_json(channel) -> dict:
    return json.loads(channel.recv().decode("utf-8"))


# ---------------------------------------------------------------------------
# Sender-side state
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class TransferState:
    file_id: str
    file_path: str
    file_size: int
    chunk_size: int
    total_chunks: int
    acked_chunks: set
    peer_id: str
    status: str = "IDLE"   # IDLE|PAIRING|TRANSFERRING|PAUSED|DEGRADED|COMPLETE|FAILED|CANCELLED
    started_at: float = 0.0
    bytes_per_sec_ema: float = 0.0
    pauses: int = 0
    resumes: int = 0
    window: int = DEFAULT_WINDOW
    compression: bool = False
    bytes_on_wire: int = 0
    transport: str = "wifi"
    encrypted: bool = False
    pair_code: str = ""

    @property
    def progress_pct(self) -> float:
        if self.total_chunks == 0:
            return 100.0
        return 100.0 * len(self.acked_chunks) / self.total_chunks

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["acked_chunks"] = len(self.acked_chunks)
        d["progress_pct"] = round(self.progress_pct, 1)
        d["goodput_mbps"] = round(self.bytes_per_sec_ema * 8 / 1e6, 2)
        saved = self.chunk_size * len(self.acked_chunks) - self.bytes_on_wire
        d["compression_saved_pct"] = round(100.0 * saved / max(1, self.chunk_size * len(self.acked_chunks)), 1)
        return d


@dataclasses.dataclass
class IncomingTransfer:
    """Receiver-side view of one file being received (survives pause/resume)."""
    file_id: str
    name: str
    size: int
    total_chunks: int
    sender: str
    dest_path: str
    received: set = dataclasses.field(default_factory=set)
    status: str = "RECEIVING"   # RECEIVING | INTERRUPTED | COMPLETE
    sessions: int = 0
    encrypted: bool = False
    started_at: float = 0.0
    updated_at: float = 0.0

    @property
    def progress_pct(self) -> float:
        return 100.0 if self.total_chunks == 0 else 100.0 * len(self.received) / self.total_chunks

    def to_dict(self) -> dict:
        return {"file_id": self.file_id, "name": self.name, "size": self.size,
                "total_chunks": self.total_chunks, "sender": self.sender,
                "dest_path": self.dest_path, "received": len(self.received),
                "status": self.status, "sessions": self.sessions,
                "encrypted": self.encrypted,
                "progress_pct": round(self.progress_pct, 1)}


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------
class TransferReceiver:
    def __init__(self, port: int, save_dir: str, auto_accept: bool = True,
                 on_event: Optional[Callable[[str], None]] = None,
                 pairing_agent: Optional[PairingAgent] = None,
                 device_name: str = "", transport=None,
                 on_pair_request: Optional[Callable[[str, str, str], None]] = None,
                 auto_pair: bool = False):
        self.port = port
        self.save_dir = save_dir
        self.auto_accept = auto_accept
        self.on_event = on_event or (lambda msg: None)
        self.pairing = pairing_agent
        self.device_name = device_name
        self.transport = transport or TcpTransport()
        self.on_pair_request = on_pair_request
        # auto_pair confirms the code without asking a human. It exists for
        # tests and unattended runs; the demo leaves it off, because confirming
        # the code is exactly what stops a stranger pairing with your laptop.
        self.auto_pair = auto_pair
        os.makedirs(save_dir, exist_ok=True)
        self._stop = threading.Event()
        self._incoming: dict[str, IncomingTransfer] = {}
        self._inc_lock = threading.Lock()
        self.latest_file_id: Optional[str] = None
        self.pending_pair: Optional[dict] = None     # shown on the dashboard
        self._pair_answer: dict[str, bool] = {}

    # ---- pairing surface for the dashboard ------------------------------
    def answer_pairing(self, peer_id: str, accepted: bool) -> None:
        self._pair_answer[peer_id] = accepted

    def incoming(self) -> Optional[IncomingTransfer]:
        with self._inc_lock:
            return self._incoming.get(self.latest_file_id) if self.latest_file_id else None

    # ---- server loop -----------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _accept_loop(self) -> None:
        srv = self.transport.listen(self.port)
        srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except (TimeoutError, OSError) as e:
                if isinstance(e, OSError) and not isinstance(e, TimeoutError):
                    if self._stop.is_set():
                        break
                continue
            try:
                conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            threading.Thread(target=self._handle_session, args=(conn, addr), daemon=True).start()
        try:
            srv.close()
        except OSError:
            pass

    # ---- one session -----------------------------------------------------
    def _handshake(self, channel) -> tuple[Optional[PairingResult], str, str]:
        """Swap keys, agree the 6-digit code, and wait for the user if needed."""
        hello = _recv_json(channel)
        if hello.get("type") != "HELLO":
            raise ConnectionError("expected HELLO")
        peer_id, peer_name = hello["device_id"], hello.get("name", "unknown")
        if self.pairing is None or not hello.get("public_key"):
            # One side has encryption switched off: no pairing, no session key.
            _send_json(channel, {"type": "HELLO_ACK", "device_id": "", "name": self.device_name,
                                 "public_key": "", "paired": False})
            return None, peer_id, peer_name
        _send_json(channel, {"type": "HELLO_ACK", "device_id": self.pairing.device_id,
                             "name": self.device_name,
                             "public_key": self.pairing.public_key.hex(), "paired": True})
        result = self.pairing.start(peer_id, bytes.fromhex(hello["public_key"]))
        if result.confirmed:
            _send_json(channel, {"type": "PAIR_RESULT", "ok": True, "already_trusted": True})
            self.on_event(f"{peer_name} is already paired with this device (code {result.code}); no confirmation needed")
            return result, peer_id, peer_name

        if self.auto_pair:
            self.pairing.confirm(peer_id, True)
            _send_json(channel, {"type": "PAIR_RESULT", "ok": True})
            self.on_event(f"Paired with {peer_name} (code {result.code}, auto-confirmed)")
            return result, peer_id, peer_name

        self.pending_pair = {"peer_id": peer_id, "peer_name": peer_name, "code": result.code,
                             "asked_at": time.time()}
        self._pair_answer.pop(peer_id, None)
        self.on_event(f"Pairing request from {peer_name}. Confirm the code {result.code} matches "
                      f"the one on their screen.")
        deadline = time.time() + PAIR_CONFIRM_TIMEOUT_SEC
        while time.time() < deadline and not self._stop.is_set():
            if peer_id in self._pair_answer:
                break
            time.sleep(0.1)
        accepted = self._pair_answer.pop(peer_id, False)
        self.pending_pair = None
        self.pairing.confirm(peer_id, accepted)
        _send_json(channel, {"type": "PAIR_RESULT", "ok": accepted})
        if not accepted:
            self.on_event(f"Pairing with {peer_name} was not confirmed; transfer refused")
            raise PairingRejected("pairing rejected")
        self.on_event(f"Paired with {peer_name} (code {result.code}). The transfer is encrypted.")
        return result, peer_id, peer_name

    def _handle_session(self, conn, addr=None) -> None:
        inc: Optional[IncomingTransfer] = None
        channel = secure_channel.PlainChannel(conn)
        try:
            result, peer_id, peer_name = self._handshake(channel)
            session_key = result.session_key if (result and result.confirmed) else None
            if session_key:
                channel = secure_channel.SecureChannel(conn, session_key, is_initiator=False)

            offer = _recv_json(channel)
            if offer.get("type") != "OFFER":
                return
            if not self.auto_accept:
                _send_json(channel, {"type": "REJECT", "file_id": offer["file_id"], "reason": "declined"})
                return
            _send_json(channel, {"type": "ACCEPT", "file_id": offer["file_id"]})

            dest_path = os.path.join(self.save_dir, os.path.basename(offer["name"]))
            sender = offer.get("sender_name") or peer_name
            # Only truncate/preallocate for a FRESH transfer; a RESUME must not.
            if not (os.path.exists(dest_path) and os.path.getsize(dest_path) == offer["size"]):
                with open(dest_path, "wb") as f:
                    f.truncate(offer["size"])

            now = time.time()
            with self._inc_lock:
                inc = self._incoming.get(offer["file_id"])
                resumed = inc is not None
                if inc is None:
                    inc = IncomingTransfer(offer["file_id"], offer["name"], offer["size"],
                                           offer["total_chunks"], sender, dest_path, started_at=now)
                    self._incoming[offer["file_id"]] = inc
                inc.status = "RECEIVING"
                inc.sessions += 1
                inc.encrypted = bool(session_key)
                inc.updated_at = now
                self.latest_file_id = offer["file_id"]
            if resumed:
                self.on_event(f"{sender} resumed sending {offer['name']} from {inc.progress_pct:.0f}% "
                              f"({len(inc.received)}/{inc.total_chunks} chunks already here)")
            else:
                self.on_event(f"Receiving {offer['name']} ({offer['size'] / 1048576:.1f} MB) from {sender} "
                              f"— {'encrypted' if session_key else 'NOT encrypted'}")

            fh = open(dest_path, "r+b")
            try:
                while True:
                    header = _recv_json(channel)
                    if header["type"] == "COMPLETE":
                        if len(inc.received) == inc.total_chunks:
                            inc.status = "COMPLETE"
                            self.on_event(f"Received {offer['name']} from {sender}: all {inc.total_chunks} "
                                          f"chunks verified (SHA-256). Saved to {dest_path}")
                        break
                    if header["type"] != "CHUNK":
                        continue
                    raw = channel.recv()
                    ok = _sha256(raw) == header["checksum"]
                    if ok and header.get("compressed"):
                        try:
                            raw = zlib.decompress(raw)
                        except zlib.error:
                            ok = False
                    if ok:
                        fh.seek(header["chunk_index"] * offer["chunk_size"])
                        fh.write(raw)
                        inc.received.add(header["chunk_index"])
                        inc.updated_at = time.time()
                    _send_json(channel, {"type": "CHUNK_ACK", "chunk_index": header["chunk_index"], "ok": ok})
            finally:
                fh.close()
        except (ConnectionError, OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
        finally:
            channel.close()
            if inc is not None and inc.status == "RECEIVING":
                inc.status = "INTERRUPTED"
                self.on_event(f"Connection from {inc.sender} interrupted at {inc.progress_pct:.0f}% "
                              f"({len(inc.received)}/{inc.total_chunks} chunks kept); waiting for resume")


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------
class TransferSender:
    def __init__(self, file_path: str, chunk_size: int = protocol.CHUNK_SIZE_DEFAULT,
                 on_state_change: Optional[Callable[[TransferState], None]] = None,
                 sender_name: str = "", pairing_agent: Optional[PairingAgent] = None,
                 transport=None, on_event: Optional[Callable[[str], None]] = None):
        self.file_path = file_path
        self.sender_name = sender_name
        self.chunk_size = chunk_size
        self.on_state_change = on_state_change
        self.pairing = pairing_agent
        self.transport = transport or TcpTransport()
        self.on_event = on_event or (lambda msg: None)
        size = os.path.getsize(file_path)
        total_chunks = (size + chunk_size - 1) // chunk_size
        self.state = TransferState(
            file_id=os.path.basename(file_path) + f"-{int(time.time())}",
            file_path=file_path, file_size=size, chunk_size=chunk_size,
            total_chunks=total_chunks, acked_chunks=set(), peer_id="",
        )
        self._cancel_current = threading.Event()
        self._lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._window_sem_lock = threading.Condition()
        self._in_flight = 0

    # ---- knobs the agent can change mid-transfer --------------------------
    def set_window(self, window: int) -> None:
        window = max(1, min(int(window), 128))
        with self._window_sem_lock:
            self.state.window = window
            self._window_sem_lock.notify_all()

    def set_compression(self, enabled: bool) -> None:
        self.state.compression = bool(enabled)

    def _emit(self):
        if self.on_state_change:
            self.on_state_change(self.state)

    @property
    def next_chunk(self) -> int:
        for idx in range(self.state.total_chunks):
            if idx not in self.state.acked_chunks:
                return idx
        return self.state.total_chunks

    # ---- the session -----------------------------------------------------
    def send_to(self, peer_id: str, ip: str, port: int, transport=None) -> bool:
        if self.aborted:
            return False
        with self._session_lock:
            return self._send_to_locked(peer_id, ip, port, transport or self.transport)

    def _handshake(self, channel, peer_id: str) -> Optional[PairingResult]:
        # HELLO is always sent, with or without a key, so both ends agree on the
        # shape of the conversation even when one has encryption switched off.
        _send_json(channel, {"type": "HELLO",
                             "device_id": self.pairing.device_id if self.pairing else "",
                             "name": self.sender_name,
                             "public_key": self.pairing.public_key.hex() if self.pairing else ""})
        ack = _recv_json(channel)
        if self.pairing is None:
            return None
        if ack.get("type") != "HELLO_ACK" or not ack.get("paired"):
            return None
        result = self.pairing.start(peer_id, bytes.fromhex(ack["public_key"]))
        self.state.pair_code = result.code
        if not result.confirmed:
            self.state.status = "PAIRING"
            self._emit()
            self.on_event(f"Pairing code {result.code} — check it matches on the other device, "
                          f"then confirm there.")
        verdict = _recv_json(channel)
        if verdict.get("type") != "PAIR_RESULT" or not verdict.get("ok"):
            self.pairing.confirm(peer_id, False)
            raise PairingRejected("the other device did not confirm the pairing code")
        self.pairing.confirm(peer_id, True)
        if not verdict.get("already_trusted"):
            self.on_event(f"Pairing confirmed (code {result.code}); the transfer is encrypted.")
        return self.pairing.sessions[peer_id]

    def _send_to_locked(self, peer_id: str, ip: str, port: int, transport) -> bool:
        with self._lock:
            if self.state.peer_id and self.state.peer_id != peer_id:
                raise ValueError(
                    f"transfer is bound to receiver {self.state.peer_id!r}; "
                    f"refusing to send it to a different device ({peer_id!r})")
            if self.state.status in ("PAUSED", "DEGRADED"):
                self.state.resumes += 1
            self.state.peer_id = peer_id
            if self.state.started_at == 0.0:
                self.state.started_at = time.time()
        self._cancel_current.clear()

        info: TransportInfo = transport.info() if hasattr(transport, "info") else None
        self.state.transport = getattr(transport, "kind", "wifi")
        channel = None
        try:
            sock = transport.connect(ip, port)
            channel = secure_channel.PlainChannel(sock)
            result = self._handshake(channel, peer_id)
            session_key = result.session_key if (result and result.confirmed) else None
            if session_key:
                channel = secure_channel.SecureChannel(sock, session_key, is_initiator=True)
            self.state.encrypted = bool(session_key) and channel.encrypted
            self.state.status = "TRANSFERRING"
            self._emit()

            _send_json(channel, {
                "type": "OFFER", "file_id": self.state.file_id,
                "name": os.path.basename(self.file_path), "size": self.state.file_size,
                "chunk_size": self.chunk_size, "total_chunks": self.state.total_chunks,
                "checksum_algo": "sha256", "sender_name": self.sender_name,
            })
            reply = _recv_json(channel)
            if reply.get("type") != "ACCEPT":
                self.state.status = "FAILED"
                self._emit()
                return False
            return self._pump(channel)
        except PairingRejected as e:
            # Not a link problem: retrying would just ask the user again forever.
            self.state.status = "FAILED"
            self._emit()
            self.on_event(f"Transfer cancelled: {e}")
            return False
        except (OSError, ConnectionError, ValueError, KeyError, json.JSONDecodeError) as e:
            if isinstance(e, ValueError) and "bound to receiver" in str(e):
                raise
            self.state.status = "CANCELLED" if self.aborted else "DEGRADED"
            self._emit()
            return False
        finally:
            if channel is not None:
                channel.close()

    def _pump(self, channel) -> bool:
        """Send the missing chunks with up to `window` in flight at once."""
        stop_reader = threading.Event()
        failed = []

        def reader():
            while not stop_reader.is_set():
                try:
                    ack = _recv_json(channel)
                except Exception:
                    failed.append(True)
                    with self._window_sem_lock:
                        self._in_flight = 0
                        self._window_sem_lock.notify_all()
                    return
                if ack.get("type") == "CHUNK_ACK":
                    if ack.get("ok"):
                        with self._lock:
                            self.state.acked_chunks.add(ack["chunk_index"])
                        self._update_rate()
                        self._emit()
                    with self._window_sem_lock:
                        self._in_flight = max(0, self._in_flight - 1)
                        self._window_sem_lock.notify_all()

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        sent_any = False
        try:
            with open(self.file_path, "rb") as f:
                pending = [i for i in range(self.state.total_chunks) if i not in self.state.acked_chunks]
                for idx in pending:
                    if self._cancel_current.is_set() or failed:
                        break
                    with self._window_sem_lock:
                        while self._in_flight >= self.state.window and not failed and not self._cancel_current.is_set():
                            self._window_sem_lock.wait(0.2)
                        self._in_flight += 1
                    f.seek(idx * self.chunk_size)
                    raw = f.read(self.chunk_size)
                    payload, compressed = raw, False
                    if self.state.compression and len(raw) >= MIN_COMPRESSIBLE:
                        squeezed = zlib.compress(raw, 1)
                        if len(squeezed) < len(raw) * 0.95:
                            payload, compressed = squeezed, True
                    _send_json(channel, {"type": "CHUNK", "chunk_index": idx, "length": len(payload),
                                         "checksum": _sha256(payload), "compressed": compressed})
                    channel.send(payload)
                    self.state.bytes_on_wire += len(payload)
                    sent_any = True
            # wait for the last ACKs
            deadline = time.time() + 30
            with self._window_sem_lock:
                while self._in_flight > 0 and not failed and time.time() < deadline \
                        and not self._cancel_current.is_set():
                    self._window_sem_lock.wait(0.2)

            if self._cancel_current.is_set():
                self.state.status = "CANCELLED" if self.aborted else "PAUSED"
                self._emit()
                return False
            if failed:
                self.state.status = "DEGRADED"
                self._emit()
                return False
            _send_json(channel, {"type": "COMPLETE"})
            done = len(self.state.acked_chunks) == self.state.total_chunks
            self.state.status = "COMPLETE" if done else "DEGRADED"
            self._emit()
            return done
        except (OSError, ConnectionError) as e:
            if self.aborted:
                self.state.status = "CANCELLED"
            else:
                self.state.status = "PAUSED" if self._cancel_current.is_set() else "DEGRADED"
            self._emit()
            return False
        finally:
            stop_reader.set()
            with self._window_sem_lock:
                self._in_flight = 0
            if sent_any:
                pass

    def _update_rate(self) -> None:
        now = time.time()
        last = getattr(self, "_last_ack_time", None)
        self._last_ack_time = now
        if last is None:
            return
        gap = max(1e-6, now - last)
        inst = self.chunk_size / gap
        ema = self.state.bytes_per_sec_ema
        self.state.bytes_per_sec_ema = inst if ema == 0 else 0.8 * ema + 0.2 * inst

    def cancel_current(self) -> None:
        self._cancel_current.set()
        with self._window_sem_lock:
            self._window_sem_lock.notify_all()

    def pause(self) -> None:
        """Stop after the chunks in flight; progress (acked_chunks) is kept."""
        with self._lock:
            self.state.pauses += 1
        self.cancel_current()

    def abort(self) -> None:
        """Give up on this transfer for good (the user pressed Cancel).

        Unlike pause(), this is terminal: the adaptation loop stops retrying
        and the sender refuses to send anything more, so a transfer that can
        never connect does not retry forever."""
        self._aborted = True
        self.state.status = "CANCELLED"
        self.cancel_current()
        self._emit()

    @property
    def aborted(self) -> bool:
        return getattr(self, "_aborted", False)

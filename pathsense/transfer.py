"""
Level 4 — Transfer Engine.
Chunked, checksummed, stop-and-wait TCP file transfer.

Review 2 change: a TransferSender is bound to ONE receiver for its whole
life. Adaptation no longer "reroutes" a file to a different device (that
would deliver the file to the wrong person). Instead, when the link to the
chosen receiver degrades, the transfer is PAUSED and later RESUMED to the
same receiver: acked_chunks is preserved and only the missing chunks are
sent (checkpoint resume).
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import socket
import threading
import time
from typing import Callable, Optional

from . import protocol


@dataclasses.dataclass
class TransferState:
    file_id: str
    file_path: str
    file_size: int
    chunk_size: int
    total_chunks: int
    acked_chunks: set
    peer_id: str
    status: str = "IDLE"   # IDLE | TRANSFERRING | PAUSED | DEGRADED | COMPLETE | FAILED
    started_at: float = 0.0
    pauses: int = 0
    resumes: int = 0
    bytes_per_sec_ema: float = 0.0

    @property
    def progress_pct(self) -> float:
        if self.total_chunks == 0:
            return 100.0
        return 100.0 * len(self.acked_chunks) / self.total_chunks

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["acked_chunks"] = len(self.acked_chunks)
        d["progress_pct"] = round(self.progress_pct, 1)
        return d


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TransferReceiver:
    def __init__(self, port: int, save_dir: str, auto_accept: bool = True):
        self.port = port
        self.save_dir = save_dir
        self.auto_accept = auto_accept
        os.makedirs(save_dir, exist_ok=True)
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _accept_loop(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(4)
        srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # Stop-and-wait (header, payload, then wait for ACK) + Nagle +
            # delayed ACKs stalls ~40 ms per chunk; disable Nagle.
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=self._handle_session, args=(conn,), daemon=True).start()

    def _handle_session(self, conn: socket.socket) -> None:
        try:
            offer = protocol.recv_json(conn)
            if offer.get("type") != "OFFER":
                return
            if not self.auto_accept:
                protocol.send_json(conn, {"type": "REJECT", "file_id": offer["file_id"], "reason": "declined"})
                return
            protocol.send_json(conn, {"type": "ACCEPT", "file_id": offer["file_id"]})

            dest_path = os.path.join(self.save_dir, offer["name"])
            # Only truncate/preallocate for a FRESH transfer (different
            # file, or first time we've seen this name+size). A RESUME
            # (same name+size already on disk) must NOT truncate.
            needs_fresh_alloc = True
            if os.path.exists(dest_path) and os.path.getsize(dest_path) == offer["size"]:
                needs_fresh_alloc = False
            if needs_fresh_alloc:
                with open(dest_path, "wb") as f:
                    f.truncate(offer["size"])

            while True:
                header = protocol.recv_json(conn)
                if header["type"] == "COMPLETE":
                    break
                if header["type"] != "CHUNK":
                    continue
                raw = protocol.recv_exact(conn, header["length"])
                ok = _sha256(raw) == header["checksum"]
                if ok:
                    with open(dest_path, "r+b") as f:
                        f.seek(header["chunk_index"] * offer["chunk_size"])
                        f.write(raw)
                protocol.send_json(conn, {"type": "CHUNK_ACK", "chunk_index": header["chunk_index"], "ok": ok})
        except (ConnectionError, OSError, KeyError):
            pass
        finally:
            conn.close()


class TransferSender:
    def __init__(self, file_path: str, chunk_size: int = protocol.CHUNK_SIZE_DEFAULT,
                 on_state_change: Optional[Callable[[TransferState], None]] = None):
        self.file_path = file_path
        self.chunk_size = chunk_size
        self.on_state_change = on_state_change
        size = os.path.getsize(file_path)
        total_chunks = (size + chunk_size - 1) // chunk_size
        self.state = TransferState(
            file_id=os.path.basename(file_path) + f"-{int(time.time())}",
            file_path=file_path, file_size=size, chunk_size=chunk_size,
            total_chunks=total_chunks, acked_chunks=set(), peer_id="",
        )
        self._cancel_current = threading.Event()
        self._lock = threading.Lock()
        # Serializes whole send_to() calls so a resume never overlaps a
        # session that is still winding down after a pause.
        self._session_lock = threading.Lock()

    def _emit(self):
        if self.on_state_change:
            self.on_state_change(self.state)

    def send_to(self, peer_id: str, ip: str, port: int) -> bool:
        with self._session_lock:
            return self._send_to_locked(peer_id, ip, port)

    def _send_to_locked(self, peer_id: str, ip: str, port: int) -> bool:
        with self._lock:
            if self.state.peer_id and self.state.peer_id != peer_id:
                raise ValueError(
                    f"transfer is bound to receiver {self.state.peer_id!r}; "
                    f"refusing to send it to a different device ({peer_id!r})")
            if self.state.status in ("PAUSED", "DEGRADED"):
                self.state.resumes += 1
            self.state.peer_id = peer_id
            self.state.status = "TRANSFERRING"
            if self.state.started_at == 0.0:
                self.state.started_at = time.time()
        self._cancel_current.clear()
        self._emit()

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(5.0)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.connect((ip, port))
            protocol.send_json(s, {
                "type": "OFFER", "file_id": self.state.file_id,
                "name": os.path.basename(self.file_path), "size": self.state.file_size,
                "chunk_size": self.chunk_size, "total_chunks": self.state.total_chunks,
                "checksum_algo": "sha256",
            })
            reply = protocol.recv_json(s)
            if reply.get("type") != "ACCEPT":
                self.state.status = "FAILED"
                self._emit()
                return False

            with open(self.file_path, "rb") as f:
                for idx in range(self.state.total_chunks):
                    if idx in self.state.acked_chunks:
                        continue
                    if self._cancel_current.is_set():
                        self.state.status = "PAUSED"
                        self._emit()
                        return False
                    f.seek(idx * self.chunk_size)
                    raw = f.read(self.chunk_size)
                    header = {"type": "CHUNK", "chunk_index": idx, "length": len(raw), "checksum": _sha256(raw)}
                    protocol.send_json(s, header)
                    s.sendall(raw)
                    ack = protocol.recv_json(s)
                    if ack.get("type") == "CHUNK_ACK" and ack.get("ok"):
                        with self._lock:
                            self.state.acked_chunks.add(idx)
                        self._emit()

            protocol.send_json(s, {"type": "COMPLETE"})
            done = len(self.state.acked_chunks) == self.state.total_chunks
            # A chunk that failed its checksum is not acked; leave the transfer
            # resumable instead of claiming it completed.
            self.state.status = "COMPLETE" if done else "DEGRADED"
            self._emit()
            return done
        except (OSError, ConnectionError):
            self.state.status = "DEGRADED"
            self._emit()
            return False
        finally:
            s.close()

    def cancel_current(self) -> None:
        self._cancel_current.set()

    def pause(self) -> None:
        """Stop after the current chunk; progress (acked_chunks) is kept."""
        with self._lock:
            self.state.pauses += 1
        self._cancel_current.set()

    @property
    def next_chunk(self) -> int:
        for idx in range(self.state.total_chunks):
            if idx not in self.state.acked_chunks:
                return idx
        return self.state.total_chunks

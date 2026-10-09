"""
Level 2b — Encrypted channel (Review 3).

Review 2 sent files as plaintext TCP. Every byte now travels inside an
authenticated encrypted frame keyed by the pairing session key, so a laptop
sniffing the hotspot sees only ciphertext and cannot alter a chunk without
being detected.

Design:
  * AES-256-GCM (via `cryptography`) — authenticated encryption: it both hides
    the data and proves it was not modified.
  * Each direction has its own key, derived from the session key with HKDF, so
    the two directions can never reuse a nonce.
  * The nonce is a 4-byte random prefix (fixed per connection) plus a 8-byte
    counter, which must never repeat for a key. The counter is refused past its
    limit rather than wrapping.
  * Frames are length-prefixed: 4-byte big-endian length, then the ciphertext.
  * If `cryptography` is not installed, PlainChannel is used instead and the
    node says loudly that the transfer is unencrypted; nothing silently
    pretends to be secure.
"""
from __future__ import annotations

import os
import socket
import struct
from typing import Optional

from .pairing import hkdf

try:                                                  # pragma: no cover - env dependent
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    ENCRYPTION_AVAILABLE = True
except Exception:                                     # pragma: no cover
    ENCRYPTION_AVAILABLE = False

MAX_FRAME = 8 * 1024 * 1024
_MAX_COUNTER = 2 ** 63


class ChannelError(ConnectionError):
    pass


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ChannelError("connection closed")
        buf += part
    return buf


class PlainChannel:
    """Length-prefixed framing with no encryption (fallback / tests)."""

    encrypted = False

    def __init__(self, sock: socket.socket):
        self.sock = sock

    def send(self, data: bytes) -> None:
        self.sock.sendall(struct.pack(">I", len(data)) + data)

    def recv(self) -> bytes:
        (length,) = struct.unpack(">I", _recv_exact(self.sock, 4))
        if length > MAX_FRAME:
            raise ChannelError(f"frame too large: {length}")
        return _recv_exact(self.sock, length)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class SecureChannel(PlainChannel):
    """AES-256-GCM frames over a TCP socket."""

    encrypted = True

    def __init__(self, sock: socket.socket, session_key: bytes, is_initiator: bool):
        super().__init__(sock)
        if not ENCRYPTION_AVAILABLE:
            raise ChannelError("encryption requires the 'cryptography' package")
        send_label = b"c2s" if is_initiator else b"s2c"
        recv_label = b"s2c" if is_initiator else b"c2s"
        self._send_aes = AESGCM(hkdf(session_key, b"", b"netshare-channel|" + send_label, 32))
        self._recv_aes = AESGCM(hkdf(session_key, b"", b"netshare-channel|" + recv_label, 32))
        self._send_prefix = os.urandom(4)
        # Exchange nonce prefixes in the clear (they are not secret).
        super().send(self._send_prefix)
        self._recv_prefix = super().recv()
        if len(self._recv_prefix) != 4:
            raise ChannelError("bad nonce prefix from peer")
        self._send_counter = 0
        self._recv_counter = 0

    def _nonce(self, prefix: bytes, counter: int) -> bytes:
        if counter >= _MAX_COUNTER:
            raise ChannelError("nonce counter exhausted; reconnect")
        return prefix + struct.pack(">Q", counter)

    def send(self, data: bytes) -> None:
        nonce = self._nonce(self._send_prefix, self._send_counter)
        self._send_counter += 1
        super().send(self._send_aes.encrypt(nonce, data, None))

    def recv(self) -> bytes:
        blob = super().recv()
        nonce = self._nonce(self._recv_prefix, self._recv_counter)
        self._recv_counter += 1
        try:
            return self._recv_aes.decrypt(nonce, blob, None)
        except Exception as e:                       # InvalidTag and friends
            raise ChannelError("frame failed authentication (wrong key or tampering)") from e


def wrap(sock: socket.socket, session_key: Optional[bytes], is_initiator: bool) -> PlainChannel:
    """Return the best channel available for this socket."""
    if session_key and ENCRYPTION_AVAILABLE:
        return SecureChannel(sock, session_key, is_initiator)
    return PlainChannel(sock)


def status(session_key: Optional[bytes]) -> str:
    if session_key and ENCRYPTION_AVAILABLE:
        return "AES-256-GCM (paired)"
    if session_key:
        return "unencrypted — install the 'cryptography' package to enable AES-GCM"
    return "unencrypted — devices are not paired"

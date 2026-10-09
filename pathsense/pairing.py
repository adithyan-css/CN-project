"""
Level 1b — Pairing (Review 3).

Two agents that have found each other agree on a shared secret without any
server and without trusting the network, then show the user a 6-digit code
derived from that secret. If both screens show the same code, the pairing is
confirmed and the shared key protects the file transfer that follows.

Crypto used (all standard, nothing invented here):
  * X25519 elliptic-curve Diffie-Hellman for the key agreement. A pure-Python
    implementation is included so the project still runs with no third-party
    packages; if `cryptography` is installed it is used instead because it is
    much faster and constant-time.
  * HKDF-SHA256 (RFC 5869, built from hmac/hashlib) to turn the raw shared
    secret into separate keys: one for the transfer, one for the 6-digit code.
  * The 6-digit code is a Short Authentication String: it is derived from BOTH
    public keys, so a man-in-the-middle who swapped the keys would produce a
    different code on each screen and the user would see the mismatch.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import os
import secrets
from typing import Optional

try:                                                  # pragma: no cover - env dependent
    from cryptography.hazmat.primitives.asymmetric.x25519 import (
        X25519PrivateKey, X25519PublicKey)
    _HAVE_CRYPTOGRAPHY = True
except Exception:                                     # pragma: no cover
    _HAVE_CRYPTOGRAPHY = False

# --------------------------------------------------------------------------
# Pure-Python X25519 (RFC 7748). Used only when `cryptography` is missing.
# --------------------------------------------------------------------------
_P = 2 ** 255 - 19
_A24 = 121665


def _cswap(swap: int, x2: int, x3: int):
    dummy = (-swap) & (x2 ^ x3)
    return x2 ^ dummy, x3 ^ dummy


def _x25519_scalarmult(scalar: bytes, u: bytes) -> bytes:
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k_int = int.from_bytes(k, "little")
    x1 = int.from_bytes(u, "little") % _P
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in range(254, -1, -1):
        k_t = (k_int >> t) & 1
        swap ^= k_t
        x2, x3 = _cswap(swap, x2, x3)
        z2, z3 = _cswap(swap, z2, z3)
        swap = k_t
        a, b = (x2 + z2) % _P, (x2 - z2) % _P
        aa, bb = a * a % _P, b * b % _P
        e = (aa - bb) % _P
        c, d = (x3 + z3) % _P, (x3 - z3) % _P
        da, cb = d * a % _P, c * b % _P
        x3 = pow(da + cb, 2, _P)
        z3 = x1 * pow(da - cb, 2, _P) % _P
        x2 = aa * bb % _P
        z2 = e * (aa + _A24 * e) % _P
    x2, x3 = _cswap(swap, x2, x3)
    z2, z3 = _cswap(swap, z2, z3)
    return (x2 * pow(z2, _P - 2, _P) % _P).to_bytes(32, "little")


_BASE_POINT = (9).to_bytes(32, "little")


def generate_keypair() -> tuple[bytes, bytes]:
    """Return (private_key, public_key), both 32 raw bytes."""
    if _HAVE_CRYPTOGRAPHY:
        from cryptography.hazmat.primitives import serialization
        priv = X25519PrivateKey.generate()
        raw_priv = priv.private_bytes(serialization.Encoding.Raw,
                                      serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption())
        raw_pub = priv.public_key().public_bytes(serialization.Encoding.Raw,
                                                 serialization.PublicFormat.Raw)
        return raw_priv, raw_pub
    priv = bytearray(secrets.token_bytes(32))
    return bytes(priv), _x25519_scalarmult(bytes(priv), _BASE_POINT)


def shared_secret(private_key: bytes, peer_public_key: bytes) -> bytes:
    if _HAVE_CRYPTOGRAPHY:
        return X25519PrivateKey.from_private_bytes(private_key).exchange(
            X25519PublicKey.from_public_bytes(peer_public_key))
    return _x25519_scalarmult(private_key, peer_public_key)


# --------------------------------------------------------------------------
# HKDF-SHA256 (RFC 5869)
# --------------------------------------------------------------------------
def hkdf(secret: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    prk = hmac.new(salt or b"\x00" * 32, secret, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


SAS_DIGITS = 6


@dataclasses.dataclass
class PairingResult:
    session_key: bytes          # used to encrypt the transfer
    code: str                   # 6 digits shown on both screens
    peer_public_key: bytes
    confirmed: bool = False

    def to_dict(self) -> dict:
        return {"code": self.code, "confirmed": self.confirmed,
                "fingerprint": fingerprint(self.peer_public_key)}


def fingerprint(public_key: bytes) -> str:
    """Short, stable identifier for a device's key (shown in the UI)."""
    return hashlib.sha256(public_key).hexdigest()[:12]


def _ordered(a: bytes, b: bytes) -> bytes:
    """Both sides must mix the two public keys in the same order."""
    return a + b if a <= b else b + a


def derive(private_key: bytes, own_public_key: bytes, peer_public_key: bytes,
           context: bytes = b"netshare-pairing-v1") -> PairingResult:
    """Finish the key agreement and compute the shared 6-digit code."""
    if len(peer_public_key) != 32:
        raise ValueError("peer public key must be 32 bytes")
    secret = shared_secret(private_key, peer_public_key)
    salt = _ordered(own_public_key, peer_public_key)
    session_key = hkdf(secret, salt, context + b"|session", 32)
    sas_key = hkdf(secret, salt, context + b"|sas", 8)
    code = str(int.from_bytes(sas_key, "big") % (10 ** SAS_DIGITS)).zfill(SAS_DIGITS)
    return PairingResult(session_key=session_key, code=code, peer_public_key=peer_public_key)


class PairingAgent:
    """Holds this device's long-lived key and remembers confirmed peers."""

    def __init__(self, device_id: str, key_path: Optional[str] = None):
        self.device_id = device_id
        self.key_path = key_path
        self.private_key, self.public_key = self._load_or_create()
        self.sessions: dict[str, PairingResult] = {}
        self.trusted: dict[str, str] = {}       # peer_id -> key fingerprint

    def _load_or_create(self) -> tuple[bytes, bytes]:
        if self.key_path and os.path.exists(self.key_path):
            raw = open(self.key_path, "rb").read()
            if len(raw) == 32:
                if _HAVE_CRYPTOGRAPHY:
                    from cryptography.hazmat.primitives import serialization
                    pub = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
                        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
                else:
                    pub = _x25519_scalarmult(raw, _BASE_POINT)
                return raw, pub
        priv, pub = generate_keypair()
        if self.key_path:
            os.makedirs(os.path.dirname(os.path.abspath(self.key_path)), exist_ok=True)
            with open(self.key_path, "wb") as f:
                f.write(priv)
            try:
                os.chmod(self.key_path, 0o600)
            except OSError:
                pass
        return priv, pub

    def start(self, peer_id: str, peer_public_key: bytes) -> PairingResult:
        result = derive(self.private_key, self.public_key, peer_public_key)
        known = self.trusted.get(peer_id)
        if known and known == fingerprint(peer_public_key):
            result.confirmed = True      # already paired with this exact key
        self.sessions[peer_id] = result
        return result

    def confirm(self, peer_id: str, accepted: bool = True) -> bool:
        """The user tapped 'the codes match' (or 'they don't')."""
        result = self.sessions.get(peer_id)
        if result is None:
            return False
        if accepted:
            result.confirmed = True
            self.trusted[peer_id] = fingerprint(result.peer_public_key)
        else:
            self.sessions.pop(peer_id, None)
            self.trusted.pop(peer_id, None)
        return accepted

    def session_key(self, peer_id: str) -> Optional[bytes]:
        result = self.sessions.get(peer_id)
        return result.session_key if result and result.confirmed else None

    def is_paired(self, peer_id: str) -> bool:
        return self.session_key(peer_id) is not None

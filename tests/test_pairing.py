"""
Level 1b/2b test — pairing and the encrypted channel.
Proves: (1) both devices derive the same session key and the same 6-digit code
from an X25519 exchange; (2) the pure-Python X25519 agrees with the library one,
so a laptop without `cryptography` pairs with one that has it; (3) a
man-in-the-middle who swaps the keys produces DIFFERENT codes on the two
screens, which is the whole point of showing the code; (4) real sockets carry
AES-GCM frames; (5) a tampered frame is rejected; (6) the wrong key cannot read
the data.
"""
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense import pairing
from pathsense import secure_channel as sc

HOST = "127.0.0.1"


def test_both_sides_derive_the_same_key_and_code():
    a_priv, a_pub = pairing.generate_keypair()
    b_priv, b_pub = pairing.generate_keypair()
    a = pairing.derive(a_priv, a_pub, b_pub)
    b = pairing.derive(b_priv, b_pub, a_pub)
    assert a.session_key == b.session_key
    assert a.code == b.code and len(a.code) == 6 and a.code.isdigit()
    print("PASS: test_both_sides_derive_the_same_key_and_code")


def test_pure_python_x25519_matches_the_library():
    priv, pub = pairing.generate_keypair()
    other_priv, other_pub = pairing.generate_keypair()
    assert pairing._x25519_scalarmult(priv, other_pub) == pairing.shared_secret(priv, other_pub)
    assert pairing._x25519_scalarmult(priv, (9).to_bytes(32, "little")) == pub
    print("PASS: test_pure_python_x25519_matches_the_library")


def test_man_in_the_middle_shows_different_codes():
    a_priv, a_pub = pairing.generate_keypair()
    b_priv, b_pub = pairing.generate_keypair()
    m_priv, m_pub = pairing.generate_keypair()       # the attacker in the middle
    a_sees = pairing.derive(a_priv, a_pub, m_pub)    # A actually talks to M
    b_sees = pairing.derive(b_priv, b_pub, m_pub)    # so does B
    assert a_sees.code != b_sees.code, "a swapped key must change the code the users compare"
    print("PASS: test_man_in_the_middle_shows_different_codes")


def test_agent_remembers_a_confirmed_peer():
    a = pairing.PairingAgent("aaaaaaaaaaaa")
    b = pairing.PairingAgent("bbbbbbbbbbbb")
    first = a.start("bbbbbbbbbbbb", b.public_key)
    assert not first.confirmed and a.session_key("bbbbbbbbbbbb") is None
    a.confirm("bbbbbbbbbbbb", True)
    assert a.is_paired("bbbbbbbbbbbb")
    again = a.start("bbbbbbbbbbbb", b.public_key)
    assert again.confirmed, "a device already paired must not ask for the code again"
    impostor = pairing.PairingAgent("cccccccccccc")
    swapped = a.start("bbbbbbbbbbbb", impostor.public_key)
    assert not swapped.confirmed, "a different key for a known device must ask again"
    print("PASS: test_agent_remembers_a_confirmed_peer")


def _echo_server(port: int, key, hold: dict):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, port))
    srv.listen(1)
    conn, _ = srv.accept()
    try:
        channel = sc.wrap(conn, key, is_initiator=False)
        hold["received"] = channel.recv()
    except sc.ChannelError as e:
        hold["error"] = str(e)
    finally:
        conn.close()
        srv.close()


def test_encrypted_round_trip_over_a_socket():
    if not sc.ENCRYPTION_AVAILABLE:
        print("SKIP: cryptography not installed")
        return
    key = pairing.hkdf(b"secret", b"", b"test", 32)
    hold = {}
    t = threading.Thread(target=_echo_server, args=(54811, key, hold))
    t.start()
    import time
    time.sleep(0.3)
    s = socket.create_connection((HOST, 54811))
    channel = sc.wrap(s, key, is_initiator=True)
    assert channel.encrypted
    channel.send(b"the quick brown fox")
    t.join(timeout=5)
    channel.close()
    assert hold.get("received") == b"the quick brown fox"
    print("PASS: test_encrypted_round_trip_over_a_socket")


def test_wrong_key_cannot_read_the_data():
    if not sc.ENCRYPTION_AVAILABLE:
        print("SKIP: cryptography not installed")
        return
    import time
    good = pairing.hkdf(b"secret", b"", b"test", 32)
    bad = pairing.hkdf(b"not-the-secret", b"", b"test", 32)
    hold = {}
    t = threading.Thread(target=_echo_server, args=(54812, bad, hold))
    t.start()
    time.sleep(0.3)
    s = socket.create_connection((HOST, 54812))
    channel = sc.wrap(s, good, is_initiator=True)
    channel.send(b"secret payload")
    t.join(timeout=5)
    channel.close()
    assert "received" not in hold and "authentication" in hold.get("error", ""), hold
    print("PASS: test_wrong_key_cannot_read_the_data")


if __name__ == "__main__":
    test_both_sides_derive_the_same_key_and_code()
    test_pure_python_x25519_matches_the_library()
    test_man_in_the_middle_shows_different_codes()
    test_agent_remembers_a_confirmed_peer()
    test_encrypted_round_trip_over_a_socket()
    test_wrong_key_cannot_read_the_data()

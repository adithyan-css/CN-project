"""
Level 4 test — Transfer Engine.
Proves: (1) a full file arrives checksum-identical (FR-11); (2) an
interrupted transfer reconnecting to the SAME peer resumes from the last
acknowledged chunk rather than resending it (FR-12, AC-3); (3) a transfer
rerouted to a DIFFERENT peer produces a full, checksum-correct file, not a
truncated/zero-padded one (FR-13, AC-4, TRD §8.1's documented rule).
Real sockets, real files, real threads — nothing mocked.
"""
import hashlib
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathsense.transfer import TransferReceiver, TransferSender

HOST = "127.0.0.1"


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _make_source_file(tmpdir: str, name: str, total_chunks: int, chunk_size: int) -> str:
    path = os.path.join(tmpdir, name)
    with open(path, "wb") as f:
        f.write(os.urandom(total_chunks * chunk_size))
    return path


def test_full_transfer_checksum_matches():
    tmp = tempfile.mkdtemp(prefix="ps_full_")
    try:
        recv_dir = os.path.join(tmp, "recv")
        src = _make_source_file(tmp, "demo.bin", total_chunks=4, chunk_size=8192)

        receiver = TransferReceiver(53101, recv_dir)
        receiver.start()
        time.sleep(0.3)
        try:
            sender = TransferSender(src, chunk_size=8192)
            ok = sender.send_to("peer-full", HOST, 53101)
            assert ok, "transfer did not report success"
            time.sleep(0.3)
            dest = os.path.join(recv_dir, "demo.bin")
            assert os.path.exists(dest)
            assert _sha256_file(dest) == _sha256_file(src), "received file checksum does not match source"
            print("PASS: test_full_transfer_checksum_matches")
        finally:
            receiver.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resume_same_peer_after_interruption():
    tmp = tempfile.mkdtemp(prefix="ps_resume_")
    try:
        recv_dir = os.path.join(tmp, "recv")
        chunk_size = 4096
        total_chunks = 6
        src = _make_source_file(tmp, "resume.bin", total_chunks=total_chunks, chunk_size=chunk_size)

        receiver = TransferReceiver(53102, recv_dir)
        receiver.start()
        time.sleep(0.3)
        try:
            sender = TransferSender(src, chunk_size=chunk_size)
            acked_snapshots = []

            def on_change(state):
                acked_snapshots.append(set(state.acked_chunks))
                if len(state.acked_chunks) >= 3:
                    sender.cancel_current()

            sender.on_state_change = on_change

            first_result = sender.send_to("peer-B", HOST, 53102)
            assert first_result is False, "expected the interrupted transfer to report failure/incomplete"
            assert len(sender.state.acked_chunks) >= 3
            acked_before_resume = set(sender.state.acked_chunks)
            assert acked_before_resume == {0, 1, 2}, f"expected exactly chunks 0,1,2 acked before cancel, got {acked_before_resume}"

            mark = len(acked_snapshots)
            # detach the auto-cancel so the resume can run to completion
            sender.on_state_change = lambda state: acked_snapshots.append(set(state.acked_chunks))

            second_result = sender.send_to("peer-B", HOST, 53102)  # same peer_id -> resume
            assert second_result is True, "resume to the same peer did not complete"

            new_events = [s for s in acked_snapshots[mark:] if s != acked_before_resume]
            assert new_events, "resume produced no chunk-ack events"
            first_new_set = new_events[0]
            newly_added = first_new_set - acked_before_resume
            assert newly_added == {3}, (
                f"resume must continue from chunk 3, not resend 0-2; "
                f"first newly-acked index after resume was {newly_added}"
            )

            dest = os.path.join(recv_dir, "resume.bin")
            assert _sha256_file(dest) == _sha256_file(src), "resumed file checksum does not match source"
            print("PASS: test_resume_same_peer_after_interruption")
        finally:
            receiver.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reroute_to_different_peer_sends_full_file_not_partial():
    tmp = tempfile.mkdtemp(prefix="ps_reroute_")
    try:
        recv_dir_b = os.path.join(tmp, "recv_b")
        recv_dir_c = os.path.join(tmp, "recv_c")
        chunk_size = 4096
        total_chunks = 6
        src = _make_source_file(tmp, "reroute.bin", total_chunks=total_chunks, chunk_size=chunk_size)

        receiver_b = TransferReceiver(53103, recv_dir_b)
        receiver_c = TransferReceiver(53104, recv_dir_c)
        receiver_b.start()
        receiver_c.start()
        time.sleep(0.3)
        try:
            sender = TransferSender(src, chunk_size=chunk_size)

            def on_change(state):
                if len(state.acked_chunks) >= 3:
                    sender.cancel_current()

            sender.on_state_change = on_change
            interrupted_result = sender.send_to("peer-B", HOST, 53103)
            assert interrupted_result is False
            assert len(sender.state.acked_chunks) >= 3

            sender.on_state_change = None
            rerouted_result = sender.send_to("peer-C", HOST, 53104)  # DIFFERENT peer_id -> full resend
            assert rerouted_result is True, "reroute to a different peer did not complete"
            assert sender.state.acked_chunks == set(range(total_chunks)), (
                "reroute must send every chunk to the new peer, not just the 'remaining' ones"
            )

            dest_c = os.path.join(recv_dir_c, "reroute.bin")
            assert os.path.exists(dest_c)
            assert os.path.getsize(dest_c) == os.path.getsize(src), "rerouted file on new peer is truncated"
            assert _sha256_file(dest_c) == _sha256_file(src), "rerouted file on new peer is corrupted/partial"
            print("PASS: test_reroute_to_different_peer_sends_full_file_not_partial")
        finally:
            receiver_b.stop()
            receiver_c.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_full_transfer_checksum_matches()
    test_resume_same_peer_after_interruption()
    test_reroute_to_different_peer_sends_full_file_not_partial()

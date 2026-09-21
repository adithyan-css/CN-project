"""
Level 4 test — Transfer Engine.
Proves: (1) a full file arrives checksum-identical (FR-11); (2) an
interrupted transfer reconnecting to the SAME peer resumes from the last
acknowledged chunk rather than resending it (FR-12, AC-3); (3) Review 2:
a sender is bound to one receiver and refuses to send the file to a
different device; pause() stops cleanly with status PAUSED and progress kept.
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


def test_sender_refuses_a_different_receiver():
    tmp = tempfile.mkdtemp(prefix="ps_bound_")
    try:
        src = _make_source_file(tmp, "bound.bin", total_chunks=4, chunk_size=4096)
        receiver = TransferReceiver(53103, os.path.join(tmp, "recv_b"))
        receiver.start()
        time.sleep(0.3)
        try:
            sender = TransferSender(src, chunk_size=4096)
            assert sender.send_to("peer-B", HOST, 53103) is True
            try:
                sender.send_to("peer-C", HOST, 53104)
            except ValueError:
                pass
            else:
                raise AssertionError("sender must refuse to send a transfer to a different receiver")
            assert sender.state.peer_id == "peer-B"
            print("PASS: test_sender_refuses_a_different_receiver")
        finally:
            receiver.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_pause_keeps_progress_and_resume_completes():
    tmp = tempfile.mkdtemp(prefix="ps_pause_")
    try:
        recv_dir = os.path.join(tmp, "recv_b")
        chunk_size, total_chunks = 4096, 10
        src = _make_source_file(tmp, "pause.bin", total_chunks=total_chunks, chunk_size=chunk_size)
        receiver = TransferReceiver(53105, recv_dir)
        receiver.start()
        time.sleep(0.3)
        try:
            sender = TransferSender(src, chunk_size=chunk_size)

            def on_change(state):
                if len(state.acked_chunks) >= 4 and state.pauses == 0:
                    sender.pause()

            sender.on_state_change = on_change
            assert sender.send_to("peer-B", HOST, 53105) is False
            assert sender.state.status == "PAUSED"
            kept = len(sender.state.acked_chunks)
            assert kept >= 4 and sender.state.pauses == 1
            assert sender.next_chunk == kept

            sender.on_state_change = None
            assert sender.send_to("peer-B", HOST, 53105) is True
            assert sender.state.resumes == 1 and sender.state.status == "COMPLETE"
            assert _sha256_file(os.path.join(recv_dir, "pause.bin")) == _sha256_file(src)
            print("PASS: test_pause_keeps_progress_and_resume_completes")
        finally:
            receiver.stop()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_full_transfer_checksum_matches()
    test_resume_same_peer_after_interruption()
    test_sender_refuses_a_different_receiver()
    test_pause_keeps_progress_and_resume_completes()

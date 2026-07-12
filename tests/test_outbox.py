import os
import tempfile
import unittest
from pathlib import Path

from assayer.outbox import Outbox, OutboxError
from tests.fake_store import FakeStore


class OutboxEnqueueTests(unittest.TestCase):
    def test_enqueue_persists_a_pending_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.jsonl"))
            entry_id = outbox.enqueue("assayer_quarantine", {"a": 1})
            self.assertTrue(entry_id)
            pending = outbox.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["id"], entry_id)
            self.assertEqual(pending[0]["row"], {"a": 1})
            self.assertEqual(pending[0]["status"], "pending")

    def test_file_created_with_0600_perms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outbox.jsonl"
            outbox = Outbox(path=str(path))
            outbox.enqueue("assayer_quarantine", {"a": 1})
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "dir" / "outbox.jsonl"
            outbox = Outbox(path=str(path))
            outbox.enqueue("assayer_quarantine", {"a": 1})
            self.assertTrue(path.exists())

    def test_multiple_enqueues_all_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.jsonl"))
            outbox.enqueue("assayer_quarantine", {"a": 1})
            outbox.enqueue("assayer_traces", {"b": 2})
            self.assertEqual(len(outbox.pending()), 2)

    def test_raises_outbox_error_when_path_unwritable(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Point the outbox at a path whose "parent" is actually a file,
            # so os.makedirs necessarily fails -> real, reproducible
            # unwritable-destination condition.
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a directory")
            bad_path = blocker / "nested" / "outbox.jsonl"
            outbox = Outbox(path=str(bad_path))
            with self.assertRaises(OutboxError):
                outbox.enqueue("assayer_quarantine", {"a": 1})


class OutboxRotationTests(unittest.TestCase):
    def test_rotates_past_max_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "outbox.jsonl"
            outbox = Outbox(path=str(path), max_bytes=10)
            outbox.enqueue("assayer_quarantine", {"a": 1})
            # First entry alone should already exceed 10 bytes; the second
            # enqueue must trigger a rotation of the first file.
            outbox.enqueue("assayer_quarantine", {"b": 2})
            rotated = list(Path(tmp).glob("outbox.jsonl.*"))
            self.assertTrue(rotated, "expected at least one rotated file")


class OutboxReplayTests(unittest.TestCase):
    def test_replay_moves_successful_entries_out_of_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.jsonl"))
            outbox.enqueue("assayer_quarantine", {"pipeline": "p", "payload_json": {}})
            store = FakeStore()
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual(replayed, 1)
            self.assertEqual(still_pending, 0)
            self.assertEqual(dead_lettered, 0)
            self.assertEqual(len(outbox.pending()), 0)
            self.assertEqual(len(store.quarantine), 1)

    def test_replay_dispatches_traces_to_insert_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.jsonl"))
            outbox.enqueue("assayer_traces", {"pipeline": "p", "item_hash": "h", "verdict": "pass"})
            store = FakeStore()
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual(replayed, 1)
            self.assertEqual(len(store.traces), 1)

    def test_replay_requeues_failed_entries_with_incremented_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.jsonl"), max_attempts=5)
            outbox.enqueue("assayer_quarantine", {"pipeline": "p"})
            store = FakeStore(raise_on_insert_quarantine=True)
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual(replayed, 0)
            self.assertEqual(still_pending, 1)
            self.assertEqual(dead_lettered, 0)
            self.assertEqual(outbox.pending()[0]["attempts"], 1)

    def test_replay_dead_letters_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.jsonl"), max_attempts=2)
            outbox.enqueue("assayer_quarantine", {"pipeline": "p"})
            store = FakeStore(raise_on_insert_quarantine=True)

            outbox.replay(store)  # attempts -> 1, still pending
            replayed, still_pending, dead_lettered = outbox.replay(store)  # attempts -> 2, dead-lettered

            self.assertEqual(replayed, 0)
            self.assertEqual(still_pending, 0)
            self.assertEqual(dead_lettered, 1)
            self.assertEqual(len(outbox.pending()), 0)
            self.assertEqual(len(outbox.dead_letters()), 1)

    def test_replay_on_empty_outbox_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.jsonl"))
            store = FakeStore()
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual((replayed, still_pending, dead_lettered), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()

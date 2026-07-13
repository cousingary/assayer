import json
import multiprocessing
import signal
import threading
import time
import unittest
from pathlib import Path

from assayer.outbox import Outbox, OutboxError
from tests.fake_store import FakeStore


class BlockingStore:
    """Store whose insert_quarantine blocks until released -- lets a test
    hold replay() inside its "persist to backend" step so a concurrent
    enqueue() can be exercised against the exact window that lost data
    under the old JSONL outbox (Sol redteam v3 finding #6)."""

    def __init__(self):
        self.quarantine = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def insert_quarantine(self, row):
        self.entered.set()
        self.release.wait(timeout=5)
        self.quarantine.append(row)
        return len(self.quarantine)

    def insert_trace(self, row):
        return row


def _claim_lease_then_sigkill(db_path, lease_seconds):
    """Run in a child process: claim a lease on the one pending entry, then
    hard-kill this process before it ever completes the row. Used to prove
    lease-based crash recovery is real (an actual dead process), not a
    simulated failure path."""
    ob = Outbox(path=db_path)
    ob._claim_batch("crashing-worker", time.time() + lease_seconds, 10)
    import os

    os.kill(os.getpid(), signal.SIGKILL)


class OutboxEnqueueTests(unittest.TestCase):
    def test_enqueue_persists_a_pending_entry(self):
        with self._tmp() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.db"))
            entry_id = outbox.enqueue("assayer_quarantine", {"a": 1})
            self.assertTrue(entry_id)
            pending = outbox.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["id"], entry_id)
            self.assertEqual(pending[0]["row"], {"a": 1})
            self.assertEqual(pending[0]["status"], "pending")

    def test_db_file_created_with_0600_perms(self):
        with self._tmp() as tmp:
            import os

            path = Path(tmp) / "outbox.db"
            Outbox(path=str(path))
            mode = os.stat(path).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_creates_parent_directory(self):
        with self._tmp() as tmp:
            path = Path(tmp) / "nested" / "dir" / "outbox.db"
            Outbox(path=str(path))
            self.assertTrue(path.exists())

    def test_multiple_enqueues_all_pending(self):
        with self._tmp() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.db"))
            outbox.enqueue("assayer_quarantine", {"a": 1})
            outbox.enqueue("assayer_traces", {"b": 2})
            self.assertEqual(len(outbox.pending()), 2)

    def test_raises_outbox_error_when_path_unwritable(self):
        with self._tmp() as tmp:
            # Point the outbox at a path whose "parent" is actually a file,
            # so os.makedirs necessarily fails -> real, reproducible
            # unwritable-destination condition. Construction (not enqueue)
            # now opens the database, so it fails fast at Outbox(...).
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a directory")
            bad_path = blocker / "nested" / "outbox.db"
            with self.assertRaises(OutboxError):
                Outbox(path=str(bad_path))

    def _tmp(self):
        import tempfile

        return tempfile.TemporaryDirectory()


class OutboxReplayTests(unittest.TestCase):
    def test_replay_moves_successful_entries_out_of_pending(self):
        with self._tmp() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.db"))
            outbox.enqueue("assayer_quarantine", {"pipeline": "p", "payload_json": {}})
            store = FakeStore()
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual(replayed, 1)
            self.assertEqual(still_pending, 0)
            self.assertEqual(dead_lettered, 0)
            self.assertEqual(len(outbox.pending()), 0)
            self.assertEqual(len(store.quarantine), 1)

    def test_replay_dispatches_traces_to_insert_trace(self):
        with self._tmp() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.db"))
            outbox.enqueue("assayer_traces", {"pipeline": "p", "item_hash": "h", "verdict": "pass"})
            store = FakeStore()
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual(replayed, 1)
            self.assertEqual(len(store.traces), 1)

    def test_replay_requeues_failed_entries_with_incremented_attempts(self):
        with self._tmp() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.db"), max_attempts=5)
            outbox.enqueue("assayer_quarantine", {"pipeline": "p"})
            store = FakeStore(raise_on_insert_quarantine=True)
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual(replayed, 0)
            self.assertEqual(still_pending, 1)
            self.assertEqual(dead_lettered, 0)
            self.assertEqual(outbox.pending()[0]["attempts"], 1)

    def test_replay_dead_letters_after_max_attempts(self):
        with self._tmp() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.db"), max_attempts=2)
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
        with self._tmp() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "outbox.db"))
            store = FakeStore()
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual((replayed, still_pending, dead_lettered), (0, 0, 0))

    def _tmp(self):
        import tempfile

        return tempfile.TemporaryDirectory()


class Sol3OutboxConcurrencyTests(unittest.TestCase):
    """Corpus #7: 'concurrent Assayer enqueue and replay'. Reproduces the
    exact audit scenario (entry enqueued while replay is blocked inside the
    backing store) and the closely-related 'two replayers race the same
    row' weakness, using real threads with independent sqlite3 connections
    -- the atomicity under test is SQLite's own file-level transaction
    locking, not a Python-level mock."""

    def test_sol3_enqueue_during_blocked_replay_is_not_lost(self):
        with self._tmp() as tmp:
            path = str(Path(tmp) / "outbox.db")
            outbox = Outbox(path=path)
            entry_a = outbox.enqueue("assayer_quarantine", {"which": "a"})

            store = BlockingStore()
            result = {}

            def run_replay():
                result["value"] = outbox.replay(store)

            t = threading.Thread(target=run_replay)
            t.start()
            self.assertTrue(store.entered.wait(timeout=5), "replay never entered the blocking insert")

            # Independent connection to the same file enqueues while
            # entry_a's replay is still blocked inside the store -- this is
            # the exact reproduction from the audit (weakness 5 in
            # governator-sol-upgrade3.md #6): under the old JSONL outbox,
            # replay's stale in-memory snapshot (taken before this enqueue)
            # would silently drop entry_b when it rewrote the file.
            outbox2 = Outbox(path=path)
            entry_b = outbox2.enqueue("assayer_quarantine", {"which": "b"})

            store.release.set()
            t.join(timeout=5)

            replayed, still_pending, dead_lettered = result["value"]
            self.assertEqual(replayed, 1)
            self.assertEqual(dead_lettered, 0)
            self.assertEqual(store.quarantine, [{"which": "a"}])

            pending_ids = {e["id"] for e in outbox2.pending()}
            self.assertIn(entry_b, pending_ids, "entry enqueued during blocked replay must not be lost")
            self.assertNotIn(entry_a, pending_ids)

    def test_sol3_two_concurrent_replayers_never_double_process(self):
        with self._tmp() as tmp:
            path = str(Path(tmp) / "outbox.db")
            seed = Outbox(path=path)
            n = 40
            for i in range(n):
                seed.enqueue("assayer_quarantine", {"i": i})

            store_a, store_b = FakeStore(), FakeStore()
            outbox_a, outbox_b = Outbox(path=path), Outbox(path=path)
            barrier = threading.Barrier(2)
            results = {}

            def worker(name, outbox, store):
                barrier.wait()
                results[name] = outbox.replay(store, batch_size=n)

            ta = threading.Thread(target=worker, args=("a", outbox_a, store_a))
            tb = threading.Thread(target=worker, args=("b", outbox_b, store_b))
            ta.start()
            tb.start()
            ta.join(timeout=10)
            tb.join(timeout=10)

            total_replayed = results["a"][0] + results["b"][0]
            self.assertEqual(total_replayed, n)
            seen = [row["i"] for row in store_a.quarantine] + [row["i"] for row in store_b.quarantine]
            self.assertEqual(sorted(seen), list(range(n)), "every row must be processed exactly once")
            self.assertEqual(len(outbox_a.pending()), 0)

    def _tmp(self):
        import tempfile

        return tempfile.TemporaryDirectory()


class Sol3OutboxCrashRecoveryTests(unittest.TestCase):
    def test_sol3_kill_mid_replay_recovers_via_lease_expiry(self):
        with self._tmp() as tmp:
            path = str(Path(tmp) / "outbox.db")
            outbox = Outbox(path=path)
            outbox.enqueue("assayer_quarantine", {"pipeline": "p"})

            proc = multiprocessing.Process(target=_claim_lease_then_sigkill, args=(path, 1))
            proc.start()
            proc.join(timeout=10)
            self.assertEqual(proc.exitcode, -signal.SIGKILL, "child must have been hard-killed")

            # Right after the crash the row is still leased (processing);
            # a fresh replay must not touch it until the lease expires.
            self.assertEqual(len(outbox.pending()), 0)

            time.sleep(1.2)
            store = FakeStore()
            replayed, still_pending, dead_lettered = outbox.replay(store)
            self.assertEqual(replayed, 1)
            self.assertEqual(store.quarantine[0]["pipeline"], "p")

    def _tmp(self):
        import tempfile

        return tempfile.TemporaryDirectory()


class Sol3OutboxMigrationTests(unittest.TestCase):
    """Corpus '#7 additional weaknesses': malformed lines silently
    discarded, and migration must be safe to run more than once."""

    def test_sol3_legacy_jsonl_at_same_path_is_auto_migrated_on_open(self):
        with self._tmp() as tmp:
            # Simulate an operator upgrading the code without moving
            # ASSAYER_OUTBOX: the path that used to hold JSONL now gets
            # opened by the SQLite-backed Outbox.
            legacy_path = Path(tmp) / "outbox.db"
            good_line = json.dumps(
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "queued_at": time.time(),
                    "table_hint": "assayer_quarantine",
                    "row": {"pipeline": "p"},
                    "attempts": 0,
                    "status": "pending",
                }
            )
            legacy_path.write_text(good_line + "\n" + "{this is not json\n")

            outbox = Outbox(path=str(legacy_path))

            pending = outbox.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["row"], {"pipeline": "p"})

            dead = outbox.dead_letters()
            self.assertEqual(len(dead), 1, "malformed line must be preserved, not discarded")
            self.assertEqual(dead[0]["table_hint"], "malformed_migration")
            self.assertIn("this is not json", dead[0]["row"]["raw_line"])

            # The original file content is preserved on disk for audit,
            # not deleted.
            self.assertTrue((Path(tmp) / "outbox.db.pre-sqlite-migration").exists())

    def test_sol3_double_migration_is_a_noop(self):
        with self._tmp() as tmp:
            jsonl_path = Path(tmp) / "legacy.jsonl"
            entry = {
                "id": "22222222-2222-2222-2222-222222222222",
                "queued_at": time.time(),
                "table_hint": "assayer_quarantine",
                "row": {"pipeline": "p"},
                "attempts": 0,
                "status": "pending",
            }
            jsonl_path.write_text(json.dumps(entry) + "\n")

            outbox = Outbox(path=str(Path(tmp) / "outbox.db"))
            outbox.migrate_from_jsonl(str(jsonl_path))
            outbox.migrate_from_jsonl(str(jsonl_path))

            self.assertEqual(len(outbox.pending()), 1, "second migration must not duplicate the entry")

    def _tmp(self):
        import tempfile

        return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()

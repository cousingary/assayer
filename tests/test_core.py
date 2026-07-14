import tempfile
import unittest
from pathlib import Path

from assayer import item_hash, quarantine_item, trace
from assayer.outbox import Outbox, OutboxError
from tests.fake_store import FakeStore


class ItemHashTests(unittest.TestCase):
    def test_stable_for_same_input(self):
        h1 = item_hash("hello world")
        h2 = item_hash("hello world")
        self.assertEqual(h1, h2)

    def test_differs_for_different_input(self):
        h1 = item_hash("hello world")
        h2 = item_hash("hello world!")
        self.assertNotEqual(h1, h2)

    def test_is_sha256_hex_length(self):
        h = item_hash("anything")
        self.assertEqual(len(h), 64)
        int(h, 16)  # raises if not valid hex

    def test_utf8_sensitive(self):
        h1 = item_hash("café")
        h2 = item_hash("cafe")
        self.assertNotEqual(h1, h2)


class TraceTests(unittest.TestCase):
    def test_trace_calls_insert_trace_with_expected_row(self):
        store = FakeStore()
        trace(
            store,
            pipeline="content_pipeline",
            job_id="job-1",
            item_hash="abc123",
            verdict="pass",
            checks={"schema": {"ok": True, "detail": "ok"}},
            model="gpt-4.1-mini",
            tokens_in=100,
            tokens_out=200,
            cost_usd=0.01,
            quarantine_id=None,
            duration_ms=42,
        )
        self.assertEqual(len(store.traces), 1)
        row = store.traces[0]
        self.assertEqual(row["pipeline"], "content_pipeline")
        self.assertEqual(row["job_id"], "job-1")
        self.assertEqual(row["item_hash"], "abc123")
        self.assertEqual(row["verdict"], "pass")
        self.assertEqual(row["model"], "gpt-4.1-mini")
        self.assertIn("ts", row)

    def test_trace_never_raises_when_insert_trace_fails(self):
        store = FakeStore(raise_on_insert_trace=True)
        try:
            trace(
                store,
                pipeline="p",
                job_id="j",
                item_hash="h",
                verdict="fail",
                checks={},
            )
        except Exception as exc:
            self.fail(f"trace() raised unexpectedly: {exc}")
        self.assertEqual(len(store.fallback_calls), 1)

    def test_trace_never_raises_when_insert_trace_and_fallback_both_fail(self):
        store = FakeStore(raise_on_insert_trace=True, raise_on_fallback=True)
        try:
            trace(
                store,
                pipeline="p",
                job_id="j",
                item_hash="h",
                verdict="error",
                checks={},
            )
        except Exception as exc:
            self.fail(f"trace() raised unexpectedly: {exc}")

    def test_trace_never_raises_when_store_has_no_fallback_attr(self):
        class MinimalStore:
            def insert_trace(self, row):
                raise RuntimeError("nope")

        try:
            trace(
                MinimalStore(),
                pipeline="p",
                job_id="j",
                item_hash="h",
                verdict="fail",
                checks={},
            )
        except Exception as exc:
            self.fail(f"trace() raised unexpectedly: {exc}")


class QuarantineItemTests(unittest.TestCase):
    def test_returns_new_id_on_success(self):
        store = FakeStore()
        qid = quarantine_item(
            store,
            pipeline="p",
            item_hash="h",
            payload={"body": "text"},
            input_ref="ref-1",
            reasons=["nonempty:body failed"],
        )
        self.assertEqual(qid, 1)
        self.assertEqual(len(store.quarantine), 1)
        row = store.quarantine[0]
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["pipeline"], "p")
        self.assertEqual(row["payload_json"], {"body": "text"})
        self.assertEqual(row["reasons_json"], ["nonempty:body failed"])

    def test_durably_queues_to_outbox_on_store_failure(self):
        # Sol audit Assayer weakness 5: quarantine evidence is blocking, not
        # optional telemetry, so a primary-insert failure must land in the
        # durable local outbox (not the old silently-swallowing
        # Store.fallback()) — verified here by reading the outbox file back.
        store = FakeStore(raise_on_insert_quarantine=True)
        with tempfile.TemporaryDirectory() as tmp:
            outbox = Outbox(path=str(Path(tmp) / "quarantine_outbox.jsonl"))
            qid = quarantine_item(
                store,
                pipeline="p",
                item_hash="h",
                payload={"body": "text"},
                input_ref="ref-1",
                reasons=["bad"],
                outbox=outbox,
            )
            self.assertIsNone(qid)
            self.assertEqual(len(store.fallback_calls), 0)
            pending = outbox.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["table_hint"], "assayer_quarantine")
            self.assertEqual(pending[0]["row"]["payload_json"], {"body": "text"})

    def test_redacts_sensitive_payload_before_storing(self):
        store = FakeStore()
        qid = quarantine_item(
            store,
            pipeline="p",
            item_hash="h",
            payload={"api_key": "sk-abcdefghijklmnopqrstuvwxyz123456", "content": "safe text"},
            input_ref="ref-1",
            reasons=["bad"],
        )
        self.assertEqual(qid, 1)
        row = store.quarantine[0]
        self.assertIn("[REDACTED:key:", row["payload_json"]["api_key"])
        self.assertEqual(row["payload_json"]["content"], "safe text")
        self.assertEqual(row["data_classification"], "sensitive")
        self.assertFalse(row["payload_encrypted"])

    def test_non_sensitive_payload_classified_standard_and_unmodified(self):
        store = FakeStore()
        quarantine_item(
            store, pipeline="p", item_hash="h",
            payload={"body": "text"}, input_ref="ref-1", reasons=["bad"],
        )
        row = store.quarantine[0]
        self.assertEqual(row["payload_json"], {"body": "text"})
        self.assertEqual(row["data_classification"], "standard")

    def test_raises_when_insert_and_outbox_both_fail(self):
        # The one case Sol's weakness explicitly says must NOT be silently
        # swallowed for blocking evidence: if even the durable outbox can't
        # be written, that is genuine evidence loss and must propagate.
        store = FakeStore(raise_on_insert_quarantine=True)

        class AlwaysFailingOutbox:
            def enqueue(self, table_hint, row):
                raise OutboxError("disk full")

        with self.assertRaises(OutboxError):
            quarantine_item(
                store,
                pipeline="p",
                item_hash="h",
                payload={"body": "text"},
                input_ref="ref-1",
                reasons=["bad"],
                outbox=AlwaysFailingOutbox(),
            )


if __name__ == "__main__":
    unittest.main()

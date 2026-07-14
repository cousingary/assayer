import json
import os
import tempfile
import unittest
from pathlib import Path

from assayer import evidence
from tests.fake_store import FakeStore


class ClassifyAndRedactTests(unittest.TestCase):
    def test_no_change_for_non_sensitive_dict(self):
        value = {"body": "text", "n": 1, "nested": {"a": [1, 2, "three"]}}
        redacted, found = evidence.classify_and_redact(value)
        self.assertEqual(redacted, value)
        self.assertFalse(found)

    def test_redacts_sensitive_key_regardless_of_value_type(self):
        value = {"api_key": "anything-at-all", "content": "safe"}
        redacted, found = evidence.classify_and_redact(value)
        self.assertTrue(found)
        self.assertTrue(redacted["api_key"].startswith("[REDACTED:key:"))
        self.assertEqual(redacted["content"], "safe")

    def test_redacts_sensitive_key_case_insensitive_and_nested(self):
        value = {"config": {"DB_PASSWORD": "hunter2"}}
        redacted, found = evidence.classify_and_redact(value)
        self.assertTrue(found)
        self.assertIn("[REDACTED:key:", redacted["config"]["DB_PASSWORD"])

    def test_redacts_aws_key_embedded_in_free_text(self):
        value = {"content": "here is my key AKIAABCDEFGHIJKLMNOP for prod"}
        redacted, found = evidence.classify_and_redact(value)
        self.assertTrue(found)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", redacted["content"])
        self.assertIn("[REDACTED:aws_access_key_id]", redacted["content"])

    def test_redacts_pem_private_key_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBogIBAAJ...\n-----END RSA PRIVATE KEY-----"
        redacted, found = evidence.classify_and_redact({"content": pem})
        self.assertTrue(found)
        self.assertNotIn("MIIBogIBAAJ", redacted["content"])

    def test_redacts_openai_style_key_in_text(self):
        value = {"content": "use sk-abcdefghijklmnopqrstuvwxyz123456 to call the API"}
        redacted, found = evidence.classify_and_redact(value)
        self.assertTrue(found)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", redacted["content"])

    def test_walks_lists(self):
        value = {"items": [{"password": "x"}, {"content": "safe"}]}
        redacted, found = evidence.classify_and_redact(value)
        self.assertTrue(found)
        self.assertIn("[REDACTED:key:", redacted["items"][0]["password"])
        self.assertEqual(redacted["items"][1]["content"], "safe")

    def test_non_dict_non_string_values_pass_through(self):
        redacted, found = evidence.classify_and_redact([1, 2.5, True, None])
        self.assertEqual(redacted, [1, 2.5, True, None])
        self.assertFalse(found)


class ClassificationForTests(unittest.TestCase):
    def test_standard_when_nothing_found(self):
        self.assertEqual(evidence.classification_for(False), "standard")

    def test_sensitive_when_found(self):
        self.assertEqual(evidence.classification_for(True), "sensitive")


class EnforceSizeLimitTests(unittest.TestCase):
    def test_passthrough_under_limit(self):
        value = {"content": "short"}
        result, truncated = evidence.enforce_size_limit(value)
        self.assertEqual(result, value)
        self.assertFalse(truncated)

    def test_truncates_oversized_payload(self):
        value = {"content": "x" * 1000}
        result, truncated = evidence.enforce_size_limit(value, max_bytes=100)
        self.assertTrue(truncated)
        self.assertTrue(result["_size_limit_exceeded"])
        self.assertGreater(result["_original_size_bytes"], 100)
        self.assertLessEqual(len(result["_truncated_preview"].encode("utf-8")), 100)


class EncryptionTests(unittest.TestCase):
    def setUp(self):
        self._old_key = os.environ.pop("ASSAYER_EVIDENCE_KEY", None)

    def tearDown(self):
        if self._old_key is not None:
            os.environ["ASSAYER_EVIDENCE_KEY"] = self._old_key
        else:
            os.environ.pop("ASSAYER_EVIDENCE_KEY", None)

    def test_passthrough_when_not_configured(self):
        value = {"content": "secret stuff"}
        stored, encrypted = evidence.encrypt_if_configured(value)
        self.assertEqual(stored, value)
        self.assertFalse(encrypted)

    def test_round_trips_when_configured(self):
        from cryptography.fernet import Fernet

        os.environ["ASSAYER_EVIDENCE_KEY"] = Fernet.generate_key().decode("utf-8")
        value = {"content": "secret stuff", "n": 1}
        stored, encrypted = evidence.encrypt_if_configured(value)
        self.assertTrue(encrypted)
        self.assertIsInstance(stored, str)
        self.assertNotIn("secret stuff", stored)
        recovered = evidence.decrypt_if_configured(stored, encrypted)
        self.assertEqual(recovered, value)

    def test_decrypt_raises_when_key_missing(self):
        with self.assertRaises(RuntimeError):
            evidence.decrypt_if_configured("some-opaque-token", True)

    def test_decrypt_passthrough_when_not_encrypted(self):
        value = {"content": "plain"}
        self.assertEqual(evidence.decrypt_if_configured(value, False), value)


class PurgeExpiredQuarantineTests(unittest.TestCase):
    def test_deletes_only_expired_terminal_status_rows(self):
        store = FakeStore()
        store.quarantine = [
            {"id": 1, "status": "discarded", "ts": "2020-01-01T00:00:00+00:00"},
            {"id": 2, "status": "discarded", "ts": "2099-01-01T00:00:00+00:00"},
            {"id": 3, "status": "open", "ts": "2020-01-01T00:00:00+00:00"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = str(Path(tmp) / "deletion_audit.jsonl")
            record = evidence.purge_expired_quarantine(
                store, retention_days=30, statuses=("discarded",), audit_path=audit_path
            )

            remaining_ids = {r["id"] for r in store.quarantine}
            self.assertEqual(remaining_ids, {2, 3}, "only the expired discarded row must be deleted")
            self.assertEqual(record["deleted_ids"], [1])
            self.assertEqual(record["count"], 1)

            with open(audit_path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 1)
            logged = json.loads(lines[0])
            self.assertEqual(logged["deleted_ids"], [1])

    def test_never_purges_open_rows(self):
        store = FakeStore()
        store.quarantine = [{"id": 1, "status": "open", "ts": "2020-01-01T00:00:00+00:00"}]
        with tempfile.TemporaryDirectory() as tmp:
            evidence.purge_expired_quarantine(
                store, retention_days=1, statuses=("released", "discarded"),
                audit_path=str(Path(tmp) / "audit.jsonl"),
            )
        self.assertEqual(len(store.quarantine), 1)

    def test_noop_still_writes_audit_record(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = str(Path(tmp) / "audit.jsonl")
            record = evidence.purge_expired_quarantine(store, retention_days=30, audit_path=audit_path)
            self.assertEqual(record["count"], 0)
            self.assertTrue(Path(audit_path).exists())


if __name__ == "__main__":
    unittest.main()

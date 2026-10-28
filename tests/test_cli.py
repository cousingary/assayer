import subprocess
import unittest
from datetime import datetime, timedelta, timezone

from cli import assayer_identity, compute_stats, parse_since


class ParseSinceTests(unittest.TestCase):
    def test_days_suffix(self):
        cutoff = parse_since("7d")
        parsed = datetime.fromisoformat(cutoff)
        expected = datetime.now(timezone.utc) - timedelta(days=7)
        self.assertAlmostEqual(parsed.timestamp(), expected.timestamp(), delta=5)

    def test_hours_suffix(self):
        cutoff = parse_since("24h")
        parsed = datetime.fromisoformat(cutoff)
        expected = datetime.now(timezone.utc) - timedelta(hours=24)
        self.assertAlmostEqual(parsed.timestamp(), expected.timestamp(), delta=5)

    def test_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            parse_since("banana")

    def test_invalid_unit_raises(self):
        with self.assertRaises(ValueError):
            parse_since("7w")

    def test_missing_number_raises(self):
        with self.assertRaises(ValueError):
            parse_since("d")


class ComputeStatsTests(unittest.TestCase):
    def test_aggregates_pipeline_and_model_counts(self):
        rows = [
            {
                "pipeline": "content_pipeline",
                "verdict": "pass",
                "model": "gpt-4.1-mini",
                "checks_json": {"schema": {"ok": True, "detail": "ok"}},
            },
            {
                "pipeline": "content_pipeline",
                "verdict": "fail",
                "model": None,
                "checks_json": {
                    "schema": {"ok": True, "detail": "ok"},
                    "nonempty:body": {"ok": False, "detail": "too short"},
                },
            },
        ]
        per_pipeline, per_check_fail, per_model = compute_stats(rows)
        self.assertEqual(per_pipeline["content_pipeline"]["pass"], 1)
        self.assertEqual(per_pipeline["content_pipeline"]["fail"], 1)
        self.assertEqual(per_check_fail["nonempty:body"], 1)
        self.assertEqual(per_model["unknown"]["fail"], 1)
        self.assertEqual(per_model["gpt-4.1-mini"]["pass"], 1)

    def test_handles_checks_json_as_string(self):
        rows = [
            {
                "pipeline": "p",
                "verdict": "fail",
                "model": "x",
                "checks_json": '{"a": {"ok": false, "detail": "bad"}}',
            }
        ]
        _, per_check_fail, _ = compute_stats(rows)
        self.assertEqual(per_check_fail["a"], 1)

    def test_empty_rows(self):
        per_pipeline, per_check_fail, per_model = compute_stats([])
        self.assertEqual(per_pipeline, {})
        self.assertEqual(per_check_fail, {})
        self.assertEqual(per_model, {})


def _head_is_exact_tag() -> bool:
    return subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        capture_output=True,
    ).returncode == 0


class IdentityTests(unittest.TestCase):
    @unittest.skipUnless(
        _head_is_exact_tag(),
        "release identity is only defined when HEAD is an exact release tag",
    )
    def test_identity_is_complete_and_self_consistent(self):
        identity = assayer_identity()
        self.assertEqual(identity["version"], "1.1.11")
        self.assertEqual(identity["tag"], "v1.1.11")
        self.assertEqual(len(identity["commit"]), 40)
        self.assertEqual(len(identity["package_tree_hash"]), 64)
        self.assertEqual(identity["schema_version"], "0004")
        self.assertTrue(identity["clean"])
        self.assertIn("Python", identity["python_runtime"])

    @unittest.skipIf(
        _head_is_exact_tag(),
        "gate only refuses when HEAD is not an exact release tag",
    )
    def test_identity_refuses_untagged_head(self):
        with self.assertRaises(subprocess.CalledProcessError):
            assayer_identity()


if __name__ == "__main__":
    unittest.main()

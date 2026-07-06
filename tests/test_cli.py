import unittest
from datetime import datetime, timedelta, timezone

from cli import compute_stats, parse_since


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
                "pipeline": "tarotsmith",
                "verdict": "pass",
                "model": "gpt-4.1-mini",
                "checks_json": {"schema": {"ok": True, "detail": "ok"}},
            },
            {
                "pipeline": "tarotsmith",
                "verdict": "fail",
                "model": None,
                "checks_json": {
                    "schema": {"ok": True, "detail": "ok"},
                    "nonempty:body": {"ok": False, "detail": "too short"},
                },
            },
        ]
        per_pipeline, per_check_fail, per_model = compute_stats(rows)
        self.assertEqual(per_pipeline["tarotsmith"]["pass"], 1)
        self.assertEqual(per_pipeline["tarotsmith"]["fail"], 1)
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


if __name__ == "__main__":
    unittest.main()

import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = REPO_ROOT / "cli.py"


def run_evaluate(request_obj=None, raw_stdin=None, profile="coding-output-v1", extra_args=None):
    args = [sys.executable, str(CLI_PATH), "evaluate"]
    if profile is not None:
        args += ["--profile", profile]
    if extra_args:
        args += extra_args
    stdin_text = raw_stdin if raw_stdin is not None else json.dumps(request_obj)
    proc = subprocess.run(
        args, input=stdin_text, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=30
    )
    return proc


def base_request(**overrides):
    req = {
        "run_id": "run-1",
        "attempt_id": "run-1",
        "job_id": "job-1",
        "contract_hash": "deadbeef",
        "job_type": "coding",
        "backend": "claude-code",
        "model": "",
        "route_decision_id": "",
        "artifact_name": "output",
        "artifact_sha256": "abc123",
        "payload": {"content": "This is a real, sufficiently long piece of generated content."},
        "check_profile": "coding-output-v1",
        "policy_version": "test-policy-v1",
    }
    req.update(overrides)
    return req


class EvaluateCLITests(unittest.TestCase):
    def test_pass_verdict(self):
        proc = run_evaluate(base_request())
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "pass")
        self.assertEqual(verdict["failed_checks"], [])
        self.assertFalse(verdict["had_error"])
        self.assertEqual(verdict["policy_version"], "test-policy-v1")
        self.assertTrue(verdict["trace_id"])
        self.assertEqual(verdict["quarantine_id"], "")
        self.assertTrue(verdict["checks_hash"])

    def test_fail_verdict_missing_required_field(self):
        req = base_request(payload={})
        proc = run_evaluate(req)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "fail")
        self.assertIn("required_fields", verdict["failed_checks"])
        self.assertFalse(verdict["had_error"])

    def test_error_verdict_malformed_json(self):
        proc = run_evaluate(raw_stdin="{not valid json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "error")
        self.assertTrue(verdict["had_error"])
        self.assertIn("malformed JSON", verdict["reason"])

    def test_error_verdict_empty_check_profile(self):
        req = base_request(check_profile="")
        proc = run_evaluate(req, profile=None)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "error")
        self.assertTrue(verdict["had_error"])
        self.assertIn("check_profile", verdict["reason"])

    def test_error_verdict_unknown_profile_from_flag(self):
        proc = run_evaluate(base_request(), profile="no-such-profile-v9")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "error")
        self.assertTrue(verdict["had_error"])

    def test_verdict_determinism_same_input_same_checks_hash(self):
        req = base_request()
        proc1 = run_evaluate(req)
        proc2 = run_evaluate(req)
        v1 = json.loads(proc1.stdout)
        v2 = json.loads(proc2.stdout)
        self.assertEqual(v1["checks_hash"], v2["checks_hash"])
        # trace_id is randomly generated per call and must NOT be part of
        # determinism (it's a per-evaluation id, not a content fingerprint).
        self.assertNotEqual(v1["trace_id"], v2["trace_id"])

    def test_placeholder_content_fails_no_placeholders_check(self):
        req = base_request(payload={"content": "TODO: fill this in with real content [placeholder]"})
        proc = run_evaluate(req)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "fail")
        self.assertIn("no_placeholder:content", verdict["failed_checks"])

    def test_missing_artifact_envelope_fields_fail_artifact_schema(self):
        req = base_request()
        del req["artifact_name"]
        proc = run_evaluate(req)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "fail")
        self.assertIn("artifact_schema", verdict["failed_checks"])

    def test_no_network_no_store_import_touched(self):
        # Regression: evaluate must never construct assayer.store.Store (no
        # network / no Supabase on this synchronous path). If it did, a
        # missing SUPABASE_URL env var plus an actual client() call would
        # raise; here we simply assert the process succeeds without any
        # Supabase env configured, which store.Store()'s lazy client would
        # need if it were touched.
        proc = run_evaluate(base_request())
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("supabase", proc.stderr.lower())


if __name__ == "__main__":
    unittest.main()

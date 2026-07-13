import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI_PATH = REPO_ROOT / "cli.py"

# Must match cli.py's ARTIFACT_PROTOCOL_VERSION exactly (Sol audit finding
# #17 / P1.7). Every test below goes through base_request(), so this is the
# single place that keeps the fixture in sync with the real constant.
ARTIFACT_PROTOCOL_VERSION = "gov-assay-artifact-protocol-v1"


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
        "artifact_declared_path": "output",
        "artifact_stored_path": "",
        "artifact_media_type": "",
        "artifact_language": "",
        "payload": {"content": "This is a real, sufficiently long piece of generated content."},
        "check_profile": "coding-output-v1",
        "policy_version": "test-policy-v1",
        "protocol_version": ARTIFACT_PROTOCOL_VERSION,
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
        # evaluation_id is always a real per-call id; trace_id stays null
        # until a trace row is actually persisted (Sol audit Assayer
        # weakness 4 — this bridge never persists one, it's offline/sync).
        self.assertTrue(verdict["evaluation_id"])
        self.assertIsNone(verdict["trace_id"])
        self.assertEqual(verdict["quarantine_id"], "")
        self.assertTrue(verdict["checks_result_hash"])
        self.assertTrue(verdict["profile_definition_hash"])
        self.assertTrue(verdict["validator_implementation_hash"])
        self.assertTrue(verdict["validator_config_hash"])
        # The four hashes measure different things and must not collide.
        self.assertEqual(
            len({
                verdict["checks_result_hash"],
                verdict["profile_definition_hash"],
                verdict["validator_implementation_hash"],
                verdict["validator_config_hash"],
            }),
            4,
        )

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
        self.assertEqual(v1["checks_result_hash"], v2["checks_result_hash"])
        self.assertEqual(v1["profile_definition_hash"], v2["profile_definition_hash"])
        self.assertEqual(v1["validator_implementation_hash"], v2["validator_implementation_hash"])
        self.assertEqual(v1["validator_config_hash"], v2["validator_config_hash"])
        # evaluation_id is randomly generated per call and must NOT be part
        # of determinism (it's a per-evaluation id, not a content fingerprint).
        self.assertNotEqual(v1["evaluation_id"], v2["evaluation_id"])

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

    def test_v2_rejects_sol_weakness1_reproduction(self):
        # Sol audit Assayer Weakness 1: {"content": "x"} received
        # {"verdict": "pass"} under coding-output-v1. coding-output-v2 must
        # not repeat that: "x" is far under the minimum content length and
        # `language` is entirely missing.
        req = base_request(payload={"content": "x"}, check_profile="coding-output-v2")
        proc = run_evaluate(req, profile="coding-output-v2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "fail")
        self.assertIn("required_fields", verdict["failed_checks"])

    def test_v2_pass_verdict_with_language_and_matching_extension(self):
        # Sol audit finding #17 regression: artifact_name is a logical
        # handle ("code") with no extension at all — file_path_consistency
        # must key off artifact_declared_path, not artifact_name, to see
        # that "output.py" matches the declared python language.
        req = base_request(
            payload={
                "content": "def add(a, b):\n    return a + b\n",
                "language": "python",
            },
            check_profile="coding-output-v2",
            artifact_name="code",
            artifact_declared_path="output.py",
        )
        proc = run_evaluate(req, profile="coding-output-v2")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "pass", verdict)

    def test_v2_fails_on_file_path_language_mismatch(self):
        req = base_request(
            payload={
                "content": "def add(a, b):\n    return a + b\n",
                "language": "python",
            },
            check_profile="coding-output-v2",
            artifact_name="code",
            artifact_declared_path="output.js",
        )
        proc = run_evaluate(req, profile="coding-output-v2")
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "fail")
        self.assertIn("file_path_consistency", verdict["failed_checks"])

    def test_v2_ignores_artifact_name_extension_mismatch(self):
        # The exact bug finding #17 describes: artifact_name alone ("output.js",
        # which would mismatch a declared python language) must no longer be
        # consulted at all once artifact_declared_path correctly says .py.
        req = base_request(
            payload={
                "content": "def add(a, b):\n    return a + b\n",
                "language": "python",
            },
            check_profile="coding-output-v2",
            artifact_name="output.js",
            artifact_declared_path="output.py",
        )
        proc = run_evaluate(req, profile="coding-output-v2")
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "pass", verdict)

    def test_v2_fails_on_syntactically_invalid_python(self):
        req = base_request(
            payload={
                "content": "def add(a, b:\n    return a + b\n",
                "language": "python",
            },
            check_profile="coding-output-v2",
            artifact_name="code",
            artifact_declared_path="output.py",
        )
        proc = run_evaluate(req, profile="coding-output-v2")
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "fail")
        self.assertIn("domain_validator", verdict["failed_checks"])

    def test_v2_passes_unregistered_language_domain_validator(self):
        req = base_request(
            payload={
                "content": "This is a sufficiently long piece of real generated markdown content.",
                "language": "markdown",
            },
            check_profile="coding-output-v2",
            artifact_name="code",
            artifact_declared_path="output.md",
        )
        proc = run_evaluate(req, profile="coding-output-v2")
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "pass", verdict)

    def test_protocol_version_mismatch_fails_closed(self):
        # Sol audit P1.7: a mismatched artifact protocol_version (a stale
        # Governator binary, or one from a future incompatible revision)
        # must fail closed with a distinguishing reason, not silently
        # evaluate against whatever fields happen to be present.
        req = base_request(protocol_version="gov-assay-artifact-protocol-v0-stale")
        proc = run_evaluate(req)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "error")
        self.assertTrue(verdict["had_error"])
        self.assertIn("protocol_version mismatch", verdict["reason"])
        self.assertEqual(verdict["failed_checks"], [])

    def test_protocol_version_missing_fails_closed(self):
        # An old Governator predating this field entirely must fail closed
        # exactly like a mismatched one — not be treated as "compatible by
        # omission".
        req = base_request()
        del req["protocol_version"]
        proc = run_evaluate(req)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "error")
        self.assertIn("protocol_version mismatch", verdict["reason"])

    def test_protocol_version_match_does_not_over_block(self):
        # No-over-blocking companion: the exact matching version must
        # evaluate normally (this is really just base_request()'s default,
        # asserted explicitly so a future accidental default change is
        # caught here too).
        req = base_request()
        self.assertEqual(req["protocol_version"], ARTIFACT_PROTOCOL_VERSION)
        proc = run_evaluate(req)
        verdict = json.loads(proc.stdout)
        self.assertEqual(verdict["verdict"], "pass", verdict)

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

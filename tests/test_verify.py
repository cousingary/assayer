import unittest

from assayer import verify
from assayer.checks import Check


def _ok_check(name):
    return Check(name, lambda item: (True, "ok"))


def _fail_check(name, detail="nope"):
    return Check(name, lambda item: (False, detail))


def _raising_check(name):
    def fn(item):
        raise ValueError("boom")

    return Check(name, fn)


class VerifyTests(unittest.TestCase):
    def test_all_pass(self):
        result = verify({"x": 1}, [_ok_check("a"), _ok_check("b")])
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.failed, [])
        self.assertFalse(result.had_error)
        self.assertTrue(result.checks["a"]["ok"])
        self.assertTrue(result.checks["b"]["ok"])

    def test_one_fail(self):
        result = verify(
            {"x": 1}, [_ok_check("a"), _fail_check("b", "field too short")]
        )
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.failed, ["b"])
        self.assertFalse(result.checks["a"]["ok"] is False)
        self.assertFalse(result.checks["b"]["ok"])
        self.assertEqual(result.checks["b"]["detail"], "field too short")

    def test_check_that_raises_counts_as_failure(self):
        result = verify({"x": 1}, [_ok_check("a"), _raising_check("b")])
        self.assertEqual(result.verdict, "fail")
        self.assertIn("b", result.failed)
        self.assertTrue(result.had_error)
        self.assertIn("check error", result.checks["b"]["detail"])
        self.assertIn("boom", result.checks["b"]["detail"])

    def test_no_checks_means_pass(self):
        result = verify({"x": 1}, [])
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.failed, [])


if __name__ == "__main__":
    unittest.main()

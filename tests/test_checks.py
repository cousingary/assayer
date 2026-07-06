import unittest

from assayer import checks
from tests.fake_store import FakeStore


class SchemaCheckTests(unittest.TestCase):
    def test_pass_when_all_fields_present_and_typed(self):
        check = checks.schema({"title": str, "count": int})
        ok, detail = check({"title": "hello", "count": 3})
        self.assertTrue(ok)

    def test_fail_when_field_missing(self):
        check = checks.schema({"title": str, "count": int})
        ok, detail = check({"title": "hello"})
        self.assertFalse(ok)
        self.assertIn("count", detail)

    def test_fail_when_field_mistyped(self):
        check = checks.schema({"title": str, "count": int})
        ok, detail = check({"title": "hello", "count": "three"})
        self.assertFalse(ok)
        self.assertIn("count", detail)

    def test_name(self):
        check = checks.schema({"title": str})
        self.assertEqual(check.name, "schema")


class NonemptyCheckTests(unittest.TestCase):
    def test_pass(self):
        check = checks.nonempty("body", min_chars=5)
        ok, _ = check({"body": "hello world"})
        self.assertTrue(ok)

    def test_fail_too_short(self):
        check = checks.nonempty("body", min_chars=20)
        ok, _ = check({"body": "short"})
        self.assertFalse(ok)

    def test_fail_not_a_string(self):
        check = checks.nonempty("body")
        ok, _ = check({"body": 123})
        self.assertFalse(ok)

    def test_name(self):
        check = checks.nonempty("body")
        self.assertEqual(check.name, "nonempty:body")


class MaxLenCheckTests(unittest.TestCase):
    def test_pass(self):
        check = checks.max_len("body", max_chars=10)
        ok, _ = check({"body": "short"})
        self.assertTrue(ok)

    def test_fail_too_long(self):
        check = checks.max_len("body", max_chars=5)
        ok, _ = check({"body": "way too long"})
        self.assertFalse(ok)

    def test_name(self):
        check = checks.max_len("body")
        self.assertEqual(check.name, "max_len:body")


class NoBoilerplateCheckTests(unittest.TestCase):
    def test_pass_on_normal_text(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "The market moved sideways today on low volume."})
        self.assertTrue(ok)

    def test_fail_on_prefix_as_an_ai(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "As an AI, I don't have opinions."})
        self.assertFalse(ok)

    def test_fail_on_prefix_case_insensitive(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "SURE, here's your answer."})
        self.assertFalse(ok)

    def test_fail_on_i_cannot(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "I cannot help with that request."})
        self.assertFalse(ok)

    def test_fail_on_here_is(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "Here is the summary you asked for."})
        self.assertFalse(ok)

    def test_fail_on_substring_as_an_ai_language_model(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "Well, as an ai language model I must clarify."})
        self.assertFalse(ok)

    def test_fail_on_fenced_json(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "```json\n{\"a\": 1}\n```"})
        self.assertFalse(ok)

    def test_fail_on_prompt_echo_atomic_insight(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "Return one atomic insight per line."})
        self.assertFalse(ok)

    def test_fail_on_prompt_echo_return_json(self):
        check = checks.no_boilerplate("body")
        ok, _ = check({"body": "Please return json with the fields."})
        self.assertFalse(ok)

    def test_name(self):
        check = checks.no_boilerplate("body")
        self.assertEqual(check.name, "no_boilerplate:body")


class NoPlaceholderCheckTests(unittest.TestCase):
    def test_pass_on_normal_text(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "This is a complete sentence with no gaps."})
        self.assertTrue(ok)

    def test_fail_on_todo_bracket(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "Summary: [TODO fill this in]"})
        self.assertFalse(ok)

    def test_fail_on_todo_colon(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "TODO: write the rest"})
        self.assertFalse(ok)

    def test_fail_on_double_curly(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "Hello {{name}}, welcome"})
        self.assertFalse(ok)

    def test_fail_on_lorem_ipsum(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "Lorem ipsum dolor sit amet"})
        self.assertFalse(ok)

    def test_fail_on_insert(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "[insert client name here]"})
        self.assertFalse(ok)

    def test_fail_on_placeholder_bracket(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "[placeholder text]"})
        self.assertFalse(ok)

    def test_fail_on_xxx_standalone_word(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "Call me at 555-xxx-1234"})
        self.assertFalse(ok)

    def test_pass_when_xxx_only_embedded_in_larger_word(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "The word affixxxes is unusual but is not a placeholder."})
        self.assertTrue(ok)

    def test_fail_on_empty_brackets(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "Result: []"})
        self.assertFalse(ok)

    def test_fail_on_ellipsis_brackets(self):
        check = checks.no_placeholder("body")
        ok, _ = check({"body": "Result: [...]"})
        self.assertFalse(ok)

    def test_name(self):
        check = checks.no_placeholder("body")
        self.assertEqual(check.name, "no_placeholder:body")


class LanguageSaneCheckTests(unittest.TestCase):
    def test_pass_on_normal_prose(self):
        check = checks.language_sane("body")
        ok, _ = check(
            {"body": "The quick brown fox jumps over the lazy dog near the river bank."}
        )
        self.assertTrue(ok)

    def test_fail_on_low_printable_ratio(self):
        check = checks.language_sane("body")
        garbage = "\x01\x02\x03\x04\x05" * 10 + "ok"
        ok, _ = check({"body": garbage})
        self.assertFalse(ok)

    def test_fail_on_degenerate_repetition(self):
        check = checks.language_sane("body")
        text = " ".join(["blah"] * 10)
        ok, detail = check({"body": text})
        self.assertFalse(ok)
        self.assertIn("repetition", detail)

    def test_pass_on_short_text_below_ngram_threshold(self):
        check = checks.language_sane("body")
        ok, _ = check({"body": "hi there"})
        self.assertTrue(ok)

    def test_fail_on_empty_string(self):
        check = checks.language_sane("body")
        ok, _ = check({"body": ""})
        self.assertFalse(ok)

    def test_name(self):
        check = checks.language_sane("body")
        self.assertEqual(check.name, "language_sane:body")


class DedupCheckTests(unittest.TestCase):
    def test_pass_when_not_seen(self):
        store = FakeStore()
        check = checks.dedup(store, "mypipeline", lambda item: item["hash"])
        ok, _ = check({"hash": "abc123"})
        self.assertTrue(ok)

    def test_fail_when_seen(self):
        store = FakeStore()
        store.mark_seen("mypipeline", "abc123")
        check = checks.dedup(store, "mypipeline", lambda item: item["hash"])
        ok, detail = check({"hash": "abc123"})
        self.assertFalse(ok)
        self.assertIn("abc123"[:8], detail)

    def test_fail_open_when_store_raises(self):
        store = FakeStore(raise_on_seen_pass=True)
        check = checks.dedup(store, "mypipeline", lambda item: item["hash"])
        ok, detail = check({"hash": "abc123"})
        self.assertTrue(ok)
        self.assertIn("unavailable", detail)

    def test_name(self):
        store = FakeStore()
        check = checks.dedup(store, "mypipeline", lambda item: item["hash"])
        self.assertEqual(check.name, "dedup")


if __name__ == "__main__":
    unittest.main()

import unittest

from assayer import profiles


class GetProfileTests(unittest.TestCase):
    def test_v1_and_v2_both_registered(self):
        self.assertIsNotNone(profiles.get_profile("coding-output-v1"))
        self.assertIsNotNone(profiles.get_profile("coding-output-v2"))

    def test_unknown_profile_is_none(self):
        self.assertIsNone(profiles.get_profile("no-such-profile"))

    def test_blank_profile_is_none(self):
        self.assertIsNone(profiles.get_profile(""))
        self.assertIsNone(profiles.get_profile(None))


class ProfileVersionTests(unittest.TestCase):
    def test_v1_and_v2_declare_distinct_semver(self):
        v1 = profiles.get_profile("coding-output-v1")
        v2 = profiles.get_profile("coding-output-v2")
        self.assertEqual(v1["version"], "1.0.0")
        # 2.2.0 (Sol redteam v4, P1-12): language_allowlist + stricter
        # domain_validator (require_validator=True) + media_type_consistency.
        self.assertEqual(v2["version"], "2.2.0")

    def test_v1_is_deprecated_advisory_only(self):
        # Sol redteam v4, P1-12: v1 has no language declaration/validation
        # at all, so it can no longer gate blocking production use --
        # downgraded to advisory and flagged deprecated.
        v1 = profiles.get_profile("coding-output-v1")
        self.assertEqual(v1["enforcement"], "advisory")
        self.assertTrue(v1.get("deprecated"))

    def test_v2_is_still_blocking(self):
        v2 = profiles.get_profile("coding-output-v2")
        self.assertEqual(v2["enforcement"], "blocking")

    def test_version_participates_in_profile_definition_hash(self):
        v1 = profiles.get_profile("coding-output-v1")
        h_real = profiles.profile_definition_hash("coding-output-v1", v1)
        bumped = dict(v1, version="9.9.9")
        h_bumped = profiles.profile_definition_hash("coding-output-v1", bumped)
        self.assertNotEqual(h_real, h_bumped)


class ProfileDefinitionHashTests(unittest.TestCase):
    def test_deterministic_for_same_profile(self):
        profile = profiles.get_profile("coding-output-v1")
        h1 = profiles.profile_definition_hash("coding-output-v1", profile)
        h2 = profiles.profile_definition_hash("coding-output-v1", profile)
        self.assertEqual(h1, h2)

    def test_differs_between_v1_and_v2(self):
        v1 = profiles.get_profile("coding-output-v1")
        v2 = profiles.get_profile("coding-output-v2")
        h1 = profiles.profile_definition_hash("coding-output-v1", v1)
        h2 = profiles.profile_definition_hash("coding-output-v2", v2)
        self.assertNotEqual(h1, h2)

    def test_differs_when_name_differs_but_body_same(self):
        profile = profiles.get_profile("coding-output-v1")
        h1 = profiles.profile_definition_hash("coding-output-v1", profile)
        h2 = profiles.profile_definition_hash("some-other-name", profile)
        self.assertNotEqual(h1, h2)

    def test_is_sha256_hex(self):
        profile = profiles.get_profile("coding-output-v1")
        h = profiles.profile_definition_hash("coding-output-v1", profile)
        self.assertEqual(len(h), 64)
        int(h, 16)


class ResolvedCheckConfigsTests(unittest.TestCase):
    def test_fills_in_module_defaults(self):
        # coding-output-v2 declares {"kind": "nonempty", "args": {"field":
        # "content"}} with no explicit min_chars -> resolved config must
        # carry checks_mod's actual default, not an empty/absent value.
        from assayer import checks as checks_mod

        v2 = profiles.get_profile("coding-output-v2")
        configs = profiles.resolved_check_configs(v2)
        nonempty_cfg = next(c for c in configs if c["kind"] == "nonempty")
        self.assertEqual(nonempty_cfg["args"]["min_chars"], checks_mod.NONEMPTY_MIN_CHARS)

    def test_required_fields_type_objects_are_json_safe(self):
        v1 = profiles.get_profile("coding-output-v1")
        configs = profiles.resolved_check_configs(v1)
        schema_cfg = next(c for c in configs if c["name"] == "required_fields")
        self.assertEqual(schema_cfg["args"]["required_fields"], {"content": "str"})

    def test_one_config_per_check(self):
        v2 = profiles.get_profile("coding-output-v2")
        configs = profiles.resolved_check_configs(v2)
        self.assertEqual(len(configs), len(v2["checks"]))


class BuildChecksTests(unittest.TestCase):
    def test_v2_builds_a_live_check_per_spec(self):
        v2 = profiles.get_profile("coding-output-v2")
        live_checks = profiles.build_checks(v2)
        self.assertEqual(len(live_checks), len(v2["checks"]))
        names = [c.name for c in live_checks]
        self.assertIn("required_fields", names)
        self.assertIn("file_path_consistency", names)
        self.assertIn("domain_validator", names)


if __name__ == "__main__":
    unittest.main()

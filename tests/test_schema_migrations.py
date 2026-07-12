"""Offline structural tests for migrations/ + schema.sql. No live Postgres
in this environment (assayer's deploy target is self-hosted Supabase), so
these tests validate migration file shape/ordering/content by parsing SQL
text rather than by applying it to a real database — the same "no DB in
CI" constraint every other test in this suite already lives with (Store is
always FakeStore/mocked, never a live Supabase instance).
"""

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"
SCHEMA_PATH = REPO_ROOT / "schema.sql"

_MIGRATION_NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


class MigrationFileShapeTests(unittest.TestCase):
    def test_migrations_dir_exists(self):
        self.assertTrue(MIGRATIONS_DIR.is_dir())

    def test_migration_files_are_sequentially_numbered_from_0001(self):
        files = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
        self.assertTrue(files, "expected at least one migration file")
        numbers = []
        for name in files:
            match = _MIGRATION_NAME_RE.match(name)
            self.assertIsNotNone(match, f"migration file {name!r} doesn't match NNNN_description.sql")
            numbers.append(int(match.group(1)))
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_baseline_migration_does_not_yet_allow_advisory(self):
        baseline = (MIGRATIONS_DIR / "0001_baseline.sql").read_text()
        verdict_check = _extract_check_values(baseline, "assayer_traces", "verdict")
        self.assertNotIn("advisory", verdict_check)
        self.assertEqual(verdict_check, {"pass", "fail", "error"})


class AdvisoryVerdictMigrationTests(unittest.TestCase):
    def test_migration_0002_widens_verdict_check_to_include_advisory(self):
        migration = (MIGRATIONS_DIR / "0002_advisory_verdict.sql").read_text()
        self.assertIn("assayer_traces", migration)
        self.assertIn("verdict", migration)
        self.assertIn("advisory", migration)
        self.assertIn("DROP CONSTRAINT IF EXISTS", migration)

    def test_migration_0002_is_idempotent_via_if_exists(self):
        migration = (MIGRATIONS_DIR / "0002_advisory_verdict.sql").read_text()
        self.assertIn("IF EXISTS", migration)


class SchemaSqlMatchesMigratedStateTests(unittest.TestCase):
    def test_fresh_install_schema_allows_advisory(self):
        schema = SCHEMA_PATH.read_text()
        verdict_check = _extract_check_values(schema, "assayer_traces", "verdict")
        self.assertEqual(verdict_check, {"pass", "fail", "error", "advisory"})

    def test_fresh_install_schema_is_baseline_plus_migrations(self):
        # The set of values schema.sql declares for a fresh install must
        # equal baseline (0001) unioned with every subsequent migration's
        # additions — never more, never less, so schema.sql and migrations/
        # can't silently drift apart.
        baseline = (MIGRATIONS_DIR / "0001_baseline.sql").read_text()
        baseline_values = _extract_check_values(baseline, "assayer_traces", "verdict")
        migrated_values = set(baseline_values)
        for path in sorted(MIGRATIONS_DIR.glob("*.sql"))[1:]:
            migrated_values |= _extract_check_values(path.read_text(), "assayer_traces", "verdict")

        schema_values = _extract_check_values(SCHEMA_PATH.read_text(), "assayer_traces", "verdict")
        self.assertEqual(schema_values, migrated_values)


def _extract_check_values(sql_text, table, column):
    """Best-effort extraction of the string literals inside a `CHECK
    (<column> IN (...))` constraint anywhere in sql_text that mentions
    `table`. Good enough for this repo's small, hand-written schema files
    (not a general SQL parser)."""
    pattern = re.compile(rf"{column}\s+IN\s*\(([^)]*)\)", re.IGNORECASE)
    match = pattern.search(sql_text)
    if not match:
        return set()
    return {v.strip().strip("'") for v in match.group(1).split(",")}


if __name__ == "__main__":
    unittest.main()

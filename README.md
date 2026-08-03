# assayer

**"Trust the model's output" isn't a QA strategy.** Assayer is the verification and trace layer for unattended LLM pipelines: every generated item gets checked, every verdict gets recorded, every failure gets quarantined durably — so a 3am content run can't silently ship garbage.

Verification + trace layer for unattended LLM pipelines running on a VPS.

## Why you'd run it

For anything where the output goes live without a human in the loop — published articles, generated code, customer-facing copy, data inserts — you need a gate that runs every time and keeps the receipts:

- **Catches bad output before it ships** — schema, nonempty, max length, boilerplate, placeholder, language-sanity, and dedup checks on every item.
- **Never silently drops a failure** — failed items go to a durable SQLite-WAL quarantine outbox with lease/replay semantics; a true double-failure raises instead of swallowing evidence.
- **Keeps an audit trail** — every verdict is traced with item hash, model, pipeline, and job id, plus four separate, independently verifiable hashes (outcome / profile / validator / config).
- **Survives the store being down** — the blocking quarantine outbox replays once the store recovers; optional telemetry is allowed to disappear, blocking evidence never is.

| Without Assayer | With Assayer |
|---|---|
| Garbage ships because nobody looked. | `verify()` runs your checks; non-passing items never insert. |
| A failed item vanishes on a store blip. | The durable outbox queues it; `replay()` claims rows atomically, reverts stale leases, dead-letters after 5 attempts. |
| "Did this run pass?" — no proof. | Traced verdicts + per-item hashes you can re-derive. |
| One combined hash you have to trust wholesale. | Four separate hashes — check the outcome, the profile, the validator, or the config independently. |

## Part of a larger stack

Assayer verifies; it pairs with **[Governator](https://github.com/cousingary/governator)**, which supervises agent runs with OS-enforced containment and a merge gate. Together with [Context Mode](https://github.com/mksglu/context-mode), [RTK](https://github.com/rtk-ai/rtk), and the Ponytail-derived minimalism ruleset, they form one bounded, frugal, recoverable agent stack — each tool owned independently. See Governator's [**The stack: one unit, four allies**](https://github.com/cousingary/governator#the-stack-one-unit-four-allies) section for the full picture.

## Flow
1. Build checks from `assayer.checks` (schema, nonempty, max_len,
   no_boilerplate, no_placeholder, language_sane, dedup).
2. `assayer.verify(item, checks, allow_empty=False)` -> `VerifyResult(verdict, checks, failed, had_error)`.
3. `assayer.trace(store, pipeline=, job_id=, item_hash=, verdict=, checks=,
   model=)` records the outcome. Never raises.
4. On fail, `assayer.quarantine_item(store, pipeline=, item_hash=, payload=,
   input_ref=, reasons=result.failed)` persists it for `assay` review. If the
   primary Store insert fails, it durably queues the row to a local outbox
   (`assayer.outbox.Outbox`, replayable via `Outbox.replay(store)`) instead
   of the old best-effort fallback; it raises `assayer.outbox.OutboxError`
   only if the outbox itself can't be written — quarantine evidence is
   blocking, so a true double-failure is not silently swallowed. `trace()`
   is unchanged (still best-effort, never raises — it's optional telemetry,
   not blocking evidence).
`assayer.store.Store` imports `supabase` lazily (tests need no install).
Deploy target: `/opt/assayer/`.

## Durable quarantine outbox (`assayer.outbox`)

`assayer.outbox.Outbox` is backed by a local SQLite database in WAL mode
(`outbox_entries`, mode `0600`, default path `/opt/assayer/quarantine_outbox.db`
/ `ASSAYER_OUTBOX`), not a JSONL file. This closes Sol redteam v3 finding #6:
the earlier JSONL implementation locked individual reads/appends/rewrites,
but not the whole `replay()` read-modify-rewrite, so an `enqueue()` landing
while `replay()` was blocked inside the backing store got silently dropped
when replay finished and rewrote the file from its stale snapshot.
`Outbox.enqueue()` is a single atomic `INSERT`. `Outbox.replay(store, ...)`
atomically claims a lease on every pending row (`UPDATE ... WHERE
status='pending' ... RETURNING`) before doing any store I/O, so a concurrent
enqueue can never land inside the claim window and two concurrent
`replay()` callers (threads or processes) never claim the same row. A
claimed row that never completes (process killed, hung) reverts to pending
once its lease expires (`Outbox.reclaim_stale_leases()`, called at the top
of every `replay()`) instead of vanishing. Rows that fail
`DEFAULT_MAX_ATTEMPTS` (5) times move to `status='dead_letter'`
(`Outbox.dead_letters()`) instead of retrying forever.

`Outbox(path=...)` auto-migrates a pre-Sol3 JSONL outbox found at the same
path (`Outbox.migrate_from_jsonl()`): the legacy file is moved aside to
`<path>.pre-sqlite-migration` (kept, not deleted) and its lines — plus any
`.deadletter` sibling — are inserted into the new table. Malformed lines are
preserved as dead-lettered `malformed_migration` entries rather than
discarded, and migration is idempotent (safe to run, or auto-trigger,
twice). This durable-outbox path is for blocking quarantine evidence only;
`trace()`'s bare `except: pass` fallback remains scoped to optional
telemetry, which is allowed to disappear silently.

## Schema migrations

`schema.sql` is the fresh-install schema; `migrations/000N_*.sql` are ordered,
idempotent (`DROP CONSTRAINT IF EXISTS` / `IF NOT EXISTS`) deltas applied to an
existing database. `migrations/0002_advisory_verdict.sql` widens
`assayer_traces_verdict_check` to accept `advisory` alongside `pass`/`fail`/
`error`, for a non-blocking verdict a profile can emit without treating it as
a hard failure. `tests/test_schema_migrations.py` asserts the migrated schema
and the fresh-install `schema.sql` agree on the exact accepted verdict set —
run new migrations through that test when adding one.

## Trace and evaluation identifiers

`cli.py evaluate`'s response always includes a fresh `evaluation_id`
(`uuid4`, unique per call — never part of any hash, since two identical
requests must still get distinct ids). `trace_id` is honestly `null`: Phase
3A's synchronous evaluator has no async persistence wired up, so there is no
trace row yet for it to point at. A future caller that persists a trace row
can set `trace_id` once that row exists; until then, treat a non-null
`trace_id` as meaningful and a null one as "not traced," never as an error.

An empty check list is non-passing by default: `verify(item, [])` returns
`no_applicable_checks`. Callers that intentionally want telemetry-only
behavior must opt in with `verify(item, [], allow_empty=True)`.

## Packaging

`pyproject.toml` pins `supabase>=2.0.0` and `python-dotenv>=1.0.0`
(`requirements-lock.txt` records the fully resolved versions actually tested
against), declares package version `1.1.0`, and requires Python `>=3.10`.
Profile semantic versions are independent of the package version and live on
each profile itself (`coding-output-v1` is `1.0.0`, `coding-output-v2` is
`2.0.0` — see `assayer/profiles.py`). `.github/workflows/ci.yml` runs
`pytest -q` from the lockfile across a Python 3.10/3.11/3.12 matrix.

## Check profiles
`assayer.profiles` registers named check-profile bundles for the
Governator<->Assayer `evaluate` bridge: `coding-output-v1` (legacy, kept for
compatibility) and `coding-output-v2` (adds a minimum content length,
language sanity, a `language` field in the payload schema, file/path
consistency between `language` and the artifact's file extension, and a
per-language domain validator registry — see `assayer/checks.py`'s
`file_path_consistency`/`domain_validator`). `cli.py evaluate`'s response
includes four separate hashes: `checks_result_hash` (the outcome),
`profile_definition_hash` (which profile), `validator_implementation_hash`
(which checks.py), and `validator_config_hash` (which resolved check
config) — deliberately not one combined hash, so each can be verified
independently.
## Example wiring
```python
from assayer import item_hash, verify, trace, quarantine_item
from assayer.checks import schema, nonempty, max_len
from assayer.store import Store
store = Store()  # url/key from SUPABASE_URL / SUPABASE_SERVICE_KEY env
item = {"body": llm_output}
h = item_hash(item["body"])
result = verify(item, [schema({"body": str}), nonempty("body"), max_len("body")])
trace(store, pipeline="content_pipeline", job_id=job_id, item_hash=h,
      verdict=result.verdict, checks=result.checks, model="gpt-4.1-mini")
if result.verdict != "pass":
    quarantine_item(store, pipeline="content_pipeline", item_hash=h,
                     payload=item, input_ref=job_id, reasons=result.failed)
```
`assay` CLI (`cli.py`): quarantine/stats, incl. `--brief` for a daily digest.

## Dependency lockfiles

This repo carries two lockfiles with distinct authority:

- **`requirements-lock.txt`** is the release-pinned artifact. The Python-matrix
  venvs (3.10–3.13) used by Governator's release pipeline are built from it,
  and it is the file signed into the source closure.
- **`uv.lock`** is a development convenience for contributors using `uv`. It is
  not part of the release evidence.

`scripts/assayer_verify.sh` (in the Governator repo) asserts the two are
consistent at release time. If they diverge, the release fails.

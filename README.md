# assayer

Verification + trace layer for unattended LLM pipelines running on a VPS.

## Flow
1. Build checks from `assayer.checks` (schema, nonempty, max_len,
   no_boilerplate, no_placeholder, language_sane, dedup).
2. `assayer.verify(item, checks)` -> `VerifyResult(verdict, checks, failed, had_error)`.
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

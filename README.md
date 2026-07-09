# assayer

Verification + trace layer for unattended LLM pipelines running on a VPS.

## Flow
1. Build checks from `assayer.checks` (schema, nonempty, max_len,
   no_boilerplate, no_placeholder, language_sane, dedup).
2. `assayer.verify(item, checks)` -> `VerifyResult(verdict, checks, failed, had_error)`.
3. `assayer.trace(store, pipeline=, job_id=, item_hash=, verdict=, checks=,
   model=)` records the outcome. Never raises.
4. On fail, `assayer.quarantine_item(store, pipeline=, item_hash=, payload=,
   input_ref=, reasons=result.failed)` persists it for `assay` review. Never raises.
`assayer.store.Store` imports `supabase` lazily (tests need no install).
Deploy target: `/opt/assayer/`.
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

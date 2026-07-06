"""assayer: verification + trace layer for unattended LLM pipelines.

Public API: verify, item_hash, trace, quarantine_item.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional


@dataclass
class VerifyResult:
    verdict: str
    checks: dict
    failed: list
    had_error: bool = False


def verify(item: dict, checks: list) -> VerifyResult:
    """Run each check against item, aggregate into a VerifyResult.

    Each check is an object with `.name` (str) and is callable as
    `check(item) -> (ok: bool, detail: str)`. If a check raises, it counts
    as a failure and sets had_error=True on the result.
    """
    results = {}
    failed = []
    had_error = False

    for check in checks:
        name = check.name
        try:
            ok, detail = check(item)
        except Exception as exc:
            ok = False
            detail = f"check error: {exc}"
            had_error = True

        results[name] = {"ok": bool(ok), "detail": detail}
        if not ok:
            failed.append(name)

    verdict = "pass" if not failed else "fail"

    return VerifyResult(verdict=verdict, checks=results, failed=failed, had_error=had_error)


def item_hash(text: str) -> str:
    """sha256 hexdigest of the UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trace(
    store,
    *,
    pipeline,
    job_id,
    item_hash,
    verdict,
    checks,
    model=None,
    tokens_in=None,
    tokens_out=None,
    cost_usd=None,
    quarantine_id=None,
    duration_ms=None,
) -> None:
    """Build a trace row and persist it. Never raises."""
    row = {
        "ts": _utc_now_iso(),
        "pipeline": pipeline,
        "job_id": job_id,
        "item_hash": item_hash,
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": cost_usd,
        "verdict": verdict,
        "checks_json": checks,
        "quarantine_id": quarantine_id,
        "duration_ms": duration_ms,
    }

    try:
        store.insert_trace(row)
    except Exception:
        try:
            fallback = getattr(store, "fallback", None)
            if fallback is not None:
                fallback(row)
        except Exception:
            pass


def quarantine_item(store, *, pipeline, item_hash, payload, input_ref, reasons) -> Optional[int]:
    """Build a quarantine row, persist it, return the new row id or None."""
    row = {
        "ts": _utc_now_iso(),
        "pipeline": pipeline,
        "item_hash": item_hash,
        "payload_json": payload,
        "input_ref": input_ref,
        "reasons_json": reasons,
        "status": "open",
    }

    try:
        return store.insert_quarantine(row)
    except Exception:
        try:
            fallback = getattr(store, "fallback", None)
            if fallback is not None:
                fallback(row)
        except Exception:
            pass
        return None

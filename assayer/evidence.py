"""Sensitive-evidence handling for quarantine/trace payloads.

Sol redteam v4, P1-13: quarantine/trace payloads can hold source code,
credentials, user data, prompts, and paths -- exactly the artifact this
package exists to preserve for later human review, which means it is also
the thing most likely to accidentally retain a leaked secret indefinitely.
This module is deliberately *not* a full DLP engine: it is a pragmatic,
deterministic first line of defense (field-level redaction by key name and a
handful of well-known secret shapes), a payload size cap, optional
authenticated encryption at rest, and a retention/deletion-audit helper --
each independently testable and each off-by-default-safe for content that
doesn't match anything.
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone

# TUNABLE: substrings matched against dict KEY names (case-insensitive). A
# key containing any of these has its entire value redacted, regardless of
# type or shape -- the point is never having to guess what a "password"
# field's value might look like.
SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "ssh_key",
    "authorization",
    "auth_token",
    "bearer",
    "access_key",
    "client_secret",
)

# TUNABLE: well-known secret shapes matched against string VALUES
# (regardless of which key they live under) -- catches a secret embedded in
# free text (e.g. pasted into `content`), not just one sitting in an
# obviously-named field.
SENSITIVE_VALUE_PATTERNS = (
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer_token", re.compile(r"[Bb]earer\s+[A-Za-z0-9\-_.=]{10,}")),
    ("openai_style_api_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("pem_private_key", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z]*PRIVATE KEY-----")),
)

# TUNABLE
MAX_PAYLOAD_BYTES = 262144  # 256 KiB

_EVIDENCE_KEY_ENV = "ASSAYER_EVIDENCE_KEY"
_EVIDENCE_KEYS_ENV = "ASSAYER_EVIDENCE_KEYS"
_EVIDENCE_KEY_ID_ENV = "ASSAYER_EVIDENCE_KEY_ID"
DEFAULT_EVIDENCE_KEY_ID = "default"
DEFAULT_DELETION_AUDIT_PATH = "/opt/assayer/deletion_audit.jsonl"


def _key_is_sensitive(key):
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEY_SUBSTRINGS)


def _redact_value_patterns(value):
    changed = False

    def _sub(name):
        def fn(match):
            nonlocal changed
            changed = True
            return f"[REDACTED:{name}]"

        return fn

    for name, pattern in SENSITIVE_VALUE_PATTERNS:
        value = pattern.sub(_sub(name), value)
    return value, changed


def classify_and_redact(value):
    """Recursively walk `value`, masking dict entries whose KEY name looks
    sensitive and string values that contain a recognized secret shape.
    Returns (redacted_value, found_sensitive: bool). A value with nothing
    sensitive in it is returned equal to the input (safe to compare with
    `==` -- existing callers that don't carry secrets see no change)."""
    found = False

    if isinstance(value, dict):
        new_dict = {}
        for k, v in value.items():
            if _key_is_sensitive(k):
                new_dict[k] = f"[REDACTED:key:{k}]"
                found = True
            else:
                new_dict[k], child_found = classify_and_redact(v)
                found = found or child_found
        return new_dict, found

    if isinstance(value, list):
        new_list = []
        for item in value:
            redacted_item, child_found = classify_and_redact(item)
            new_list.append(redacted_item)
            found = found or child_found
        return new_list, found

    if isinstance(value, tuple):
        redacted_tuple, child_found = classify_and_redact(list(value))
        return tuple(redacted_tuple), child_found

    if isinstance(value, str):
        redacted, changed = _redact_value_patterns(value)
        return redacted, changed

    return value, False


def classification_for(found_sensitive):
    return "sensitive" if found_sensitive else "standard"


def enforce_size_limit(value, max_bytes=MAX_PAYLOAD_BYTES):
    """Cap the serialized size of `value`. An oversized payload is replaced
    entirely by a small marker (original size, a truncated preview) rather
    than selectively trimming individual fields -- simple and deterministic,
    at the cost of losing the untruncated fields entirely; that tradeoff is
    intentional; evidence too large to store in full is itself the anomaly
    worth recording. Returns (value_or_marker, was_truncated: bool)."""
    serialized = json.dumps(value, default=str)
    size = len(serialized.encode("utf-8"))
    if size <= max_bytes:
        return value, False

    preview_bytes = serialized.encode("utf-8")[:max_bytes]
    return {
        "_size_limit_exceeded": True,
        "_original_size_bytes": size,
        "_max_bytes": max_bytes,
        "_truncated_preview": preview_bytes.decode("utf-8", errors="ignore"),
    }, True


def encryption_configured():
    return bool(os.environ.get(_EVIDENCE_KEY_ENV) or os.environ.get(_EVIDENCE_KEYS_ENV))


def _configured_keys():
    keys_json = os.environ.get(_EVIDENCE_KEYS_ENV)
    if keys_json:
        keys = json.loads(keys_json)
        if not isinstance(keys, dict) or not keys:
            raise RuntimeError(f"{_EVIDENCE_KEYS_ENV} must be a non-empty JSON object")
        return {str(k): str(v) for k, v in keys.items()}
    legacy = os.environ.get(_EVIDENCE_KEY_ENV)
    if legacy:
        return {os.environ.get(_EVIDENCE_KEY_ID_ENV, DEFAULT_EVIDENCE_KEY_ID): legacy}
    return {}


def _active_key():
    keys = _configured_keys()
    if not keys:
        return None, None
    key_id = os.environ.get(_EVIDENCE_KEY_ID_ENV) or next(iter(keys))
    if key_id not in keys:
        raise RuntimeError(f"active evidence key_id {key_id!r} is not configured")
    return key_id, keys[key_id]


def encrypt_if_configured(value):
    """Optional authenticated encryption at rest with key identity metadata.

    Opt in via ASSAYER_EVIDENCE_KEYS='{"key-id":"fernet-key"}' plus
    ASSAYER_EVIDENCE_KEY_ID, or the legacy ASSAYER_EVIDENCE_KEY. Returns
    (stored_repr, encrypted). When encrypted, stored_repr is an envelope with
    key_id, algorithm, format_version, nonce field, and ciphertext.
    """
    key_id, key = _active_key()
    if not key:
        return value, False
    from cryptography.fernet import Fernet

    fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    token = fernet.encrypt(json.dumps(value, default=str).encode("utf-8"))
    return {
        "format_version": 1,
        "algorithm": "fernet",
        "key_id": key_id,
        "nonce": None,
        "ciphertext": token.decode("utf-8"),
        "auth_metadata": {"serializer": "json"},
    }, True


def decrypt_if_configured(stored, encrypted):
    if not encrypted:
        return stored
    keys = _configured_keys()
    if not keys:
        raise RuntimeError(
            f"no evidence decryption keys are set but this row is marked payload_encrypted=true"
        )
    from cryptography.fernet import Fernet

    if isinstance(stored, dict):
        if stored.get("algorithm") != "fernet" or stored.get("format_version") != 1:
            raise RuntimeError("unsupported encrypted evidence envelope")
        key_id = stored.get("key_id")
        if key_id not in keys:
            raise RuntimeError(f"evidence key_id {key_id!r} is unavailable")
        key = keys[key_id]
        token = stored["ciphertext"]
    else:
        # Backward compatibility for pre-S9 rows that stored only the token.
        key_id, key = _active_key()
        if not key:
            raise RuntimeError("legacy encrypted evidence has no active key available")
        token = stored
    fernet = Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    plaintext = fernet.decrypt(token.encode("utf-8") if isinstance(token, str) else token)
    return json.loads(plaintext)


def store_representation_for_classification(value, classification):
    """Return (stored, encrypted) and require encryption for classified evidence."""
    stored, encrypted = encrypt_if_configured(value)
    if classification in ("classified", "sensitive") and not encrypted:
        raise RuntimeError(f"{classification} evidence requires configured encryption")
    return stored, encrypted


def _append_audit(path, record):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
        os.chmod(parent, 0o700)
    with open(path, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")
    os.chmod(path, 0o600)


def purge_expired_quarantine(store, retention_days, statuses=("released", "discarded"), now=None, audit_path=None):
    """Delete quarantine rows in a terminal status (never `open` -- rows
    still awaiting human review are not subject to retention) older than
    `retention_days`, and durably record what was deleted (Sol redteam v4,
    P1-13: "retention limits" + "deletion auditing"). Returns the audit
    record that was written."""
    now = now or datetime.now(timezone.utc)
    cutoff_iso = (now - timedelta(days=retention_days)).isoformat()

    if hasattr(store, "delete_expired_quarantine"):
        expired_ids = store.delete_expired_quarantine(statuses, cutoff_iso) or []
    else:
        expired_ids = []
        for status in statuses:
            for row in store.list_quarantine(status=status) or []:
                ts = row.get("ts") or ""
                if ts and ts < cutoff_iso:
                    expired_ids.append(row["id"])
        if expired_ids:
            store.delete_quarantine(expired_ids)

    record = {
        "executed_at": now.isoformat(),
        "cutoff_iso": cutoff_iso,
        "statuses": list(statuses),
        "deleted_ids": expired_ids,
        "count": len(expired_ids),
    }
    _append_audit(audit_path or os.environ.get("ASSAYER_DELETION_AUDIT", DEFAULT_DELETION_AUDIT_PATH), record)
    return record

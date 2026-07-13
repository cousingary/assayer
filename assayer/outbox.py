"""Durable local outbox for blocking evidence that must never be silently
lost.

Sol audit Assayer weakness 5 ("persistence fallback can disappear
silently"): the old `Store.fallback()` wrote a best-effort JSONL line
inside a bare `except Exception: pass`, so a primary-insert failure
followed by a fallback-write failure (disk full, bad permissions, ...)
vanished without a trace. That is acceptable for optional telemetry
(trace()) but not for quarantine evidence, which is the one artifact that
lets a human recover a pipeline's rejected output later — losing it is
losing the item.

Sol redteam v3, finding #6 ("Assayer's durable outbox can lose concurrent
entries"): the first-generation fix above used a JSONL file protected by
`fcntl` locks around individual reads/appends/rewrites, but not around the
whole replay read-modify-rewrite. An `enqueue()` that lands *while*
`replay()` is mid-flight (blocked inside the backing store) gets silently
dropped when replay finishes and rewrites the file from its stale
in-memory snapshot. This module replaces that file with a SQLite WAL-backed
table so enqueue and claim/complete are each a single atomic statement —
there is no read-modify-write window for a concurrent writer to fall into.
"""

import json
import os
import sqlite3
import time
import uuid

# TUNABLE
DEFAULT_OUTBOX_PATH = "/opt/assayer/quarantine_outbox.db"
# TUNABLE: after this many failed replay attempts, an entry is dead-lettered
# instead of retried again.
DEFAULT_MAX_ATTEMPTS = 5
# TUNABLE: how long a claimed entry is leased to one worker before another
# worker's stale-lease recovery is allowed to reclaim it.
DEFAULT_LEASE_SECONDS = 60

_SQLITE_MAGIC = b"SQLite format 3\x00"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_entries (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    table_hint TEXT NOT NULL,
    payload TEXT NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outbox_status_created
    ON outbox_entries (status, created_at);
"""


class OutboxError(Exception):
    """Raised when an entry cannot even be durably queued (e.g. disk full,
    permission denied). Deliberately loud: a caller catching this must treat
    it as evidence loss, never swallow it silently."""


class Outbox:
    def __init__(self, path=None, max_attempts=DEFAULT_MAX_ATTEMPTS):
        self.path = path or os.environ.get("ASSAYER_OUTBOX", DEFAULT_OUTBOX_PATH)
        self.max_attempts = max_attempts
        self._conn = None
        try:
            self._ensure_parent(self.path)
            legacy = self._detach_legacy_jsonl_if_present()
            self._conn = self._connect()
            self._secure_perms()
            if legacy is not None:
                legacy_active, legacy_dead = legacy
                self.migrate_from_jsonl(legacy_active, legacy_dead)
        except (sqlite3.Error, OSError) as exc:
            raise OutboxError(f"failed to open durable outbox at {self.path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, table_hint, row):
        """Durably persist one entry and return its outbox id. Raises
        OutboxError if the entry cannot be written at all."""
        entry_id = str(uuid.uuid4())
        now = time.time()
        try:
            with self._conn:
                self._conn.execute(
                    """INSERT INTO outbox_entries
                       (id, status, attempts, table_hint, payload, created_at, updated_at)
                       VALUES (?, 'pending', 0, ?, ?, ?, ?)""",
                    (entry_id, table_hint, json.dumps(row, default=str), now, now),
                )
        except sqlite3.Error as exc:
            raise OutboxError(f"failed to durably queue outbox entry: {exc}") from exc
        return entry_id

    def pending(self):
        return self._read_status("pending")

    def dead_letters(self):
        return self._read_status("dead_letter")

    def replay(self, store, lease_seconds=DEFAULT_LEASE_SECONDS, worker_id=None, batch_size=200):
        """Claim every currently-pending entry (atomically, so a concurrent
        enqueue() can never land inside the claim window) and attempt to
        persist each to `store`. Entries that succeed are deleted; entries
        that fail are released back to pending with an incremented attempt
        count, or dead-lettered once max_attempts is exceeded.

        Returns (n_replayed, n_still_pending, n_dead_lettered)."""
        worker_id = worker_id or f"pid{os.getpid()}-{uuid.uuid4()}"
        now = time.time()
        self.reclaim_stale_leases(now=now)
        claimed = self._claim_batch(worker_id, now + lease_seconds, batch_size)

        replayed = 0
        dead_lettered = 0
        for entry in claimed:
            try:
                if entry["table_hint"] == "assayer_quarantine":
                    store.insert_quarantine(entry["row"])
                else:
                    store.insert_trace(entry["row"])
            except Exception as exc:
                status = self._complete_failure(entry["id"], worker_id, entry["attempts"], str(exc))
                if status == "dead_letter":
                    dead_lettered += 1
            else:
                self._complete_success(entry["id"], worker_id)
                replayed += 1

        still_pending = self._count_status("pending")
        return replayed, still_pending, dead_lettered

    def reclaim_stale_leases(self, now=None):
        """Return any entry whose lease has expired (its worker crashed,
        was killed, or hung) back to pending so another replay() can claim
        it. Safe to call at any time, including concurrently."""
        now = time.time() if now is None else now
        with self._conn:
            self._conn.execute(
                """UPDATE outbox_entries
                   SET status='pending', lease_owner=NULL, lease_until=NULL, updated_at=?
                   WHERE status='processing' AND lease_until IS NOT NULL AND lease_until < ?""",
                (now, now),
            )

    def migrate_from_jsonl(self, jsonl_path, deadletter_path=None):
        """Idempotent migration of the pre-Sol3 JSONL outbox into this
        table. Safe to call twice (or on a path with no legacy file at
        all): rows are keyed so a repeat run inserts nothing new. Malformed
        lines are preserved as dead-lettered entries rather than dropped."""
        n_migrated = 0
        if jsonl_path and os.path.exists(jsonl_path):
            n_migrated += self._migrate_jsonl_file(jsonl_path, default_status="pending")
        if deadletter_path and os.path.exists(deadletter_path):
            n_migrated += self._migrate_jsonl_file(deadletter_path, default_status="dead_letter")
        return n_migrated

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(_SCHEMA)
        return conn

    def _secure_perms(self):
        for suffix in ("", "-wal", "-shm"):
            candidate = self.path + suffix
            if os.path.exists(candidate):
                os.chmod(candidate, 0o600)

    def _ensure_parent(self, path):
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _is_sqlite_file(self, path):
        try:
            with open(path, "rb") as f:
                return f.read(len(_SQLITE_MAGIC)) == _SQLITE_MAGIC
        except OSError:
            return False

    def _detach_legacy_jsonl_if_present(self):
        """If `self.path` currently holds pre-Sol3 JSONL content (an
        operator upgraded the code without moving ASSAYER_OUTBOX), move it
        aside so a fresh SQLite database can be created at the same path,
        and return the moved-aside paths for migrate_from_jsonl(). Returns
        None if there is nothing to migrate."""
        if not os.path.exists(self.path):
            return None
        if self._is_sqlite_file(self.path):
            return None
        legacy_active = self.path + ".pre-sqlite-migration"
        if not os.path.exists(legacy_active):
            os.replace(self.path, legacy_active)
        legacy_dead = self.path + ".deadletter"
        return (legacy_active, legacy_dead if os.path.exists(legacy_dead) else None)

    def _migrate_jsonl_file(self, path, default_status):
        n = 0
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                n += self._migrate_jsonl_line(line, default_status)
        return n

    def _migrate_jsonl_line(self, line, default_status):
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict):
                raise ValueError("legacy outbox line is not a JSON object")
            entry_id = entry.get("id") or str(uuid.uuid5(uuid.NAMESPACE_URL, line))
            table_hint = entry["table_hint"]
            row = entry["row"]
            attempts = int(entry.get("attempts", 0))
            status = entry.get("status") or default_status
            if status not in ("pending", "dead_letter"):
                status = default_status
            created_at = float(entry.get("queued_at", time.time()))
            payload = json.dumps(row, default=str)
            last_error = None
        except Exception as exc:
            # Malformed line: preserved, never silently discarded (this is
            # the exact "malformed JSON lines are silently discarded"
            # weakness the audit calls out).
            entry_id = str(uuid.uuid5(uuid.NAMESPACE_URL, line))
            table_hint = "malformed_migration"
            payload = json.dumps({"raw_line": line})
            attempts = 0
            status = "dead_letter"
            created_at = time.time()
            last_error = f"malformed jsonl line during migration: {exc}"

        now = time.time()
        with self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO outbox_entries
                   (id, status, attempts, table_hint, payload, last_error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry_id, status, attempts, table_hint, payload, last_error, created_at, now),
            )
        return 1

    def _claim_batch(self, worker_id, lease_until_ts, batch_size):
        now = time.time()
        with self._conn:
            rows = self._conn.execute(
                """UPDATE outbox_entries
                   SET status='processing', lease_owner=?, lease_until=?, updated_at=?
                   WHERE id IN (
                       SELECT id FROM outbox_entries
                       WHERE status='pending'
                       ORDER BY created_at
                       LIMIT ?
                   )
                   RETURNING id, table_hint, payload, attempts""",
                (worker_id, lease_until_ts, now, batch_size),
            ).fetchall()
        return [
            {"id": r[0], "table_hint": r[1], "row": json.loads(r[2]), "attempts": r[3]}
            for r in rows
        ]

    def _complete_success(self, entry_id, worker_id):
        with self._conn:
            self._conn.execute(
                "DELETE FROM outbox_entries WHERE id=? AND lease_owner=?",
                (entry_id, worker_id),
            )

    def _complete_failure(self, entry_id, worker_id, attempts, error):
        new_attempts = attempts + 1
        status = "dead_letter" if new_attempts >= self.max_attempts else "pending"
        now = time.time()
        with self._conn:
            self._conn.execute(
                """UPDATE outbox_entries
                   SET status=?, attempts=?, lease_owner=NULL, lease_until=NULL,
                       last_error=?, updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (status, new_attempts, error, now, entry_id, worker_id),
            )
        return status

    def _count_status(self, status):
        row = self._conn.execute(
            "SELECT COUNT(*) FROM outbox_entries WHERE status=?", (status,)
        ).fetchone()
        return row[0]

    def _read_status(self, status):
        rows = self._conn.execute(
            """SELECT id, table_hint, payload, attempts, status, created_at
               FROM outbox_entries WHERE status=? ORDER BY created_at""",
            (status,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "table_hint": r[1],
                "row": json.loads(r[2]),
                "attempts": r[3],
                "status": r[4],
                "queued_at": r[5],
            }
            for r in rows
        ]

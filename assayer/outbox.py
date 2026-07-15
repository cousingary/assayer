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

Sol redteam v4, P0-8 ("duplicate outbox processing"): the WAL-backed table
above still let a stale-but-not-yet-reclaimed lease and a later reclaim
race a real downstream insert -- a batch's claimed rows all shared one
lease_until set at claim time (so a slow batch could expire mid-way through
its own processing, not just on crash), and a crashed worker's downstream
insert that actually succeeded before it died would be silently reprocessed
by whoever reclaimed the row. Report §9 attacks 13/14. Fixed here by:
every entry carries a stable `event_id` (the outbox row's own id) embedded
into the row payload once at enqueue() time, so every replay attempt of the
same entry submits the identical idempotency key -- `store.py`'s
insert_quarantine/insert_trace upsert on it and tolerate "already inserted"
as success rather than creating a duplicate row. The lease is renewed
per-entry immediately before it is submitted (not just once for the whole
claimed batch), and a completion only counts if the affected-row check
proves this worker still owned the lease at that moment. Every claim AND
every stale-lease reclaim increments `attempts` (previously only an
explicit replay failure did), so a worker that repeatedly crashes before
ever raising still burns down to dead-letter instead of looping forever.
"""

import json
import os
import sqlite3
import threading
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
KNOWN_TABLE_HINTS = {"assayer_quarantine", "assayer_traces"}
UNKNOWN_OUTBOX_OPERATION = "UNKNOWN_OUTBOX_OPERATION"

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
        # Sol redteam v4, P0-8: one sqlite3.Connection is not safe to share
        # across threads without external serialization. Each thread that
        # touches this Outbox instance gets its own connection to the same
        # WAL-mode file instead -- SQLite's own file locking (already relied
        # on by the multi-Outbox-instance concurrency tests) is what
        # actually serializes conflicting writes, not the Connection object.
        self._local = threading.local()
        try:
            self._ensure_parent(self.path)
            legacy = self._detach_legacy_jsonl_if_present()
            self._local.conn = self._connect()
            self._secure_perms()
            if legacy is not None:
                legacy_active, legacy_dead = legacy
                self.migrate_from_jsonl(legacy_active, legacy_dead)
        except (sqlite3.Error, OSError) as exc:
            raise OutboxError(f"failed to open durable outbox at {self.path}: {exc}") from exc

    @property
    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, table_hint, row):
        """Durably persist one entry and return its outbox id. Raises
        OutboxError if the entry cannot be written at all."""
        entry_id = str(uuid.uuid4())
        now = time.time()
        # Sol redteam v4, P0-8: event_id is this entry's stable idempotency
        # key. It never changes across retries/reclaims, so every attempt
        # to persist this entry submits the identical key -- the target
        # store's unique constraint on event_id is what actually makes a
        # duplicate downstream insert (report §9 attacks 13/14) a no-op
        # instead of a second row. setdefault: a caller that already
        # assigned its own event_id (e.g. to share identity with a prior
        # direct-insert attempt) is respected, never overwritten.
        row = dict(row)
        row.setdefault("event_id", entry_id)
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
        # Self-heal before reading: a "processing" row whose lease already
        # expired is effectively pending (nobody is actually working it),
        # but would otherwise stay invisible to pending() -- an operator
        # asking "what's actionable right now" must not undercount a
        # crashed worker's abandoned entry just because nobody has called
        # replay() since it died.
        self.reclaim_stale_leases()
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
        claimed, dead_lettered = self._claim_batch(worker_id, now + lease_seconds, batch_size)

        replayed = 0
        for entry in claimed:
            if entry["table_hint"] not in KNOWN_TABLE_HINTS:
                if self._dead_letter_unknown_operation(entry["id"], worker_id, entry["table_hint"]):
                    dead_lettered += 1
                continue

            if not self._renew_lease(entry["id"], worker_id, time.time() + lease_seconds):
                continue
            heartbeat = self._start_lease_heartbeat(entry["id"], worker_id, lease_seconds)
            try:
                if entry["table_hint"] == "assayer_quarantine":
                    store.insert_quarantine(entry["row"])
                elif entry["table_hint"] == "assayer_traces":
                    store.insert_trace(entry["row"])
                else:
                    raise RuntimeError(f"{UNKNOWN_OUTBOX_OPERATION}: {entry['table_hint']}")
            except Exception as exc:
                status = self._complete_failure(entry["id"], worker_id, entry["attempts"], str(exc))
                if status == "dead_letter":
                    dead_lettered += 1
            else:
                # Sol redteam v4, P0-8: only count this as replayed if the
                # completion actually affected a row this worker still
                # owned the lease on. If it didn't (stolen between the
                # renewal above and this delete -- an extremely tight
                # window), don't report success for a row we no longer
                # provably finished: the store-side event_id uniqueness
                # constraint is what keeps the downstream data itself
                # correct even in that race; this counter just avoids
                # double-claiming credit for it.
                if self._complete_success(entry["id"], worker_id):
                    replayed += 1
            finally:
                heartbeat.stop()

        still_pending = self._count_status("pending")
        return replayed, still_pending, dead_lettered

    def reclaim_stale_leases(self, now=None):
        """Return any entry whose lease has expired (its worker crashed,
        was killed, or hung) back to pending so another replay() can claim
        it -- or straight to dead_letter if this stale lease is itself the
        entry's max_attempts'th attempt (Sol redteam v4, P0-8: a worker that
        crashes on every single claim, before ever raising an explicit
        failure, must still burn down to dead-letter instead of being
        reclaimed forever). Safe to call at any time, including
        concurrently."""
        now = time.time() if now is None else now
        with self._conn:
            self._conn.execute(
                """UPDATE outbox_entries
                   SET status=CASE WHEN attempts >= ? THEN 'dead_letter' ELSE 'pending' END,
                       last_error=CASE WHEN attempts >= ? THEN 'lease expired (stale worker)' ELSE last_error END,
                       lease_owner=NULL, lease_until=NULL, updated_at=?
                   WHERE status='processing' AND lease_until IS NOT NULL AND lease_until < ?""",
                (self.max_attempts, self.max_attempts, now, now),
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
        # check_same_thread=True (the sqlite3 default): each thread now gets
        # its own Connection via the _conn property, so nothing should ever
        # reuse this object cross-thread -- if something does, a loud
        # ProgrammingError beats silent corruption.
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
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
            # Sol redteam v4, P0-8: os.makedirs' mode is masked by the
            # process umask, so the actual resulting permissions are
            # whatever the caller's environment happens to have set --
            # quarantine evidence's parent directory should not depend on
            # that. chmod explicitly rather than trusting umask.
            os.makedirs(parent, exist_ok=True)
            os.chmod(parent, 0o700)

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
        # Sol redteam v4, P0-8: attempts increments here, on every claim --
        # not only on an explicit replay failure. A worker that crashes
        # before ever reaching the try/except in replay() (e.g. dies right
        # after being claimed) still burns down toward max_attempts via
        # reclaim_stale_leases' dead-letter check, instead of being reclaimed
        # and reprocessed forever.
        now = time.time()
        with self._conn:
            rows = self._conn.execute(
                """UPDATE outbox_entries
                   SET status='processing', lease_owner=?, lease_until=?,
                       attempts=attempts+1, updated_at=?
                   WHERE id IN (
                       SELECT id FROM outbox_entries
                       WHERE status='pending'
                       ORDER BY created_at
                       LIMIT ?
                   )
                   RETURNING id, table_hint, payload, attempts""",
                (worker_id, lease_until_ts, now, batch_size),
            ).fetchall()

        claimed = []
        unparseable = 0
        for r in rows:
            entry_id, table_hint, payload, attempts = r[0], r[1], r[2], r[3]
            try:
                row = json.loads(payload)
            except (ValueError, TypeError) as exc:
                # Sol redteam v4, P0-8: a malformed payload must not poison
                # the whole claimed batch -- it will never parse
                # successfully on retry, so dead-letter this one entry
                # immediately and keep going with the rest of the batch.
                self._dead_letter_unparseable(entry_id, worker_id, str(exc))
                unparseable += 1
                continue
            claimed.append({"id": entry_id, "table_hint": table_hint, "row": row, "attempts": attempts})
        return claimed, unparseable

    def _dead_letter_unparseable(self, entry_id, worker_id, error):
        with self._conn:
            self._conn.execute(
                """UPDATE outbox_entries
                   SET status='dead_letter', lease_owner=NULL, lease_until=NULL,
                       last_error=?, updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (f"unparseable payload: {error}", time.time(), entry_id, worker_id),
            )

    def _start_lease_heartbeat(self, entry_id, worker_id, lease_seconds):
        """Renew a processing lease for the full downstream call.

        S9/P1-4: a single pre-call renewal is not sufficient when a target
        operation outlives the lease. The heartbeat prevents another worker
        from reclaiming and executing the same outbox operation mid-call.
        """

        class Heartbeat:
            def __init__(self, outer):
                self._outer = outer
                self._stop = threading.Event()
                self.lost = threading.Event()
                self._interval = max(0.05, min(lease_seconds / 3.0, 5.0))
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

            def _run(self):
                while not self._stop.wait(self._interval):
                    ok = self._outer._renew_lease(entry_id, worker_id, time.time() + lease_seconds)
                    if not ok:
                        self.lost.set()
                        return

            def stop(self):
                self._stop.set()
                self._thread.join(timeout=max(1.0, self._interval * 2))

        return Heartbeat(self)

    def _dead_letter_unknown_operation(self, entry_id, worker_id, table_hint):
        with self._conn:
            cur = self._conn.execute(
                """UPDATE outbox_entries
                   SET status='dead_letter', lease_owner=NULL, lease_until=NULL,
                       last_error=?, updated_at=?
                   WHERE id=? AND lease_owner=? AND status='processing'""",
                (f"{UNKNOWN_OUTBOX_OPERATION}: {table_hint}", time.time(), entry_id, worker_id),
            )
        return cur.rowcount > 0

    def _renew_lease(self, entry_id, worker_id, lease_until_ts):
        with self._conn:
            cur = self._conn.execute(
                """UPDATE outbox_entries SET lease_until=?, updated_at=?
                   WHERE id=? AND lease_owner=? AND status='processing'""",
                (lease_until_ts, time.time(), entry_id, worker_id),
            )
        return cur.rowcount > 0

    def _complete_success(self, entry_id, worker_id):
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM outbox_entries WHERE id=? AND lease_owner=?",
                (entry_id, worker_id),
            )
        # Sol redteam v4, P0-8: never report success on an update that
        # affected zero rows -- rowcount 0 means the lease was already lost
        # (stolen/reclaimed) before this delete landed, so this worker
        # cannot prove it was the one that actually finished the row.
        return cur.rowcount > 0

    def _complete_failure(self, entry_id, worker_id, attempts, error):
        # attempts was already incremented once by _claim_batch for this
        # very attempt -- do not increment again here, or one attempt would
        # count twice.
        status = "dead_letter" if attempts >= self.max_attempts else "pending"
        now = time.time()
        with self._conn:
            self._conn.execute(
                """UPDATE outbox_entries
                   SET status=?, lease_owner=NULL, lease_until=NULL,
                       last_error=?, updated_at=?
                   WHERE id=? AND lease_owner=?""",
                (status, error, now, entry_id, worker_id),
            )
        return status

    def _count_status(self, status):
        row = self._conn.execute(
            "SELECT COUNT(*) FROM outbox_entries WHERE status=?", (status,)
        ).fetchone()
        return row[0]

    def _read_status(self, status):
        rows = self._conn.execute(
            """SELECT id, table_hint, payload, attempts, status, created_at, last_error
               FROM outbox_entries WHERE status=? ORDER BY created_at""",
            (status,),
        ).fetchall()
        entries = []
        for r in rows:
            try:
                row = json.loads(r[2])
            except (ValueError, TypeError):
                # A malformed payload must not poison listing either (Sol
                # redteam v4, P0-8) -- surface it as an opaque raw string
                # rather than raising out of pending()/dead_letters().
                row = {"_unparseable_raw_payload": r[2]}
            entries.append(
                {
                    "id": r[0],
                    "table_hint": r[1],
                    "row": row,
                    "attempts": r[3],
                    "status": r[4],
                    "queued_at": r[5],
                    "last_error": r[6],
                }
            )
        return entries

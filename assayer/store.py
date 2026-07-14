"""Supabase-backed store for assayer traces and quarantine rows.

`supabase` is imported lazily (inside the client property) because it is
not installed in every environment that imports this module (e.g. local
dev), but is present on the deployment target.
"""

import json
import os


class Store:
    def __init__(self, url=None, key=None, fallback_path=None):
        self.url = url or os.environ.get("SUPABASE_URL")
        self.key = key or os.environ.get("SUPABASE_SERVICE_KEY")
        self.fallback_path = fallback_path or os.environ.get(
            "ASSAYER_FALLBACK", "/opt/assayer/assayer_fallback.jsonl"
        )
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from supabase import create_client

            self._client = create_client(self.url, self.key)
        return self._client

    def insert_trace(self, row):
        # Sol redteam v4, P0-8: when the row carries an event_id (the
        # outbox's stable idempotency key), upsert on it instead of a bare
        # insert -- a retried outbox entry (lease expired mid-flight, or a
        # crash between this insert and the outbox marking itself complete,
        # report §9 attacks 13/14) must not create a second trace row for
        # the same event. ignore_duplicates=True means a conflicting
        # event_id is a silent no-op (already recorded), tolerated as
        # success rather than raised as an error.
        event_id = row.get("event_id")
        if event_id:
            return (
                self.client.table("assayer_traces")
                .upsert(row, on_conflict="event_id", ignore_duplicates=True)
                .execute()
            )
        return self.client.table("assayer_traces").insert(row).execute()

    def insert_quarantine(self, row) -> int:
        event_id = row.get("event_id")
        if not event_id:
            result = self.client.table("assayer_quarantine").insert(row).execute()
            return result.data[0]["id"]

        # Sol redteam v4, P0-8: same idempotent-upsert reasoning as
        # insert_trace above, but quarantine evidence's caller needs the row
        # id back. ignore_duplicates=True returns no data on a conflict (a
        # no-op DO NOTHING has nothing to RETURNING), so "already inserted"
        # is tolerated by looking the existing row up by event_id rather
        # than treating an empty result as failure.
        result = (
            self.client.table("assayer_quarantine")
            .upsert(row, on_conflict="event_id", ignore_duplicates=True)
            .execute()
        )
        if result.data:
            return result.data[0]["id"]
        existing = (
            self.client.table("assayer_quarantine")
            .select("id")
            .eq("event_id", event_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            return existing.data[0]["id"]
        raise RuntimeError(
            f"upsert on event_id={event_id!r} returned no data and no existing row was found"
        )

    def delete_quarantine(self, ids):
        """Delete quarantine rows by id. Used by retention/purge (Sol
        redteam v4, P1-13) -- callers are responsible for only passing ids
        that have already cleared whatever retention policy applies."""
        if not ids:
            return
        self.client.table("assayer_quarantine").delete().in_("id", list(ids)).execute()

    def seen_pass(self, pipeline, item_hash) -> bool:
        result = (
            self.client.table("assayer_traces")
            .select("id")
            .eq("pipeline", pipeline)
            .eq("item_hash", item_hash)
            .eq("verdict", "pass")
            .limit(1)
            .execute()
        )
        return bool(result.data)

    def list_quarantine(self, pipeline=None, since_iso=None, status="open"):
        query = self.client.table("assayer_quarantine").select("*")
        if pipeline:
            query = query.eq("pipeline", pipeline)
        if since_iso:
            query = query.gte("ts", since_iso)
        if status:
            query = query.eq("status", status)
        result = query.execute()
        return result.data

    def get_quarantine(self, qid):
        result = (
            self.client.table("assayer_quarantine")
            .select("*")
            .eq("id", qid)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def set_quarantine_status(self, qid, status):
        result = (
            self.client.table("assayer_quarantine")
            .update({"status": status})
            .eq("id", qid)
            .execute()
        )
        return result.data

    def stats(self, since_iso=None):
        query = self.client.table("assayer_traces").select(
            "id,pipeline,verdict,checks_json,model,ts"
        )
        if since_iso:
            query = query.gte("ts", since_iso)
        result = query.execute()
        return result.data

    def fallback(self, row):
        try:
            path = self.fallback_path
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            table_hint = "assayer_quarantine" if "payload_json" in row else "assayer_traces"
            with open(path, "a") as f:
                f.write(
                    json.dumps({"table_hint": table_hint, "row": row}, default=str) + "\n"
                )
        except Exception:
            pass

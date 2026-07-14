"""In-memory FakeStore for tests. No network, no supabase import."""


class FakeStore:
    def __init__(
        self,
        raise_on_insert_trace=False,
        raise_on_insert_quarantine=False,
        raise_on_fallback=False,
        raise_on_seen_pass=False,
    ):
        self.traces = []
        self.quarantine = []
        self.fallback_calls = []
        self._next_id = 1
        self._passed_hashes = set()

        self.raise_on_insert_trace = raise_on_insert_trace
        self.raise_on_insert_quarantine = raise_on_insert_quarantine
        self.raise_on_fallback = raise_on_fallback
        self.raise_on_seen_pass = raise_on_seen_pass

    def insert_trace(self, row):
        if self.raise_on_insert_trace:
            raise RuntimeError("insert_trace failed")
        event_id = row.get("event_id")
        if event_id and any(r.get("event_id") == event_id for r in self.traces):
            return row  # already inserted: tolerate, do not duplicate
        self.traces.append(row)
        if row.get("verdict") == "pass":
            self._passed_hashes.add((row["pipeline"], row["item_hash"]))
        return row

    def insert_quarantine(self, row):
        if self.raise_on_insert_quarantine:
            raise RuntimeError("insert_quarantine failed")
        event_id = row.get("event_id")
        if event_id:
            existing = next((r for r in self.quarantine if r.get("event_id") == event_id), None)
            if existing is not None:
                return existing["id"]  # already inserted: tolerate, do not duplicate
        qid = self._next_id
        self._next_id += 1
        row_copy = dict(row)
        row_copy["id"] = qid
        self.quarantine.append(row_copy)
        return qid

    def delete_quarantine(self, ids):
        ids = set(ids)
        self.quarantine = [r for r in self.quarantine if r.get("id") not in ids]

    def seen_pass(self, pipeline, item_hash):
        if self.raise_on_seen_pass:
            raise RuntimeError("seen_pass failed")
        return (pipeline, item_hash) in self._passed_hashes

    def mark_seen(self, pipeline, item_hash):
        self._passed_hashes.add((pipeline, item_hash))

    def fallback(self, row):
        if self.raise_on_fallback:
            raise RuntimeError("fallback failed")
        self.fallback_calls.append(row)

    def list_quarantine(self, pipeline=None, since_iso=None, status="open"):
        rows = self.quarantine
        if pipeline:
            rows = [r for r in rows if r.get("pipeline") == pipeline]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if since_iso:
            rows = [r for r in rows if r.get("ts", "") >= since_iso]
        return rows

    def get_quarantine(self, qid):
        for row in self.quarantine:
            if row.get("id") == qid:
                return row
        return None

    def set_quarantine_status(self, qid, status):
        for row in self.quarantine:
            if row.get("id") == qid:
                row["status"] = status
        return None

    def stats(self, since_iso=None):
        rows = self.traces
        if since_iso:
            rows = [r for r in rows if r.get("ts", "") >= since_iso]
        return rows

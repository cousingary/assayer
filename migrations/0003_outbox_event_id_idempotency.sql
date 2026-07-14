-- Sol redteam v4, P0-8: "duplicate outbox processing". A lease that expires
-- mid-flight, or a crash between a downstream insert succeeding and the
-- outbox marking that entry complete, used to reprocess the same entry --
-- with no idempotency key at the target table, that reprocessing created a
-- second row. assayer/outbox.py now embeds a stable event_id into every row
-- at enqueue time (report §9 attacks 13/14); this migration adds the column
-- + uniqueness constraint that make a repeated insert with the same
-- event_id a no-op instead of a duplicate.
--
-- Existing rows (inserted before this migration, or inserted directly and
-- never via the outbox) get NULL -- Postgres UNIQUE treats multiple NULLs
-- as distinct, so this is safe to apply without backfilling.

ALTER TABLE assayer_traces ADD COLUMN IF NOT EXISTS event_id text;
ALTER TABLE assayer_quarantine ADD COLUMN IF NOT EXISTS event_id text;

ALTER TABLE assayer_traces DROP CONSTRAINT IF EXISTS assayer_traces_event_id_key;
ALTER TABLE assayer_traces ADD CONSTRAINT assayer_traces_event_id_key UNIQUE (event_id);

ALTER TABLE assayer_quarantine DROP CONSTRAINT IF EXISTS assayer_quarantine_event_id_key;
ALTER TABLE assayer_quarantine ADD CONSTRAINT assayer_quarantine_event_id_key UNIQUE (event_id);

NOTIFY pgrst, 'reload schema';

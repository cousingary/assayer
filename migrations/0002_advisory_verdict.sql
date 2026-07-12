-- Sol audit Assayer weakness 2: "database schema does not allow 'advisory'".
-- assayer.verify_scored() can already return an "advisory" verdict (4-way:
-- pass/advisory/fail/error), but this table's CHECK constraint only ever
-- allowed the 3 verify()-era values. Persisting an advisory verdict would
-- fail outright. Widen the constraint to match verify_scored()'s real
-- output domain.
--
-- Postgres auto-names an inline CHECK on CREATE TABLE as
-- <table>_<column>_check; DROP...IF EXISTS makes this migration safe to
-- replay against a database that already has the widened constraint.
ALTER TABLE assayer_traces DROP CONSTRAINT IF EXISTS assayer_traces_verdict_check;
ALTER TABLE assayer_traces ADD CONSTRAINT assayer_traces_verdict_check
    CHECK (verdict IN ('pass', 'fail', 'error', 'advisory'));

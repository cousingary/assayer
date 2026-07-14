-- Sol redteam v4, P1-13: "sensitive evidence" in quarantine/trace payloads.
-- assayer/evidence.py now classifies every row as it's built:
-- data_classification records whether field-level redaction found and
-- masked anything in the payload/reasons body; payload_encrypted records
-- whether the stored payload_json is an opaque authenticated-encryption
-- token (ASSAYER_EVIDENCE_KEY configured) rather than plain JSON. Both are
-- metadata *about* the sensitive body, kept in their own queryable columns
-- rather than inside payload_json itself, so an operator can find/audit
-- sensitive rows without decrypting or parsing anything.

ALTER TABLE assayer_traces ADD COLUMN IF NOT EXISTS data_classification text NOT NULL DEFAULT 'standard';
ALTER TABLE assayer_traces ADD COLUMN IF NOT EXISTS payload_encrypted boolean NOT NULL DEFAULT false;

ALTER TABLE assayer_quarantine ADD COLUMN IF NOT EXISTS data_classification text NOT NULL DEFAULT 'standard';
ALTER TABLE assayer_quarantine ADD COLUMN IF NOT EXISTS payload_encrypted boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_assayer_quarantine_data_classification
    ON assayer_quarantine (data_classification);

NOTIFY pgrst, 'reload schema';

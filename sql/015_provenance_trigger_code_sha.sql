-- Mannaz — B-23, B-54 (2026-10-05): pochodzenie przebiegów.
-- B-54: `source_runs.triggered_by` — co uruchomiło pobranie/import: 'cycle'
-- (`run_p3 cycle`) albo 'cli' (pojedyncze polecenie `run_p3`). Dotąd zapis
-- identyfikowalny tylko po źródle (raport-B24 P4).
-- B-23: `code_sha` — SHA commita kodu przebiegu (`mannaz.provenance.code_sha`,
-- sufiks '-dirty' przy niezatwierdzonych zmianach) w `source_runs` i
-- `risk_daily`; dotąd pochodzenie `risk_daily` ustalał test zachowania.
-- Kolumny NULL: wiersze historyczne zostają bez wartości (brak zgadywania).
-- Idempotentny. Każda baza wymaga tej migracji PRZED kodem z B-23/B-54
-- (INSERT do nowych kolumn).

BEGIN;

ALTER TABLE source_runs ADD COLUMN IF NOT EXISTS triggered_by TEXT;
ALTER TABLE source_runs ADD COLUMN IF NOT EXISTS code_sha TEXT;
ALTER TABLE source_runs DROP CONSTRAINT IF EXISTS source_runs_triggered_by_check;
ALTER TABLE source_runs
    ADD CONSTRAINT source_runs_triggered_by_check
    CHECK (triggered_by IS NULL OR triggered_by IN ('cycle', 'cli'));

ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS code_sha TEXT;

COMMIT;

-- Mannaz — brief CC-N (B-30): poziom 1 budżetu ryzyka (§19.2: ryzyko
-- pojedynczej nazwy <= 1% kapitału satelity) liczony per NAZWĘ (emitent,
-- decyzja ownera 6.3), a nie per wiersz risk_daily. Idempotentny.
--
-- name_key       — id instrumentu wyceny: equity/etf = własny instrument_id;
--                  future = id instrumentu bazowego (instruments.base_symbol
--                  -> instruments.yahoo_symbol). Ta sama spółka w różnych
--                  walutach rozliczenia = ten sam instrument_id = ten sam
--                  klucz. Zapisywany dla wszystkich wierszy (także core).
-- name_risk_pct  — ryzyko całej nazwy (suma risk_pln uprawnionych wierszy
--                  nazwy) jako % kapitału satelity. NULL dla wierszy
--                  nieuprawnionych do budżetu albo przy niepełnej nazwie
--                  (wiersz uprawniony bez risk_pln / brak kapitału) — wtedy
--                  level1_breach też NULL (jawnie niepełne, B-19).

BEGIN;

ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS name_key BIGINT REFERENCES instruments(id);
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS name_risk_pct NUMERIC;

COMMIT;

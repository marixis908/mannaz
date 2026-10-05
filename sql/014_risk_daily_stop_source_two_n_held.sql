-- Mannaz — brief CC-B52 (B-52), 2026-10-05: nowa wartość `stop_source`.
-- Wariant A (decyzja ownera 2026-10-05, §19.1 "nigdy przesuwany w dół"):
-- stop efektywny ma zapadkę w okresie posiadania — gdy wygrywa wcześniejszy
-- stop 2N utrzymany zapadką (`stop_2n_held`), `run_risk` zapisuje
-- `stop_source = 'two_n_held'`. Dotychczasowy CHECK (sql/003) dopuszczał
-- wyłącznie 'two_n' i 'chandelier' — INSERT nowej wartości przerwałby
-- `run_risk`. Rozszerzenie listy jest zgodne wstecz (kod sprzed B-52 zapisuje
-- tylko dwie stare wartości). Ta sama nazwa ograniczenia i ta sama obsługa
-- NULL co w sql/003. Idempotentny (DROP ... IF EXISTS + ADD). Każda inna baza
-- (np. VPS) wymaga tej migracji przed kodem z B-52.

BEGIN;

ALTER TABLE risk_daily DROP CONSTRAINT IF EXISTS risk_daily_stop_source_check;
ALTER TABLE risk_daily
    ADD CONSTRAINT risk_daily_stop_source_check
    CHECK (stop_source IS NULL OR stop_source IN ('two_n', 'chandelier', 'two_n_held'));

COMMIT;

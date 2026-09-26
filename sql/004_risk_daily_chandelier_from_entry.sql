-- Mannaz — poprawka P4.1 (sesja główna 2026-09-26, §19.3 "dzień zero na żywej
-- książce"): risk_daily.chandelier_stop (główny stop, wchodzi do
-- stop_effective) przestaje być ratchetowany od pierwszego POZOSTAŁEGO LOTU
-- (historyczne wejście) — zapadka zaczyna się od INICJALIZACJI SYSTEMU dla tej
-- pozycji (najwcześniejsza risk_date już zapisana w risk_daily sprzed D; brak
-- takiego wiersza -> dzień zero -> wartość BEZ zapadki). Logika w
-- src/mannaz/risk.py::run_risk (`_earliest_prior_risk_date`).
--
-- Stary wariant ("zapadka od pierwszego pozostałego lotu") zostaje jako
-- kolumna informacyjna, NIE wchodzi już do stop_effective:
--   chandelier_from_entry        — poprzednia wartość chandelier_stop.
--   below_chandelier_from_entry  — close_d po złej stronie chandelier_from_entry.
--
-- Idempotentny.

BEGIN;

ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS chandelier_from_entry NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS below_chandelier_from_entry BOOLEAN;

COMMIT;

-- Mannaz — rew. 4.19 (brief CC-CH2, 2026-10-16): wersja reguły stopu, wariant
-- poprzedniej reguły i kontekst trendu w `risk_daily`.
-- stop_rule: wersja reguły stopu użytej w wierszu ('4.19'); wiersze sprzed
--   zmiany = NULL (brak zgadywania wstecz).
-- *_prev_rule (stop_effective_prev_rule, stop_source_prev_rule,
--   risk_state_prev_rule): stop wg poprzedniej reguły (bez A i B1) liczony na
--   tej samej dacie D i tych samych danych; wypełniane tylko, gdy wersja reguły
--   poprzedniej oceny != bieżąca (pierwszy cykl po wdrożeniu), w pozostałych NULL.
-- chandelier_rolling_ctx: Chandelier kroczący 22 sesji z zapadką od RATCHET_START
--   (dotychczasowy składnik chandelier) — kontekst trendu, NIE stop.
-- entry_against_trend: kotwica wejścia wbrew Chandelierowi kroczącemu w sesji
--   początku okresu posiadania (A8); NULL przy braku Chandeliera.
-- entry_price_contract: tylko future — średnia ważona cen kontraktu; `entry_price`
--   kontraktu to kotwica użyta w 2N (zamknięcia bazy, B1).
-- Wszystkie kolumny NULL, bez DEFAULT, bez DML. Idempotentny.
-- Każda baza wymaga tej migracji PRZED kodem rew. 4.19 (INSERT do nowych kolumn).

BEGIN;

ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS stop_rule TEXT;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS stop_effective_prev_rule NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS stop_source_prev_rule TEXT;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS risk_state_prev_rule TEXT;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS chandelier_rolling_ctx NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS entry_against_trend BOOLEAN;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS entry_price_contract NUMERIC;

COMMIT;

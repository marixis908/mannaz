-- Mannaz — brief CC-U (B-19), U4: flaga forward-fillu ceny na D (T27
-- dokumentu projektowego — forward-fill max 1-2 dni, wyłącznie rynek
-- faktycznie zamknięty, zawsze z flagą stale). Idempotentny.
--
-- price_is_stale   — True, gdy pozycja na tym wierszu NIE ma ceny dokładnie
--                     z risk_date: rynek instrumentu wyceny był zamknięty w
--                     risk_date wg exchange_calendars, a ostatnia realna cena
--                     <= risk_date nie jest starsza niż 2 sesje tej giełdy
--                     (`risk.resolve_price_on_d`, status='stale'). Domyślnie
--                     FALSE — cena dokładnie na D (status='ok').
-- price_date_used   — data ceny faktycznie użytej do ATR/stopów/close_d
--                     (== risk_date, gdy price_is_stale=FALSE; wcześniejsza,
--                     gdy price_is_stale=TRUE). Nigdy > risk_date.
--
-- Pozycja bez rozstrzygniętej ceny na D wg tej reguły (rynek otwarty bez
-- ceny / cena starsza niż 2 sesje / brak kalendarza dla giełdy) NIGDY nie
-- trafia do risk_daily jako wiersz "pusty" — `run_risk` rzuca
-- `IncompleteRiskDateError` przed jakimkolwiek zapisem dla całego D (decyzja
-- nadzorcy 2026-09-27, brief CC-U U2). Stąd brak tu kolumny "no_price" — po
-- migracji KAŻDY wiersz risk_daily ma cenę (dokładną albo forward-filled).

BEGIN;

ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS price_is_stale BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS price_date_used DATE;

COMMIT;

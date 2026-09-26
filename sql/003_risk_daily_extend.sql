-- Mannaz — rozszerzenie risk_daily dla ryzyka i sizingu (brief CC-P, P4.1/P4.2, §19).
-- Idempotentny: bezpieczny do wielokrotnego uruchomienia (ADD COLUMN IF NOT EXISTS).
--
-- Kontekst: 001_schema.sql już definiuje risk_daily z (atr20, atr22, sma200,
-- chandelier_stop, two_n_stop, risk_state) — te kolumny są PONOWNIE UŻYTE bez
-- zmian znaczenia:
--   chandelier_stop = stop_chandelier_D (zapadka, §19 wzór), już we właściwej
--   jednostce (waluta notowania instrumentu/bazy).
--   two_n_stop      = stop_2n (entry -/+ 2*ATR20 z daty ostatniego pozostałego
--   lotu).
--   risk_state      = 'HIGH' (close_D po złej stronie stop_effective, ryzyko
--   nie liczone do sum) albo 'NORMAL'.
--
-- Nowe kolumny (P4.1/P4.2):
--   settlement_currency — waluta rozliczenia pozycji (positions_fifo.currency).
--   quote_currency      — waluta notowania instrumentu użyta do ATR/stopów
--                         (dla futures: waluta BAZY, nie kontraktu — zawsze
--                         ta sama PLN na GPW w praktyce, ale pole ogólne).
--   qty                 — ilość z FIFO (kontraktu dla futures, akcji/ETF dla
--                         reszty); może być ujemna (short, KONTRAKTOWY).
--   entry_price         — średnia ważona cena wejścia pozostałych lotów FIFO,
--                         w quote_currency, już po przeliczeniu przez iloczyn
--                         ratio splitów (spójność z close_split_adj). Dla
--                         equity/etf i dla samego kontraktu futures (jednostki
--                         kontraktu ~ jednostki bazy na GPW).
--   close_d             — close_split_adj instrumentu/bazy na risk_date.
--   stop_effective      — max(stop_2n, chandelier_stop) dla long, min(...)
--                         dla short (P4.2).
--   stop_source         — który stop wygrał ('two_n' | 'chandelier').
--   chandelier_hold      — wariant kontrolny B (do P4.4): max(high od
--                         pierwszego pozostałego lotu do D) - 3*ATR22_D (long),
--                         mirror dla short. NIE jest zapadką — pojedyncza
--                         wartość na dzień D.
--   below_chandelier_hold — close_D po złej stronie chandelier_hold (wariant B).
--   position_kind        — 'long' | 'short' (znak qty, P4.2 kontrakty).
--   risk_native          — max(close_D - stop_effective, 0) [long] albo
--                         max(stop_effective - close_D, 0) [short], * |qty| *
--                         multiplier (1 dla equity/etf), w quote_currency.
--                         NULL gdy multiplier_missing.
--   fx_rate / fx_rate_date — kurs NBP A (pair quote_currency/PLN) użyty do
--                         przewalutowania i data, z której faktycznie
--                         pochodzi (<=risk_date, ostatni dostępny). 1/risk_date
--                         gdy quote_currency='PLN'.
--   risk_pln             — risk_native * fx_rate. NULL gdy multiplier_missing
--                         albo brak ceny na D (note != '').
--   risk_pct_satellite_capital — risk_pln / kapitał_satelity_total * 100.
--                         NULL dla pozycji is_core (kapitał satelity je
--                         wyklucza z definicji — patrz §19.2, brief P4.1) i
--                         dla pozycji bez policzalnego risk_pln.
--   level1_breach        — risk_pct_satellite_capital > 1 (poziom 1, §19.2).
--                         NULL gdy risk_pct_satellite_capital jest NULL.
--   regime                — REGIME z §19.1 (close_D < SMA200_D < SMA200_D-21).
--   warning               — OSTRZEŻENIE z §19.1 (sigma20 > 2*sigma60 OR
--                         close_D/max(close,252) < 0,75).
--   multiplier_missing    — futures z instruments.multiplier IS NULL (brief:
--                         dziś tylko FMDVZ26). Domyślnie FALSE dla equity/etf.
--   note                  — powód braku obliczenia (np. 'no_price_on_d',
--                         'insufficient_history', 'multiplier_missing').
--
-- Unique key rozszerzony o settlement_currency — positions_fifo pozwala
-- teoretycznie na dwie pozycje tego samego (rachunek, instrument) w różnych
-- walutach rozliczenia (rzadkie, ale schema na to pozwala — patrz komentarz
-- positions_fifo w 001_schema.sql), risk_daily musi mieć ten sam klucz.

BEGIN;

ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS settlement_currency TEXT;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS quote_currency TEXT;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS qty NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS entry_price NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS close_d NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS stop_effective NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS stop_source TEXT;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS chandelier_hold NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS below_chandelier_hold BOOLEAN;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS position_kind TEXT;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS risk_native NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS fx_rate NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS fx_rate_date DATE;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS risk_pln NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS risk_pct_satellite_capital NUMERIC;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS level1_breach BOOLEAN;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS regime BOOLEAN;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS warning BOOLEAN;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS multiplier_missing BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';

ALTER TABLE risk_daily DROP CONSTRAINT IF EXISTS risk_daily_stop_source_check;
ALTER TABLE risk_daily
    ADD CONSTRAINT risk_daily_stop_source_check
    CHECK (stop_source IS NULL OR stop_source IN ('two_n', 'chandelier'));

ALTER TABLE risk_daily DROP CONSTRAINT IF EXISTS risk_daily_position_kind_check;
ALTER TABLE risk_daily
    ADD CONSTRAINT risk_daily_position_kind_check
    CHECK (position_kind IS NULL OR position_kind IN ('long', 'short'));

ALTER TABLE risk_daily DROP CONSTRAINT IF EXISTS risk_daily_risk_state_check;
ALTER TABLE risk_daily
    ADD CONSTRAINT risk_daily_risk_state_check
    CHECK (risk_state IS NULL OR risk_state IN ('HIGH', 'NORMAL'));

ALTER TABLE risk_daily DROP CONSTRAINT IF EXISTS risk_daily_rachunek_instrument_id_risk_date_key;
ALTER TABLE risk_daily DROP CONSTRAINT IF EXISTS risk_daily_rachunek_instrument_currency_risk_date_key;
ALTER TABLE risk_daily
    ADD CONSTRAINT risk_daily_rachunek_instrument_currency_risk_date_key
    UNIQUE (rachunek, instrument_id, settlement_currency, risk_date);

COMMIT;

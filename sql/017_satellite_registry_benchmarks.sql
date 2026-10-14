-- Mannaz — B-38 część B (brief CC-B38B, rew. 4.17, decyzje 2026-10-14):
-- progi satelity w bazie oraz trwały rejestr alertów i naruszeń M89.
--
-- 1. `instruments.is_benchmark` — instrument progu (M81). Cykl pobiera jego
--    ceny (`prices._mapped_instruments`: OR is_benchmark), satelita czyta progi
--    z `prices_daily` tą samą ścieżką ceny co pozycje (close_split_adj, T27).
--    Instrument progu nie jest pozycją ani rdzeniem: bez transakcji, bez
--    `positions_fifo`, `is_core` = false.
-- 2. SXR8 (iShares Core S&P 500 UCITS ETF, akumulujący — cena = total return):
--    ISIN IE00B5BMR087, Yahoo SXR8.DE, EUR, exchange jak wiersz SPYI (XETRA,
--    odczyt P2 2026-10-14). SPYI (id 3) zostaje rdzeniem, wiersz bez zmian.
-- 3. `satellite_mandate_events` i `satellite_mandate_violations` — rejestr M89
--    tylko do dopisywania: wiersz zapisany raz zostaje także wtedy, gdy
--    przeliczenie po rewizji danych go nie odtwarza. UPDATE i DELETE odrzuca
--    trigger. Zapis: etap satelity cyklu, INSERT ... ON CONFLICT DO NOTHING.
--
-- Idempotentny: drugie zastosowanie = 0 zmian. Zgodny wstecz: kod sprzed
-- B-38B nie czyta nowych obiektów, a wiersza SXR8 nie wybiera do cen ani
-- ryzyka (brak pozycji, is_core = false; raport-B38B P3).

BEGIN;

-- 1. Kolumna instrumentu progu.
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS is_benchmark BOOLEAN NOT NULL DEFAULT FALSE;

-- 2. Instrument progu SXR8.
INSERT INTO instruments (broker_ticker, isin, yahoo_symbol, currency, exchange, instrument_type, is_core, is_benchmark)
VALUES ('SXR8', 'IE00B5BMR087', 'SXR8.DE', 'EUR', 'XETRA', 'etf', false, true)
ON CONFLICT (isin) WHERE isin IS NOT NULL DO NOTHING;

DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM instruments
    WHERE isin = 'IE00B5BMR087'
      AND broker_ticker = 'SXR8' AND yahoo_symbol = 'SXR8.DE' AND currency = 'EUR'
      AND exchange = 'XETRA' AND instrument_type = 'etf'
      AND is_core = false AND is_benchmark = true;
    IF n <> 1 THEN
        RAISE EXCEPTION '017: wiersz IE00B5BMR087 ma w bazie inne wartosci niz decyzja 3 (zgodnych: %)', n;
    END IF;
    SELECT count(*) INTO n FROM instruments WHERE is_benchmark;
    IF n <> 1 THEN
        RAISE EXCEPTION '017: oczekiwany 1 instrument is_benchmark, jest %', n;
    END IF;
END $$;

-- 3. Rejestr zdarzeń alertów M89 (ostrzeżenie 75%, alert 100%).
CREATE TABLE IF NOT EXISTS satellite_mandate_events (
    id                BIGSERIAL PRIMARY KEY,
    registry          SMALLINT    NOT NULL CHECK (registry IN (1, 2)),
    component         TEXT        NOT NULL CHECK (component IN ('abs', 'rel_sp500', 'rel_spyi')),
    event_date        DATE        NOT NULL,
    level             TEXT        NOT NULL CHECK (level IN ('ostrzezenie', 'alert')),
    dd_current        NUMERIC     NOT NULL,
    limit_value       NUMERIC     NOT NULL,
    data_status       TEXT        NOT NULL CHECK (data_status IN ('zweryfikowane', 'niezweryfikowane')),
    first_seen_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    code_sha          TEXT,
    CONSTRAINT uq_satellite_mandate_events UNIQUE (registry, component, event_date, level)
);

-- 4. Rejestr naruszeń mandatu M88/M89 (okno kroczące kończące się w window_end).
CREATE TABLE IF NOT EXISTS satellite_mandate_violations (
    id                BIGSERIAL PRIMARY KEY,
    registry          SMALLINT    NOT NULL CHECK (registry IN (1, 2)),
    component         TEXT        NOT NULL CHECK (component IN ('abs', 'rel_sp500', 'rel_spyi')),
    window_start      DATE        NOT NULL,
    window_end        DATE        NOT NULL,
    max_dd            NUMERIC     NOT NULL,
    limit_value       NUMERIC     NOT NULL,
    data_status       TEXT        NOT NULL CHECK (data_status IN ('zweryfikowane', 'niezweryfikowane')),
    first_seen_run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    code_sha          TEXT,
    CONSTRAINT uq_satellite_mandate_violations UNIQUE (registry, component, window_end)
);

-- 5. Tylko dopisywanie: UPDATE i DELETE odrzucone w bazie.
CREATE OR REPLACE FUNCTION satellite_registry_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'rejestr M89 (%) tylko do dopisywania: % odrzucony', TG_TABLE_NAME, TG_OP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_satellite_mandate_events_append_only'
                   AND tgrelid = 'satellite_mandate_events'::regclass) THEN
        CREATE TRIGGER trg_satellite_mandate_events_append_only
            BEFORE UPDATE OR DELETE ON satellite_mandate_events
            FOR EACH ROW EXECUTE FUNCTION satellite_registry_append_only();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_satellite_mandate_violations_append_only'
                   AND tgrelid = 'satellite_mandate_violations'::regclass) THEN
        CREATE TRIGGER trg_satellite_mandate_violations_append_only
            BEFORE UPDATE OR DELETE ON satellite_mandate_violations
            FOR EACH ROW EXECUTE FUNCTION satellite_registry_append_only();
    END IF;
END $$;

COMMIT;

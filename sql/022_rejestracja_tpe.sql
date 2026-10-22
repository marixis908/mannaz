-- Mannaz — rejestracja kontraktu FTPEZ26 (import 2026-10-22, plik
-- 2026-10-21_4b365e63.csv) oraz jego instrumentu bazowego TPE.WA. Cykl
-- 2026-10-22 08:29 zakończył się DATA FAILURE na bramce rejestracji (brak
-- bazy, tematu i mnożnika FTPEZ26). Pozycja +5 (long, 2 kupna 2026-10-20).
-- Wzór: sql/018 (FPKNZ26, FKTYZ26). Idempotentny.
--
-- Reguła ownera 2026-09-30: kontrakt mapuje się wyłącznie przez instrument
-- bazowy (bez własnego yahoo_symbol; cena i ryzyko idą przez bazę).
--
-- Źródła (odczyt 2026-10-22):
--   TPE.WA  = wzór KTY.WA z 018 (equity, PLN, GPW, is_core=false, bez tematu,
--             bez archetypu — baza, nie pozycja); ISIN z karty spółki
--             https://www.gpw.pl/spolka?isin=PLTAURN00011 (skrót TAURONPE);
--             przed migracją brak wiersza (ISIN 0, yahoo_symbol 0).
--   mnożnik = https://www.gpw.pl/standard-kontraktow-terminowych-na-akcje-spolek
--             ("TAURON POLSKA ENERGIA S.A. | FTPE | 1000"; wyjątek seryjny
--             w przypisach tylko dla LPP); kontrolnie z rozliczeń 2026-10-20:
--             3 x 9,481 x 1000 = 28 443,00 (+7,50 prowizji),
--             2 x 9,500 x 1000 = 19 000,00 (+5,00 prowizji).
--   temat   = decyzja ownera 2026-10-22: "GPW-przemysl/energia" (istniejący
--             temat FPGEZ26/FPKNZ26/FKTYZ26; wspólny limit poziomu 2).
--   archetyp= decyzja ownera 2026-10-22: A0 (klucz §14.1 pyt. 1), secondary A1
--             (pyt. 8): Tauron — energetyka (pyt. 2-7 nie); kapitalizacja
--             17,08 mld PLN (yfinance TPE.WA, 2026-10-22) >= T34 (5 mld PLN),
--             Operating Income > 0 w 2023-2025 (3 209 / 2 547 / 5 004 mln PLN).
--             Pyt. 9 (capex/przychód 2025 = 15,4% > T36) nie jest zadawane —
--             pierwsze "tak" rozstrzyga; spójnie z ORLEN w 018.
-- Nie nadpisuje innych, już zapisanych wartości — rozbieżność = błąd migracji.

BEGIN;

-- 1. Instrument bazowy TPE.WA.
DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM instruments WHERE isin = 'PLTAURN00011';
    IF n > 1 THEN RAISE EXCEPTION '022: więcej niż 1 wiersz z ISIN PLTAURN00011 (%)', n; END IF;
    SELECT count(*) INTO n FROM instruments
    WHERE isin = 'PLTAURN00011' AND yahoo_symbol IS NOT NULL AND yahoo_symbol <> 'TPE.WA';
    IF n > 0 THEN RAISE EXCEPTION '022: wiersz ISIN PLTAURN00011 ma inny yahoo_symbol niz TPE.WA'; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'TPE.WA' AND isin IS DISTINCT FROM 'PLTAURN00011';
    IF n > 0 THEN RAISE EXCEPTION '022: TPE.WA przypisany do wiersza bez ISIN PLTAURN00011'; END IF;
END $$;

UPDATE instruments SET yahoo_symbol = 'TPE.WA'
WHERE isin = 'PLTAURN00011' AND yahoo_symbol IS NULL;

INSERT INTO instruments (broker_ticker, name, isin, yahoo_symbol, currency, exchange, instrument_type, is_core)
SELECT 'TPE', 'Tauron Polska Energia S.A.', 'PLTAURN00011', 'TPE.WA', 'PLN', 'GPW', 'equity', false
WHERE NOT EXISTS (SELECT 1 FROM instruments WHERE yahoo_symbol = 'TPE.WA' OR isin = 'PLTAURN00011');

-- 2. Kontrakt (id wyszukane po broker_ticker).
DO $$
DECLARE n INT; conflicts INT;
BEGIN
    SELECT count(*) INTO n FROM instruments WHERE broker_ticker = 'FTPEZ26' AND instrument_type = 'future';
    IF n <> 1 THEN RAISE EXCEPTION '022: oczekiwany 1 wiersz FTPEZ26 (future), jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'TPE.WA';
    IF n <> 1 THEN RAISE EXCEPTION '022: oczekiwany 1 instrument z yahoo_symbol TPE.WA, jest %', n; END IF;
    SELECT count(*) INTO conflicts FROM instruments
    WHERE broker_ticker = 'FTPEZ26'
      AND ((multiplier IS NOT NULL AND multiplier <> 1000)
        OR (base_symbol IS NOT NULL AND base_symbol <> 'TPE.WA')
        OR (theme IS NOT NULL AND theme <> 'GPW-przemysl/energia'));
    IF conflicts > 0 THEN RAISE EXCEPTION '022: FTPEZ26 ma w bazie inny mnoznik/baze/temat'; END IF;
END $$;

UPDATE instruments
SET base_symbol = 'TPE.WA',
    multiplier = 1000,
    multiplier_source = 'GPW STD - lista mnożników klas (FTPE=1000, brak wyjątku seryjnego); odczyt 2026-10-22',
    theme = 'GPW-przemysl/energia'
WHERE broker_ticker = 'FTPEZ26' AND instrument_type = 'future';

-- 3. Archetyp kontraktu (mechanizm seed_archetypes_v2 + 006): A0, secondary = archetyp bazy.
INSERT INTO archetype_assignments
    (instrument_id, archetype, archetype_secondary, answers, resolution, key_version,
     source_tags, archetype_key, valid_from, reason)
SELECT i.id, 'A0', 'A1', 'T--------', 'q1', 'v2', 'Z', 'A0', DATE '2026-10-20',
       'future - outside §14; base TPE.WA mcap 17.08 bn PLN >= T34, operating income > 0 2023-2025 (q8); decyzja ownera 2026-10-22'
FROM instruments i
WHERE i.broker_ticker = 'FTPEZ26' AND i.instrument_type = 'future'
ON CONFLICT (instrument_id, valid_from) DO NOTHING;

COMMIT;

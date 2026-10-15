-- Mannaz — rejestracja kontraktów FPKNZ26 i FKTYZ26 (import 2026-10-15) oraz
-- ich instrumentów bazowych PKN.WA i KTY.WA (B-66). FPKNZ26 to pierwsza krótka
-- pozycja w historii (-5, KONTRAKTOWY); po naprawie FIFO (B-65) bramka
-- rejestracji zatrzymałaby cykl (brak bazy, tematu i mnożnika obu kontraktów).
-- Wzór: sql/016 (FKRUZ26, FOPLZ26). Idempotentny.
--
-- Reguła ownera 2026-09-30: kontrakt mapuje się wyłącznie przez instrument
-- bazowy (bez własnego yahoo_symbol; cena i ryzyko idą przez bazę).
--
-- Źródła (odczyt 2026-10-15):
--   PKN.WA  = istniejący wiersz akcji ORLEN z ISIN PLPKN0000018 (karta spółki
--             https://www.gpw.pl/spolka?isin=PLPKN0000018, skrót PKNORLEN);
--             yahoo_symbol uzupełniany tylko gdy NULL, inna wartość = błąd.
--             B-33: drugi wiersz tej spółki (PKNORLEN, bez ISIN i yahoo_symbol)
--             pozostaje bez zmian — decyzja ownera, bez scalania.
--   KTY.WA  = wzór KRU.WA z 016 (equity, PLN, GPW, is_core=false, bez tematu,
--             bez archetypu — baza, nie pozycja); ISIN z karty spółki
--             https://www.gpw.pl/spolka?isin=PLKETY000011 (skrót KETY)
--   mnożnik = https://www.gpw.pl/standard-kontraktow-terminowych-na-akcje-spolek
--             ("POLSKI KONCERN NAFTOWY ORLEN S.A. | FPKN | 100",
--             "GRUPA KĘTY S.A. | FKTY | 10"; wyjątek seryjny w przypisach tylko
--             dla LPP); kontrolnie FKGH = 100, FKRU = 10, FOPL = 1000,
--             FPGE = 1000, zgodnie z 012/016 i bazą.
--   temat   = decyzja ownera 2026-10-15: "GPW-przemysl/energia" dla obu
--             (istniejący temat FPGEZ26; ryzyko sumuje się w jednym limicie
--             poziomu 2).
--   archetyp= A0 (klucz §14.1 pyt. 1), secondary A1 dla obu baz (pyt. 8):
--             ORLEN: rafinacja/petrochemia/energia (pyt. 2-7 nie);
--             kapitalizacja 179,3 mld PLN (yfinance PKN.WA, 2026-10-15)
--             >= T34 (5 mld PLN), Operating Income > 0 w 2023-2025
--             (38,9 / 21,0 / 24,7 mld PLN).
--             Kęty: wyroby aluminiowe i opakowania (pyt. 2-7 nie);
--             kapitalizacja 12,4 mld PLN (yfinance KTY.WA, 2026-10-15)
--             >= T34, Operating Income > 0 w 2023-2025 (686 / 719 / 785 mln PLN).
--   tematy  = decyzja ownera 2026-10-15 dla AVGO, USAR, UUUU (sekcja 4).
-- Nie nadpisuje innych, już zapisanych wartości — rozbieżność = błąd migracji.

BEGIN;

-- 1. Instrumenty bazowe.
-- ORLEN: wiersz z ISIN musi istnieć dokładnie raz; yahoo_symbol tylko gdy NULL.
DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM instruments WHERE isin = 'PLPKN0000018';
    IF n <> 1 THEN RAISE EXCEPTION '018: oczekiwany 1 wiersz z ISIN PLPKN0000018, jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments
    WHERE isin = 'PLPKN0000018' AND yahoo_symbol IS NOT NULL AND yahoo_symbol <> 'PKN.WA';
    IF n > 0 THEN RAISE EXCEPTION '018: wiersz ISIN PLPKN0000018 ma inny yahoo_symbol niz PKN.WA'; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'PKN.WA' AND isin IS DISTINCT FROM 'PLPKN0000018';
    IF n > 0 THEN RAISE EXCEPTION '018: PKN.WA przypisany do wiersza bez ISIN PLPKN0000018'; END IF;
    SELECT count(*) INTO n FROM instruments
    WHERE isin = 'PLKETY000011' AND yahoo_symbol IS NOT NULL AND yahoo_symbol <> 'KTY.WA';
    IF n > 0 THEN RAISE EXCEPTION '018: wiersz ISIN PLKETY000011 ma inny yahoo_symbol niz KTY.WA'; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'KTY.WA' AND isin IS DISTINCT FROM 'PLKETY000011';
    IF n > 0 THEN RAISE EXCEPTION '018: KTY.WA przypisany do wiersza bez ISIN PLKETY000011'; END IF;
END $$;

UPDATE instruments SET yahoo_symbol = 'PKN.WA'
WHERE isin = 'PLPKN0000018' AND yahoo_symbol IS NULL;

UPDATE instruments SET yahoo_symbol = 'KTY.WA'
WHERE isin = 'PLKETY000011' AND yahoo_symbol IS NULL;

INSERT INTO instruments (broker_ticker, name, isin, yahoo_symbol, currency, exchange, instrument_type, is_core)
SELECT 'KTY', 'Grupa Kęty S.A.', 'PLKETY000011', 'KTY.WA', 'PLN', 'GPW', 'equity', false
WHERE NOT EXISTS (SELECT 1 FROM instruments WHERE yahoo_symbol = 'KTY.WA' OR isin = 'PLKETY000011');

-- 2. Kontrakty (id wyszukane po broker_ticker).
DO $$
DECLARE n INT; conflicts INT;
BEGIN
    SELECT count(*) INTO n FROM instruments WHERE broker_ticker = 'FPKNZ26' AND instrument_type = 'future';
    IF n <> 1 THEN RAISE EXCEPTION '018: oczekiwany 1 wiersz FPKNZ26 (future), jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments WHERE broker_ticker = 'FKTYZ26' AND instrument_type = 'future';
    IF n <> 1 THEN RAISE EXCEPTION '018: oczekiwany 1 wiersz FKTYZ26 (future), jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'PKN.WA';
    IF n <> 1 THEN RAISE EXCEPTION '018: oczekiwany 1 instrument z yahoo_symbol PKN.WA, jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'KTY.WA';
    IF n <> 1 THEN RAISE EXCEPTION '018: oczekiwany 1 instrument z yahoo_symbol KTY.WA, jest %', n; END IF;
    SELECT count(*) INTO conflicts FROM instruments
    WHERE broker_ticker = 'FPKNZ26'
      AND ((multiplier IS NOT NULL AND multiplier <> 100)
        OR (base_symbol IS NOT NULL AND base_symbol <> 'PKN.WA')
        OR (theme IS NOT NULL AND theme <> 'GPW-przemysl/energia'));
    IF conflicts > 0 THEN RAISE EXCEPTION '018: FPKNZ26 ma w bazie inny mnoznik/baze/temat'; END IF;
    SELECT count(*) INTO conflicts FROM instruments
    WHERE broker_ticker = 'FKTYZ26'
      AND ((multiplier IS NOT NULL AND multiplier <> 10)
        OR (base_symbol IS NOT NULL AND base_symbol <> 'KTY.WA')
        OR (theme IS NOT NULL AND theme <> 'GPW-przemysl/energia'));
    IF conflicts > 0 THEN RAISE EXCEPTION '018: FKTYZ26 ma w bazie inny mnoznik/baze/temat'; END IF;
END $$;

UPDATE instruments
SET base_symbol = 'PKN.WA',
    multiplier = 100,
    multiplier_source = 'GPW STD - lista mnożników klas (FPKN=100, brak wyjątku seryjnego); odczyt 2026-10-15',
    theme = 'GPW-przemysl/energia'
WHERE broker_ticker = 'FPKNZ26' AND instrument_type = 'future';

UPDATE instruments
SET base_symbol = 'KTY.WA',
    multiplier = 10,
    multiplier_source = 'GPW STD - lista mnożników klas (FKTY=10, brak wyjątku seryjnego); odczyt 2026-10-15',
    theme = 'GPW-przemysl/energia'
WHERE broker_ticker = 'FKTYZ26' AND instrument_type = 'future';

-- 3. Archetypy kontraktów (mechanizm seed_archetypes_v2 + 006): A0, secondary = archetyp bazy.
INSERT INTO archetype_assignments
    (instrument_id, archetype, archetype_secondary, answers, resolution, key_version,
     source_tags, archetype_key, valid_from, reason)
SELECT i.id, 'A0', 'A1', 'T--------', 'q1', 'v2', 'Z', 'A0', DATE '2026-10-15', t.reason
FROM instruments i
JOIN (VALUES
    ('FPKNZ26', 'future - outside §14; base PKN.WA mcap 179.3 bn PLN >= T34, operating income > 0 2023-2025 (q8); decyzja ownera 2026-10-15'),
    ('FKTYZ26', 'future - outside §14; base KTY.WA mcap 12.4 bn PLN >= T34, operating income > 0 2023-2025 (q8); decyzja ownera 2026-10-15')
) AS t(ticker, reason) ON t.ticker = i.broker_ticker
WHERE i.instrument_type = 'future'
ON CONFLICT (instrument_id, valid_from) DO NOTHING;

-- 4. Tematy nowych pozycji ZAGRANICZNY z importu 2026-10-15 (bramka rejestracji
--    na positions_as_of(2026-10-15): brak instruments.theme). Decyzja ownera
--    2026-10-15: AVGO = "semi/AI-infra", USAR = "surowce/metale",
--    UUUU = "surowce/metale" (tematy istniejące, sql/seed_themes.sql).
--    Tylko gdy temat pusty; inny niepusty temat = błąd migracji.
DO $$
DECLARE n INT;
BEGIN
    SELECT count(*) INTO n FROM instruments
    WHERE (broker_ticker = 'AVGO' AND theme IS NOT NULL AND theme <> 'semi/AI-infra')
       OR (broker_ticker IN ('USAR', 'UUUU') AND theme IS NOT NULL AND theme <> 'surowce/metale');
    IF n > 0 THEN RAISE EXCEPTION '018: AVGO/USAR/UUUU maja w bazie inny temat'; END IF;
    SELECT count(*) INTO n FROM instruments WHERE broker_ticker IN ('AVGO', 'USAR', 'UUUU');
    IF n <> 3 THEN RAISE EXCEPTION '018: oczekiwane 3 wiersze AVGO/USAR/UUUU, jest %', n; END IF;
END $$;

UPDATE instruments SET theme = 'semi/AI-infra'
WHERE broker_ticker = 'AVGO' AND theme IS NULL;

UPDATE instruments SET theme = 'surowce/metale'
WHERE broker_ticker IN ('USAR', 'UUUU') AND theme IS NULL;

COMMIT;

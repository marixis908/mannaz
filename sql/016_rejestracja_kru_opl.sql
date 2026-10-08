-- Mannaz — rejestracja kontraktów FKRUZ26 i FOPLZ26 (import 2026-10-08) oraz
-- ich instrumentów bazowych KRU.WA i OPL.WA. Cykl z 2026-10-08 11:56 zatrzymała
-- bramka rejestracji (brak bazy, tematu i mnożnika obu kontraktów).
-- Wzór: sql/012 (FKGHZ26). Idempotentny.
--
-- Reguła ownera 2026-09-30: kontrakt mapuje się wyłącznie przez instrument
-- bazowy (bez własnego yahoo_symbol; cena i ryzyko idą przez bazę).
--
-- Źródła (odczyt 2026-10-08):
--   KRU.WA  = wzór KGH.WA (equity, PLN, GPW, is_core=false, bez tematu,
--             bez archetypu — baza, nie pozycja); ISIN z karty spółki
--             https://www.gpw.pl/spolka?isin=PLKRK0000010
--   OPL.WA  = jw.; ISIN z karty spółki https://www.gpw.pl/spolka?isin=PLTLKPL00017
--   mnożnik = https://www.gpw.pl/standard-kontraktow-terminowych-na-akcje-spolek
--             ("KRUK S.A. | FKRU | 10", "ORANGE POLSKA S.A. | FOPL | 1000";
--             wyjątek seryjny w przypisach tylko dla LPP); kontrolnie
--             FKGH = 100, zgodnie z sql/012.
--   temat   = decyzja ownera 2026-10-08: nowe tematy "GPW-finanse" (FKRUZ26)
--             i "GPW-telekom" (FOPLZ26).
--   archetyp= decyzja ownera 2026-10-08: A0 (klucz §14.1 pyt. 1), secondary A1
--             dla obu baz. KRUK: windykacja (jak PRA Group, B2 Holding), nie
--             pożyczkodawca -> pyt. 3 nie; kapitalizacja ok. 8,4 mld PLN
--             (2026-08-05) >= T34 i zysk dodatni -> pyt. 8 tak. Orange Polska:
--             kapitalizacja >= T34, EBIT > 0 -> pyt. 8 tak (przed pyt. 9).
-- Nie nadpisuje innych, już zapisanych wartości — rozbieżność = błąd migracji.

BEGIN;

-- 1. Instrumenty bazowe (jeśli nie istnieją).
INSERT INTO instruments (broker_ticker, name, isin, yahoo_symbol, currency, exchange, instrument_type, is_core)
SELECT 'KRU', 'KRUK S.A.', 'PLKRK0000010', 'KRU.WA', 'PLN', 'GPW', 'equity', false
WHERE NOT EXISTS (SELECT 1 FROM instruments WHERE yahoo_symbol = 'KRU.WA' OR isin = 'PLKRK0000010');

INSERT INTO instruments (broker_ticker, name, isin, yahoo_symbol, currency, exchange, instrument_type, is_core)
SELECT 'OPL', 'Orange Polska S.A.', 'PLTLKPL00017', 'OPL.WA', 'PLN', 'GPW', 'equity', false
WHERE NOT EXISTS (SELECT 1 FROM instruments WHERE yahoo_symbol = 'OPL.WA' OR isin = 'PLTLKPL00017');

-- 2. Kontrakty (id wyszukane po broker_ticker).
DO $$
DECLARE n INT; conflicts INT;
BEGIN
    SELECT count(*) INTO n FROM instruments WHERE broker_ticker = 'FKRUZ26' AND instrument_type = 'future';
    IF n <> 1 THEN RAISE EXCEPTION '016: oczekiwany 1 wiersz FKRUZ26 (future), jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments WHERE broker_ticker = 'FOPLZ26' AND instrument_type = 'future';
    IF n <> 1 THEN RAISE EXCEPTION '016: oczekiwany 1 wiersz FOPLZ26 (future), jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'KRU.WA';
    IF n <> 1 THEN RAISE EXCEPTION '016: oczekiwany 1 instrument z yahoo_symbol KRU.WA, jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'OPL.WA';
    IF n <> 1 THEN RAISE EXCEPTION '016: oczekiwany 1 instrument z yahoo_symbol OPL.WA, jest %', n; END IF;
    SELECT count(*) INTO conflicts FROM instruments
    WHERE broker_ticker = 'FKRUZ26'
      AND ((multiplier IS NOT NULL AND multiplier <> 10)
        OR (base_symbol IS NOT NULL AND base_symbol <> 'KRU.WA')
        OR (theme IS NOT NULL AND theme <> 'GPW-finanse'));
    IF conflicts > 0 THEN RAISE EXCEPTION '016: FKRUZ26 ma w bazie inny mnoznik/baze/temat'; END IF;
    SELECT count(*) INTO conflicts FROM instruments
    WHERE broker_ticker = 'FOPLZ26'
      AND ((multiplier IS NOT NULL AND multiplier <> 1000)
        OR (base_symbol IS NOT NULL AND base_symbol <> 'OPL.WA')
        OR (theme IS NOT NULL AND theme <> 'GPW-telekom'));
    IF conflicts > 0 THEN RAISE EXCEPTION '016: FOPLZ26 ma w bazie inny mnoznik/baze/temat'; END IF;
END $$;

UPDATE instruments
SET base_symbol = 'KRU.WA',
    multiplier = 10,
    multiplier_source = 'GPW STD - lista mnożników klas (FKRU=10, brak wyjątku seryjnego); odczyt 2026-10-08',
    theme = 'GPW-finanse'
WHERE broker_ticker = 'FKRUZ26' AND instrument_type = 'future';

UPDATE instruments
SET base_symbol = 'OPL.WA',
    multiplier = 1000,
    multiplier_source = 'GPW STD - lista mnożników klas (FOPL=1000, brak wyjątku seryjnego); odczyt 2026-10-08',
    theme = 'GPW-telekom'
WHERE broker_ticker = 'FOPLZ26' AND instrument_type = 'future';

-- 3. Archetypy kontraktów (mechanizm seed_archetypes_v2 + 006): A0, secondary = archetyp bazy.
INSERT INTO archetype_assignments
    (instrument_id, archetype, archetype_secondary, answers, resolution, key_version,
     source_tags, archetype_key, valid_from, reason)
SELECT i.id, 'A0', 'A1', 'T--------', 'q1', 'v2', 'Z', 'A0', DATE '2026-10-08', t.reason
FROM instruments i
JOIN (VALUES
    ('FKRUZ26', 'future - outside §14; base KRU.WA debt collector (not lender, q3 no), mcap >= T34, profitable (q8); decyzja ownera 2026-10-08'),
    ('FOPLZ26', 'future - outside §14; base OPL.WA mcap >= T34, EBIT > 0 (q8); decyzja ownera 2026-10-08')
) AS t(ticker, reason) ON t.ticker = i.broker_ticker
WHERE i.instrument_type = 'future'
ON CONFLICT (instrument_id, valid_from) DO NOTHING;

COMMIT;

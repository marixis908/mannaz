-- Mannaz — brief CC-K: rejestracja kontraktu FKGHZ26 (import 2026-09-30) i
-- jego instrumentu bazowego KGH.WA. Cykl z 2026-09-30 20:05 zatrzymała bramka
-- rejestracji (brak mnożnika, bazy, tematu i archetypu FKGHZ26; KGH.WA nie
-- istniał — baza bez transakcji nie powstaje z importu). Idempotentny.
--
-- Reguła ownera 2026-09-30: kontrakt mapuje się wyłącznie przez instrument
-- bazowy (bez własnego yahoo_symbol; cena i ryzyko idą przez bazę).
--
-- Źródła (odczyt 2026-09-30):
--   KGH.WA  = wzór CDR.WA/PGE.WA (equity, PLN, GPW, is_core=false, bez tematu,
--             bez archetypu — baza, nie pozycja); ISIN z karty spółki
--             https://www.gpw.pl/spolka?isin=PLKGHM000017; nazwa z yfinance.
--   mnożnik = https://www.gpw.pl/standard-kontraktow-terminowych-na-akcje-spolek
--             ("KGHM POLSKA MIEDŹ S.A. | FKGH | 100", wyjątek seryjny w
--             przypisach tylko dla LPP); karta serii FKGHZ26 — 404;
--             potwierdzone rozliczeniem brokera 2026-09-29 (1 x 323,49 ->
--             32 352 PLN = 323,49 x 100 + 3 PLN prowizji).
--   temat   = decyzja ownera 2026-09-30: nowy temat "surowce/metale" (15.).
--   archetyp= decyzja ownera 2026-09-30: A0 (klucz §14.1 pyt. 1), secondary A9
--             (baza: OpInc 2023 < 0 -> pyt. 8 nie; capex/rev 2025 15,3 % > 10 %
--             -> pyt. 9 tak).
-- Nie nadpisuje innych, już zapisanych wartości — rozbieżność = błąd migracji.

BEGIN;

-- 1. Instrument bazowy KGH.WA (jeśli nie istnieje).
INSERT INTO instruments (broker_ticker, name, isin, yahoo_symbol, currency, exchange, instrument_type, is_core)
SELECT 'KGH', 'KGHM Polska Miedz S.A.', 'PLKGHM000017', 'KGH.WA', 'PLN', 'GPW', 'equity', false
WHERE NOT EXISTS (SELECT 1 FROM instruments WHERE yahoo_symbol = 'KGH.WA' OR isin = 'PLKGHM000017');

-- 2. Kontrakt FKGHZ26 (id wyszukane po broker_ticker).
DO $$
DECLARE n INT; conflicts INT;
BEGIN
    SELECT count(*) INTO n FROM instruments WHERE broker_ticker = 'FKGHZ26' AND instrument_type = 'future';
    IF n <> 1 THEN RAISE EXCEPTION '012: oczekiwany 1 wiersz FKGHZ26 (future), jest %', n; END IF;
    SELECT count(*) INTO n FROM instruments WHERE yahoo_symbol = 'KGH.WA';
    IF n <> 1 THEN RAISE EXCEPTION '012: oczekiwany 1 instrument z yahoo_symbol KGH.WA, jest %', n; END IF;
    SELECT count(*) INTO conflicts FROM instruments
    WHERE broker_ticker = 'FKGHZ26'
      AND ((multiplier IS NOT NULL AND multiplier <> 100)
        OR (base_symbol IS NOT NULL AND base_symbol <> 'KGH.WA')
        OR (theme IS NOT NULL AND theme <> 'surowce/metale'));
    IF conflicts > 0 THEN RAISE EXCEPTION '012: FKGHZ26 ma w bazie inny mnoznik/baze/temat'; END IF;
END $$;

UPDATE instruments
SET base_symbol = 'KGH.WA',
    multiplier = 100,
    multiplier_source = 'GPW STD - lista mnożników klas (FKGH=100, brak wyjątku seryjnego), karta serii 404, potwierdzone rozliczeniem brokera; odczyt 2026-09-30',
    theme = 'surowce/metale'
WHERE broker_ticker = 'FKGHZ26' AND instrument_type = 'future';

-- 3. Archetyp kontraktu (mechanizm seed_archetypes_v2 + 006): A0, secondary = archetyp bazy.
INSERT INTO archetype_assignments
    (instrument_id, archetype, archetype_secondary, answers, resolution, key_version,
     source_tags, archetype_key, valid_from, reason)
SELECT i.id, 'A0', 'A9', 'T--------', 'q1', 'v2', 'Z', 'A0', DATE '2026-09-30',
       'future - outside §14; base KGH.WA capex/rev 15.3 %; decyzja ownera 2026-09-30'
FROM instruments i
WHERE i.broker_ticker = 'FKGHZ26' AND i.instrument_type = 'future'
ON CONFLICT (instrument_id, valid_from) DO NOTHING;

COMMIT;

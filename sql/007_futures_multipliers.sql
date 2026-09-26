-- K4b (brief CC-R): mnożniki serii kontraktów terminowych na akcje GPW ze
-- specyfikacji GPW (dane publiczne, nie dane ownera). Zastępuje heurystykę
-- "potęga 10" z K4 — `risk.kontraktowy_account_value` wymaga mnożnika na każdym
-- wierszu kupna/sprzedaży i bez niego zgłasza błąd z nazwą serii.
-- Idempotentny. Źródła odczytane 2026-09-26.
--
-- Źródła:
--   STD  = https://www.gpw.pl/standard-kontraktow-terminowych-na-akcje-spolek
--          (tabela "Lista kontraktów akcyjnych wraz z liczbą akcji przypadającą
--          na jeden kontrakt"; mnożnik bieżący, brak komunikatu o zmianie
--          w okresie obrotu serii)
--   DNP  = komunikaty GPW z 21.07.2025 (cmn_id=117129) i 30.07.2025
--          (cmn_id=117158): split DNP 1:10, od sesji 31.07.2025 mnożnik serii
--          FDNPU25/FDNPZ25/FDNPH26 zmieniony ze 100 na 1000; serie wprowadzone
--          później mają mnożnik standardowy 100 (STD)
--   LPP  = komunikat GPW z 17.04.2025 (cmn_id=116819): zmiana mnożnika tylko
--          dla FLPPM25/FLPPU25, FLPPZ25 bez zmiany -> mnożnik standardowy 1 (STD)

BEGIN;

ALTER TABLE instruments ADD COLUMN IF NOT EXISTS multiplier_source TEXT;

CREATE TEMP TABLE _k4b_multipliers (broker_ticker TEXT PRIMARY KEY, multiplier NUMERIC NOT NULL, source TEXT NOT NULL) ON COMMIT DROP;

INSERT INTO _k4b_multipliers (broker_ticker, multiplier, source) VALUES
    ('FCDRZ25', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FXTBZ25', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FACPZ25', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FDNPZ25', 1000, 'GPW komunikat 30.07.2025 cmn_id=117158 (split DNP 1:10), odczyt 2026-09-26'),
    ('FLPPZ25', 1,    'GPW STD + komunikat 17.04.2025 cmn_id=116819 (FLPPZ25 bez zmiany), odczyt 2026-09-26'),
    ('FACPH26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FKGHH26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FDNPH26', 1000, 'GPW komunikat 30.07.2025 cmn_id=117158 (split DNP 1:10), odczyt 2026-09-26'),
    ('FCDRH26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FALEH26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FCPSM26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FCDRM26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FDNPM26', 100,  'GPW STD (seria po splicie DNP), odczyt 2026-09-26'),
    ('FACPM26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FALEM26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FMDVM26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FKGHM26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FKGHU26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FACPU26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FCDRU26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FMDVU26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FALEU26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FPGEU26', 1000, 'GPW STD, odczyt 2026-09-26'),
    ('FDNPU26', 100,  'GPW STD (seria po splicie DNP), odczyt 2026-09-26'),
    -- serie otwarte: mnożnik już w bazie, tu tylko źródło (wartość musi się zgadzać)
    ('FCDRZ26', 100,  'GPW STD, odczyt 2026-09-26'),
    ('FPGEZ26', 1000, 'GPW STD, odczyt 2026-09-26'),
    ('FMDVZ26', 100,  'GPW STD, odczyt 2026-09-26');

-- Nie nadpisuj innego, już zapisanego mnożnika — rozbieżność = błąd migracji.
DO $$
DECLARE conflicts INT; missing INT;
BEGIN
    SELECT count(*) INTO conflicts FROM instruments i JOIN _k4b_multipliers m USING (broker_ticker)
    WHERE i.multiplier IS NOT NULL AND i.multiplier <> m.multiplier;
    IF conflicts > 0 THEN RAISE EXCEPTION '007: % serii ma w bazie inny mnoznik niz specyfikacja GPW', conflicts; END IF;
    SELECT count(*) INTO missing FROM _k4b_multipliers m
    WHERE NOT EXISTS (SELECT 1 FROM instruments i WHERE i.broker_ticker = m.broker_ticker);
    IF missing > 0 THEN RAISE EXCEPTION '007: % serii nie ma w instruments', missing; END IF;
END $$;

UPDATE instruments i
SET multiplier = m.multiplier, multiplier_source = m.source
FROM _k4b_multipliers m
WHERE i.broker_ticker = m.broker_ticker;

COMMIT;

-- Mannaz — poprawka P2.3/P2.5 (brief CC-P): kolumny źródła zdarzeń korporacyjnych.
-- Idempotentny: bezpieczny do wielokrotnego uruchomienia.
--
-- Kontekst: corporate_events do tej pory zasilane było wyłącznie parserem
-- tytułów (import_history.py — 'wykup_certyfikatow' / 'zamiana_akcji'). Ten
-- etap dodaje dwa kolejne, niezależne mechanizmy detekcji splitów (patrz
-- src/mannaz/corp_actions.py):
--   'yfinance_splits'       — bezpośredni import z yfinance Ticker.splits
--                             (znana data i współczynnik z API).
--   'price_ratio_detector'  — wykryte z ilorazu cena_brokera / close_yahoo,
--                             gdy yfinance nie ma zarejestrowanego splitu
--                             (retroaktywna korekta bez śladu w API, np. SPYI).
--
-- date_source dotyczy WYŁĄCZNIE wierszy source='price_ratio_detector' — mówi,
-- skąd wzięła się `event_date`, skoro sam detektor cenowy nie zna dat:
--   'yfinance_splits'    — znaleziono pasujący split w pełnej historii yfinance
--                          (Ticker.splits), użyto jego daty.
--   'inferred_boundary'  — brak dopasowania w yfinance; data = pierwsza
--                          transakcja, w której iloraz wraca do ~1 (granica
--                          przed/po realnym zdarzeniu).
--   'inferred_all'       — brak dopasowania w yfinance I brak przejścia do ~1
--                          w żadnej transakcji (zdarzenie zastosowane do
--                          wszystkich transakcji instrumentu).
-- NULL dla source IN ('title_parser', 'yfinance_splits').

BEGIN;

ALTER TABLE corporate_events
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'title_parser';

ALTER TABLE corporate_events
    ADD COLUMN IF NOT EXISTS date_source TEXT;

ALTER TABLE corporate_events
    DROP CONSTRAINT IF EXISTS corporate_events_source_check;

ALTER TABLE corporate_events
    ADD CONSTRAINT corporate_events_source_check
    CHECK (source IN ('title_parser', 'yfinance_splits', 'price_ratio_detector'));

ALTER TABLE corporate_events
    DROP CONSTRAINT IF EXISTS corporate_events_date_source_check;

ALTER TABLE corporate_events
    ADD CONSTRAINT corporate_events_date_source_check
    CHECK (date_source IS NULL OR date_source IN ('yfinance_splits', 'inferred_boundary', 'inferred_all'));

COMMIT;

-- Mannaz — brief CC-C, C2: rejestr plików financeHistory*.csv przetworzonych
-- PRZEZ CYKL (mannaz.cycle) — świadomie ODDZIELNY od `source_runs`, który już
-- zawiera 7 plików financeHistory zaimportowanych PRZED uruchomieniem cyklu
-- (patrz brief CC-C, mapa istniejącego kodu, pkt 0). `source_runs` nie jest
-- rejestrem cyklu: `processed_history_files` prowadzi WYŁĄCZNIE cykl, bez
-- backfillu z `source_runs` (decyzja) — plik zaimportowany wcześniej,
-- ponownie wrzucony do `_incoming`, przechodzi normalną deduplikację po
-- krotce transakcji (UNIQUE w `transactions`), nie po tej tabeli.
--
-- Idempotentna (CREATE TABLE IF NOT EXISTS), styl jak sql/009.

BEGIN;

CREATE TABLE IF NOT EXISTS processed_history_files (
    sha256              TEXT PRIMARY KEY,
    original_name       TEXT NOT NULL,
    stored_path         TEXT NOT NULL,          -- ścieżka w raw_archive_dir po przeniesieniu
    first_date          DATE,                   -- min(transaction_date) w pliku
    last_date           DATE,                   -- max(transaction_date) w pliku
    rows_total          INTEGER NOT NULL,
    rows_new            INTEGER NOT NULL,
    rows_duplicate      INTEGER NOT NULL,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;

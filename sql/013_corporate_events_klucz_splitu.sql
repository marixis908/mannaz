-- Mannaz — brief CC-B45 (B-45): klucz tożsamości splitu w corporate_events.
-- Dotychczasowy UNIQUE (broker_ticker, event_date, event_type, title_raw) nie
-- łapie duplikatu, gdy zmienia się title_raw (detektor z ilorazu wpisuje w nim
-- median_ratio i n próbek, np. SPYI n=8 vs n=9) — to samo zdarzenie lądowało
-- w tabeli wielokrotnie. Reguła nadzorcy: split/reverse_split jest znany po
-- (instrument_id, event_date, event_type). Częściowy indeks unikalny tylko
-- dla tych dwóch typów; wymiany i wykupy (title_parser) bez zmian.
-- Idempotentny (IF NOT EXISTS). `_insert_event` używa ON CONFLICT na tym
-- indeksie. UWAGA: tworzenie indeksu nie powiedzie się, gdy w tabeli już są
-- duplikaty klucza — wtedy najpierw porządkowanie (decyzja ownera), nie ta migracja.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS corporate_events_split_klucz_uq
    ON corporate_events (instrument_id, event_date, event_type)
    WHERE event_type IN ('split', 'reverse_split');

COMMIT;

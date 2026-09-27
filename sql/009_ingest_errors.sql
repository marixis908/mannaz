-- Mannaz — brief CC-I (B-21), I5: tabela `ingest_errors` (T12 circuit
-- breaker: "zero wierszy albo same NaN -> nie zapisuj, zaloguj do
-- ingest_errors"; dokument §8.2, wiersz `ingest_errors` = zdarzenie ->
-- źródło, instrument, typ błędu, treść, czy zdegradował stan, append).
-- Append-only: importer (`mannaz.prices.run_prices_fetch`) wyłącznie
-- wstawia (INSERT), nigdy nie robi UPDATE/DELETE na tej tabeli.
--
-- degraded_state — kolumna z modelu dokumentu ("czy zdegradował stan").
-- Importer zapisuje tu zawsze FALSE: o degradacji stanu (np. forward-fill/
-- stale wg T27, brief CC-U) decyduje warstwa odczytu (`risk_daily`), nie ten
-- importer — importer loguje wyłącznie fakt odrzucenia wiersza/odpowiedzi.
--
-- Idempotentna (CREATE TABLE IF NOT EXISTS), styl jak sql/008.

BEGIN;

CREATE TABLE IF NOT EXISTS ingest_errors (
    id                  BIGSERIAL PRIMARY KEY,
    source              TEXT NOT NULL,          -- np. "yahoo"
    instrument_id       BIGINT REFERENCES instruments(id),
    price_date          DATE,                   -- NULL dla błędów całej odpowiedzi (np. empty_response)
    error_type          TEXT NOT NULL,          -- np. "row_missing_price", "empty_response"
    detail              TEXT,
    degraded_state       BOOLEAN NOT NULL DEFAULT FALSE,
    run_started_at       TIMESTAMPTZ NOT NULL,   -- jeden znacznik na cały przebieg run_prices_fetch
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ingest_errors_source_run_idx ON ingest_errors (source, run_started_at);

COMMIT;

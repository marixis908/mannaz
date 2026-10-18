-- Konsensus Yahoo, T48/M93 (rew. 4.22). Migracja bez danych i bez pobran HTTP.
BEGIN;

CREATE TABLE IF NOT EXISTS estimate_snapshots (
    id BIGSERIAL PRIMARY KEY,
    instrument_id BIGINT NOT NULL REFERENCES instruments(id),
    source TEXT NOT NULL,
    source_symbol TEXT NOT NULL,
    fetched_at_utc TIMESTAMPTZ NOT NULL,
    modules TEXT[] NOT NULL,
    query_params JSONB NOT NULL,
    library TEXT NOT NULL,
    library_version TEXT NOT NULL,
    http_status INTEGER,
    error TEXT,
    raw_ref TEXT,
    raw_sha256 TEXT,
    byte_count BIGINT,
    target_mean NUMERIC,
    price_currency TEXT,
    quality_flag TEXT,
    parse_status TEXT,
    skipped_labels TEXT[],
    code_sha TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (instrument_id, source, fetched_at_utc),
    CHECK ((raw_ref IS NULL) = (raw_sha256 IS NULL))
);

CREATE TABLE IF NOT EXISTS estimates (
    id BIGSERIAL PRIMARY KEY,
    snapshot_id BIGINT NOT NULL REFERENCES estimate_snapshots(id),
    period TEXT NOT NULL,
    period_type TEXT NOT NULL CHECK (period_type IN ('quarter', 'year')),
    period_end DATE,
    fiscal_year INTEGER,
    eps_avg NUMERIC,
    eps_low NUMERIC,
    eps_high NUMERIC,
    n_analysts_eps INTEGER,
    rev_avg NUMERIC,
    rev_low NUMERIC,
    rev_high NUMERIC,
    n_analysts_rev INTEGER,
    eps_currency TEXT,
    rev_currency TEXT,
    eps_status TEXT,
    rev_status TEXT,
    period_end_check TEXT NOT NULL,
    currency_check TEXT,
    status TEXT,
    UNIQUE (snapshot_id, period)
);

CREATE OR REPLACE FUNCTION estimates_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'konsensus (%) tylko do dopisywania: % odrzucony', TG_TABLE_NAME, TG_OP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_estimate_snapshots_append_only'
                   AND tgrelid = 'estimate_snapshots'::regclass) THEN
        CREATE TRIGGER trg_estimate_snapshots_append_only
            BEFORE UPDATE OR DELETE ON estimate_snapshots
            FOR EACH ROW EXECUTE FUNCTION estimates_append_only();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_estimates_append_only'
                   AND tgrelid = 'estimates'::regclass) THEN
        CREATE TRIGGER trg_estimates_append_only
            BEFORE UPDATE OR DELETE ON estimates
            FOR EACH ROW EXECUTE FUNCTION estimates_append_only();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_estimate_snapshots_no_truncate'
                   AND tgrelid = 'estimate_snapshots'::regclass) THEN
        CREATE TRIGGER trg_estimate_snapshots_no_truncate
            BEFORE TRUNCATE ON estimate_snapshots
            FOR EACH STATEMENT EXECUTE FUNCTION estimates_append_only();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_estimates_no_truncate'
                   AND tgrelid = 'estimates'::regclass) THEN
        CREATE TRIGGER trg_estimates_no_truncate
            BEFORE TRUNCATE ON estimates
            FOR EACH STATEMENT EXECUTE FUNCTION estimates_append_only();
    END IF;
END $$;

COMMIT;

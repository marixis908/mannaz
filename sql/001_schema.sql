-- Mannaz — schemat bazowy (ETAP P2)
-- Idempotentny: bezpieczny do wielokrotnego uruchomienia (CREATE ... IF NOT EXISTS).
-- Zakres tabel per brief CC-P P2.2. Pola dobrane pod kątem §8.2, §12, §19
-- projekt-systemu-monitoringu.md, ale nazwy tabel/kolumn są wersją uproszczoną
-- dla etapu "przekrój brokerski" (feat/przekroj-brk), nie 1:1 z docelowym modelem z §8.

BEGIN;

-- ---------------------------------------------------------------------------
-- instruments
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS instruments (
    id                  BIGSERIAL PRIMARY KEY,
    broker_ticker       TEXT NOT NULL,          -- ticker jak w tytule transakcji brokera (np. "APH", "GMT")
    name                TEXT,                   -- pełna nazwa emitenta/instrumentu z tytułu
    isin                TEXT,                   -- ISIN, jeśli obecny w tytule
    yahoo_symbol        TEXT,                   -- do uzupełnienia później (P3+), NULL na tym etapie
    currency            TEXT NOT NULL,          -- waluta notowania/rozliczenia instrumentu
    exchange            TEXT,                   -- giełda / MIC, jeśli znany
    instrument_type     TEXT NOT NULL DEFAULT 'equity'
                        CHECK (instrument_type IN ('equity', 'etf', 'future', 'certificate', 'unknown')),
    multiplier          NUMERIC,                -- mnożnik kontraktu (futures), NULL dla equity/etf
    contract_expiry     DATE,                   -- data wygaśnięcia kontraktu (futures), NULL dla equity/etf
    base_symbol         TEXT,                   -- bazowy symbol dla instrumentów pochodnych (np. "KGH" dla FKGHU25)
    theme               TEXT,                   -- tag tematyczny, do ręcznego uzupełnienia
    is_core             BOOLEAN NOT NULL DEFAULT FALSE, -- SPYI / V80A / V60A = true
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Naturalny klucz instrumentu: ISIN gdy znany, inaczej broker_ticker. To samo ISIN
-- może pojawić się z różnymi tickerami/nazwami w tytułach (np. ADR notowany w PLN i
-- w EUR) — to wciąż jeden instrument, waluta rozliczenia żyje w transactions/positions,
-- nie w instruments.
CREATE UNIQUE INDEX IF NOT EXISTS uq_instruments_isin
    ON instruments (isin) WHERE isin IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_instruments_ticker_no_isin
    ON instruments (broker_ticker) WHERE isin IS NULL;

-- ---------------------------------------------------------------------------
-- transactions
-- Ziarno: pojedynczy wiersz z historii przepływów gotówki brokera.
-- Dedup między plikami po pełnej krotce (data, rachunek, waluta, tytuł, wartość);
-- occurrence_no odróżnia prawdziwe duplikaty WEWNĄTRZ jednego pliku (patrz P2.4).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id                  BIGSERIAL PRIMARY KEY,
    transaction_date    DATE NOT NULL,          -- kolumna "Data" ze źródła
    rachunek            TEXT NOT NULL,          -- kolumna "Rachunek" ze źródła (np. "AKCYJNY 000001")
    currency            TEXT NOT NULL,          -- kolumna "Waluta" ze źródła (waluta rozliczenia wiersza)
    title_raw           TEXT NOT NULL,          -- kolumna "Tytuł przelewu"/"Tytuł operacji" ze źródła, bez zmian
    amount              NUMERIC NOT NULL,       -- kolumna "Wartość" ze źródła, znak zgodny ze źródłem
    occurrence_no       INTEGER NOT NULL DEFAULT 1, -- numer wystąpienia identycznej krotki WEWNĄTRZ jednego pliku źródłowego
    row_type            TEXT NOT NULL DEFAULT 'unknown',
                        -- kupno / sprzedaz / dywidenda_netto / dywidenda_brutto / podatek_dywidenda /
                        -- oplata_transakcyjna / oplata_rachunek / oplata_przechowanie / depozyt_dopłata /
                        -- depozyt_zwrot / prowizja_wygasniecie / przelew_wewnetrzny / przelew_zewnetrzny /
                        -- wykup_certyfikatow / zamiana_akcji / unknown
    instrument_id       BIGINT REFERENCES instruments(id), -- NULL dla wierszy bez instrumentu (opłaty, przelewy)
    qty                 NUMERIC,                -- ilość, jeśli obecna w tytule (kupno/sprzedaż)
    price               NUMERIC,                -- cena jednostkowa, jeśli obecna w tytule
    broker_order_no     TEXT,                   -- "nr Z..." / "nr ..." ze źródła, jeśli obecny
    source_file         TEXT NOT NULL,           -- nazwa pliku źródłowego (bez ścieżki)
    source_sha256       TEXT NOT NULL,           -- sha256 CAŁEGO pliku źródłowego, z którego wiersz pochodzi
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (transaction_date, rachunek, currency, title_raw, amount, occurrence_no)
);

CREATE INDEX IF NOT EXISTS idx_transactions_instrument ON transactions (instrument_id, transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_rachunek ON transactions (rachunek, transaction_date);

-- ---------------------------------------------------------------------------
-- corporate_events
-- Zdarzenia korporacyjne wykryte z tytułów transakcji (split/scalenie/zamiana/wykup).
-- ratio = liczba jednostek PO zdarzeniu przypadająca na 1 jednostkę PRZED zdarzeniem
-- (np. split 25:1 -> ratio = 25; scalenie 1:2 -> ratio = 0.5). NULL gdy tytuł nie
-- pozwala jednoznacznie wyliczyć współczynnika (wymaga ręcznej weryfikacji).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corporate_events (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       BIGINT REFERENCES instruments(id),
    broker_ticker       TEXT NOT NULL,          -- ticker jak w tytule źródłowym, na wypadek braku dopasowania do instruments
    event_date          DATE NOT NULL,
    event_type          TEXT NOT NULL
                        CHECK (event_type IN ('split', 'reverse_split', 'share_exchange', 'certificate_redemption', 'other')),
    ratio               NUMERIC,                -- patrz komentarz tabeli; NULL jeśli nierozstrzygalne z samego tytułu
    source_transaction_id BIGINT REFERENCES transactions(id),
    title_raw           TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (broker_ticker, event_date, event_type, title_raw)
);

-- ---------------------------------------------------------------------------
-- positions_fifo
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions_fifo (
    id                  BIGSERIAL PRIMARY KEY,
    rachunek            TEXT NOT NULL,
    instrument_id       BIGINT NOT NULL REFERENCES instruments(id),
    currency            TEXT NOT NULL,          -- waluta rozliczenia pozycji
    qty                 NUMERIC NOT NULL,        -- ilość otwarta (po FIFO); może być ujemna (short) — patrz P2.5
    residual_cost       NUMERIC NOT NULL,        -- koszt rezydualny otwartej ilości, w walucie rozliczenia
    first_entry_date    DATE NOT NULL,
    last_entry_date     DATE NOT NULL,
    entry_max_high      NUMERIC,                -- NULL do P3 (wymaga danych cenowych)
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rachunek, instrument_id, currency)
);

-- ---------------------------------------------------------------------------
-- prices_daily
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS prices_daily (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       BIGINT NOT NULL REFERENCES instruments(id),
    price_date          DATE NOT NULL,
    currency             TEXT NOT NULL,
    source              TEXT NOT NULL,          -- np. "yahoo"
    open_raw            NUMERIC,
    high_raw            NUMERIC,
    low_raw              NUMERIC,
    close_raw           NUMERIC,
    volume_raw          NUMERIC,
    open_split_adj       NUMERIC,
    high_split_adj      NUMERIC,
    low_split_adj       NUMERIC,
    close_split_adj     NUMERIC,
    volume_split_adj    NUMERIC,
    adjustment_convention TEXT NOT NULL DEFAULT 'unknown',
                        -- opis konwencji dostosowania (np. "yahoo_auto_adjust", "manual_corporate_events")
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (instrument_id, price_date, source)
);

-- ---------------------------------------------------------------------------
-- fx_nbp
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fx_nbp (
    id                  BIGSERIAL PRIMARY KEY,
    pair                TEXT NOT NULL,          -- np. "USD/PLN"
    rate_date           DATE NOT NULL,
    fixing_id           TEXT NOT NULL DEFAULT 'nbp_a',
    rate                NUMERIC NOT NULL,
    source              TEXT NOT NULL DEFAULT 'nbp',
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (pair, rate_date, fixing_id)
);

-- ---------------------------------------------------------------------------
-- risk_daily
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_daily (
    id                  BIGSERIAL PRIMARY KEY,
    rachunek            TEXT NOT NULL,
    instrument_id       BIGINT NOT NULL REFERENCES instruments(id),
    risk_date           DATE NOT NULL,
    atr20               NUMERIC,
    atr22               NUMERIC,
    sma200              NUMERIC,
    chandelier_stop      NUMERIC,
    two_n_stop          NUMERIC,
    risk_state          TEXT,                   -- np. HIGH/NORMAL/LOW (patrz §19)
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rachunek, instrument_id, risk_date)
);

-- ---------------------------------------------------------------------------
-- archetype_assignments
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS archetype_assignments (
    id                  BIGSERIAL PRIMARY KEY,
    instrument_id       BIGINT NOT NULL REFERENCES instruments(id),
    archetype           TEXT NOT NULL,          -- patrz §14
    valid_from          DATE NOT NULL,
    valid_to            DATE,
    reason              TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (instrument_id, valid_from)
);

-- ---------------------------------------------------------------------------
-- source_runs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_runs (
    id                  BIGSERIAL PRIMARY KEY,
    source              TEXT NOT NULL,          -- np. nazwa pliku importu / źródła
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_count           INTEGER NOT NULL,
    sha256_input        TEXT NOT NULL,
    UNIQUE (source, sha256_input)
);

COMMIT;

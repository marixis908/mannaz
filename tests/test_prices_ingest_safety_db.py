"""Testy bazodanowe I2-I6 (brief CC-I, B-21) — bezpiecznik importera cen wg
T12 (dokument §T12: "zero wierszy albo same NaN -> nie zapisuj, zaloguj do
ingest_errors") i M67 (§23.2: "dane sesji D są odświeżane w oknie D-5"). Każdy
test w JEDNEJ transakcji zakończonej `conn.rollback()` w `finally` — zero
trwałych zmian w bazie (wzorzec `tests/test_risk_pit_db.py::db_conn`).
Pomijane (skip), gdy `mannaz.db.get_connection()` zawiedzie albo brak
odpowiedniego instrumentu testowego w bieżącej bazie. Dane syntetyczne
wyłącznie: `fetch_ohlc`/`fetch_currency` podmienione (monkeypatch), ceny z
datami 1990-01-0x (poza realnym zakresem importu), zero ilości/kosztów/
rachunków ownera."""

from datetime import date
from decimal import Decimal

import pytest

from mannaz.db import get_connection
from mannaz.prices import MissingIngestErrorsTableError, OhlcRow, run_prices_fetch

# sql/009_ingest_errors.sql zduplikowane tu ręcznie (nie parsujemy pliku) —
# ten sam wzorzec co `tests/test_risk_pit_db.py::db_conn` dla sql/008: plik
# migracji ma własne BEGIN/COMMIT, których nie można wykonać wewnątrz już
# otwartej transakcji testowej bez przedwczesnego COMMIT (utrata izolacji
# rollbackiem).
_INGEST_ERRORS_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS ingest_errors (
    id                  BIGSERIAL PRIMARY KEY,
    source              TEXT NOT NULL,
    instrument_id       BIGINT REFERENCES instruments(id),
    price_date          DATE,
    error_type          TEXT NOT NULL,
    detail              TEXT,
    degraded_state       BOOLEAN NOT NULL DEFAULT FALSE,
    run_started_at       TIMESTAMPTZ NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_INGEST_ERRORS_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS ingest_errors_source_run_idx ON ingest_errors (source, run_started_at)"
)

# Daty syntetyczne z dalekiej przeszłości — zero kolizji z realnym importem.
D_PREV = date(1990, 1, 2)
D0 = date(1990, 1, 3)


def _row(price_date: date, high="11", low="9", close="10.5") -> OhlcRow:
    return OhlcRow(
        price_date=price_date,
        open_split_adj=Decimal("10"),
        high_split_adj=Decimal(high) if high is not None else None,
        low_split_adj=Decimal(low) if low is not None else None,
        close_split_adj=Decimal(close) if close is not None else None,
        volume_split_adj=Decimal("1000"),
        adj_close_total_return=Decimal(close) if close is not None else None,
    )


@pytest.fixture
def db_conn():
    """I5 (brief CC-I): tabela `ingest_errors` (sql/009) — jeśli nie jest
    jeszcze zastosowana w bazie, aplikujemy jej treść w TEJ SAMEJ transakcji
    przed testem (DDL w Postgresie jest transakcyjny), żeby
    `run_prices_fetch` mogło logować do niej w testach. Po teście: rollback +
    weryfikacja przez `to_regclass`, że istnienie tabeli jest takie samo jak
    przed testem (zero trwałych zmian niezależnie od tego, czy 009 była już
    zastosowana w realnej bazie)."""
    try:
        conn = get_connection()
    except Exception as exc:  # brak .env/hasla/serwera -> caly test pomijamy
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return

    def _ingest_errors_exists() -> bool:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('ingest_errors')")
            return cur.fetchone()[0] is not None

    existed_before = _ingest_errors_exists()
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(_INGEST_ERRORS_TABLE_DDL)
        cur.execute(_INGEST_ERRORS_INDEX_DDL)
    try:
        yield conn
    finally:
        conn.rollback()  # obronnie — testy robia wlasny rollback, ale gdyby nie zrobily
        existed_after = _ingest_errors_exists()
        conn.close()
        assert existed_after == existed_before, (
            "I5 (brief CC-I): stan tabeli ingest_errors zmienil sie trwale w tescie — "
            f"przed: {existed_before}, po: {existed_after}"
        )


@pytest.fixture
def test_instrument(db_conn):
    """Istniejący instrument z `yahoo_symbol` i BEZ zdarzeń splitowych ze
    znanym `ratio` (raw == split_adj wprost, bez mnożnika
    `cumulative_ratio_after`) — używany BEZ zmiany jego własnych danych
    (tylko odczyt id/yahoo_symbol/currency; wiersze `prices_daily`/
    `ingest_errors` wstawiane/usuwane w tej samej transakcji testowej mają
    datę 1990-01-0x)."""
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.yahoo_symbol, i.currency FROM instruments i
            WHERE i.yahoo_symbol IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM corporate_events c
                  WHERE c.instrument_id = i.id AND c.ratio IS NOT NULL
              )
            ORDER BY i.id
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("brak instrumentu bez zdarzen splitowych w biezacej bazie testowej")
    return {"id": row[0], "yahoo_symbol": row[1], "currency": row[2]}


def _clean_synthetic_rows(conn, instrument_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM prices_daily WHERE instrument_id = %s AND price_date IN (%s, %s)",
            (instrument_id, D_PREV, D0),
        )
        cur.execute("DELETE FROM ingest_errors WHERE instrument_id = %s", (instrument_id,))


# ---------------------------------------------------------------------------
# I6 (1) — jeden wiersz NaN w close: ten wiersz nie zapisany, reszta tak,
# dokładnie jeden wpis w logu.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_i6_1_one_nan_close_row_rejected_others_saved_one_log_entry(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])

        good_row = _row(D_PREV)
        bad_row = _row(D0, close=None)

        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: [good_row, bad_row])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])

        summary = run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False)

        assert len(summary.results) == 1
        result = summary.results[0]
        assert result.rows_fetched == 2
        assert result.rows_inserted == 1
        assert result.rows_rejected == 1
        assert result.status == "rows_rejected"
        assert summary.rows_rejected_total == 1
        assert summary.instruments_with_rejected_rows == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT price_date, close_split_adj FROM prices_daily "
                "WHERE instrument_id = %s AND price_date IN (%s, %s)",
                (inst["id"], D_PREV, D0),
            )
            saved = {r[0]: r[1] for r in cur.fetchall()}
        assert saved == {D_PREV: Decimal("10.5")}

        with conn.cursor() as cur:
            cur.execute(
                "SELECT error_type, price_date, detail FROM ingest_errors WHERE instrument_id = %s",
                (inst["id"],),
            )
            logs = cur.fetchall()
        assert len(logs) == 1
        assert logs[0][0] == "row_missing_price"
        assert logs[0][1] == D0
        assert "close_split_adj" in logs[0][2]
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# I6 (2) — pusta odpowiedź: zero zapisu, jeden wpis 'empty_response'.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_i6_2_empty_response_zero_writes_one_log_entry(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])

        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: [])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])

        summary = run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False)

        result = summary.results[0]
        assert result.rows_fetched == 0
        assert result.rows_inserted == 0
        assert result.status == "empty_response"
        assert summary.instruments_empty_response == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM prices_daily WHERE instrument_id = %s AND price_date IN (%s, %s)",
                (inst["id"], D_PREV, D0),
            )
            assert cur.fetchone()[0] == 0

            cur.execute(
                "SELECT error_type FROM ingest_errors WHERE instrument_id = %s",
                (inst["id"],),
            )
            logs = cur.fetchall()
        assert len(logs) == 1
        assert logs[0][0] == "empty_response"
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# I6 (3) — same wiersze NaN: jak pusta odpowiedź (zero zapisu, jeden wpis).
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_i6_3_all_rows_nan_treated_as_empty_response(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])

        rows = [_row(D_PREV, close=None), _row(D0, high=None)]
        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: rows)
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])

        summary = run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False)

        result = summary.results[0]
        assert result.rows_fetched == 2
        assert result.rows_rejected == 2
        assert result.rows_inserted == 0
        assert result.status == "empty_response"
        assert summary.instruments_empty_response == 1
        assert summary.rows_rejected_total == 2

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM prices_daily WHERE instrument_id = %s AND price_date IN (%s, %s)",
                (inst["id"], D_PREV, D0),
            )
            assert cur.fetchone()[0] == 0

            cur.execute(
                "SELECT error_type, detail FROM ingest_errors WHERE instrument_id = %s",
                (inst["id"],),
            )
            logs = cur.fetchall()
        # I3 [S]: JEDEN wpis 'empty_response' z liczbą odrzuconych w treści,
        # nie N wpisów 'row_missing_price'.
        assert len(logs) == 1
        assert logs[0][0] == "empty_response"
        assert "2" in logs[0][1]
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# I6 (4) — pełny wiersz istniejący w bazie + nowa odpowiedź z NaN na tę datę
# → wiersz w bazie bez zmian (wszystkie kolumny cen i fetched_at).
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_i6_4_existing_full_row_untouched_by_later_nan_response(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO prices_daily (
                    instrument_id, price_date, currency, source,
                    open_raw, high_raw, low_raw, close_raw, volume_raw,
                    open_split_adj, high_split_adj, low_split_adj,
                    close_split_adj, volume_split_adj, adjustment_convention
                ) VALUES (%s, %s, %s, 'yahoo', %s, %s, %s, %s, NULL,
                          %s, %s, %s, %s, %s, %s)
                RETURNING open_raw, high_raw, low_raw, close_raw,
                          open_split_adj, high_split_adj, low_split_adj,
                          close_split_adj, volume_split_adj, adjustment_convention, fetched_at
                """,
                (
                    inst["id"], D0, inst["currency"],
                    Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"),
                    Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"),
                    Decimal("1000"), "yahoo_split_adjusted",
                ),
            )
            before = cur.fetchone()

        bad_row = _row(D0, close=None)
        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: [bad_row])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])

        run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D0, end=D0, commit=False)

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT open_raw, high_raw, low_raw, close_raw,
                       open_split_adj, high_split_adj, low_split_adj,
                       close_split_adj, volume_split_adj, adjustment_convention, fetched_at
                FROM prices_daily WHERE instrument_id = %s AND price_date = %s AND source = 'yahoo'
                """,
                (inst["id"], D0),
            )
            after = cur.fetchone()
        assert after == before
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# I6 (5) — samonaprawa: przebieg 1 z NaN na D (brak wiersza D, wpis w logu),
# przebieg 2 z pełną ceną na D → wiersz D obecny i pełny.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_i6_5_self_healing_second_run_with_full_price_fills_row(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])

        # przebieg 1: odpowiedź NIE jest wyłącznie odrzucona (D_PREV OK obok
        # złego D0) — dzięki temu D0 loguje się jako 'row_missing_price' z
        # WŁASNĄ price_date, a nie zwija się do zbiorczego 'empty_response'
        # bez daty (I3 [S] — patrz test_i6_3_...).
        run1_rows = [_row(D_PREV), _row(D0, close=None)]
        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: run1_rows)
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])
        run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM prices_daily WHERE instrument_id = %s AND price_date = %s",
                (inst["id"], D0),
            )
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT count(*) FROM ingest_errors WHERE instrument_id = %s AND price_date = %s "
                "AND error_type = 'row_missing_price'",
                (inst["id"], D0),
            )
            assert cur.fetchone()[0] == 1

        # przebieg 2 (samonaprawa, I4): pełna cena na D0 -> wiersz obecny i pełny.
        good_row = _row(D0)
        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: [good_row])
        run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D0, end=D0, commit=False)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT close_split_adj FROM prices_daily WHERE instrument_id = %s AND price_date = %s",
                (inst["id"], D0),
            )
            row = cur.fetchone()
        assert row is not None
        assert row[0] == Decimal("10.5")
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# I6 (6) — przebieg z kompletem: wiersze jak w starej logice (te same
# wartości kolumn), zero wpisów w logu.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_i6_6_full_response_saved_as_before_zero_log_entries(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])

        rows = [_row(D_PREV), _row(D0)]
        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: rows)
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])

        summary = run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False)

        result = summary.results[0]
        assert result.rows_fetched == 2
        assert result.rows_inserted == 2
        assert result.rows_rejected == 0
        assert result.status == "ok"
        assert summary.instruments_ok == 1

        with conn.cursor() as cur:
            cur.execute(
                "SELECT price_date, open_split_adj, high_split_adj, low_split_adj, "
                "close_split_adj, volume_split_adj FROM prices_daily "
                "WHERE instrument_id = %s AND price_date IN (%s, %s) ORDER BY price_date",
                (inst["id"], D_PREV, D0),
            )
            saved = cur.fetchall()
        assert saved == [
            (D_PREV, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"), Decimal("1000")),
            (D0, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"), Decimal("1000")),
        ]

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM ingest_errors WHERE instrument_id = %s", (inst["id"],))
            assert cur.fetchone()[0] == 0
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# Dodatkowo (brief CC-I, I6) — Infinity w high -> odrzucony (round-trip DB).
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_i6_extra_infinity_high_rejected_not_saved(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])

        inf_row = _row(D0)
        inf_row.high_split_adj = Decimal("Infinity")
        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: [inf_row])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])

        summary = run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D0, end=D0, commit=False)

        result = summary.results[0]
        assert result.rows_rejected == 1
        assert result.rows_inserted == 0
        assert result.status == "empty_response"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM prices_daily WHERE instrument_id = %s AND price_date = %s",
                (inst["id"], D0),
            )
            assert cur.fetchone()[0] == 0
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# I5 — brak tabeli ingest_errors: blad czytelny PRZED jakimkolwiek zapisem
# cen. Uzywa WLASNEGO polaczenia (bez fixture db_conn, ktora zawsze aplikuje
# 009 przed testem); brak tabeli symulowany w transakcji (DROP TABLE IF
# EXISTS, DDL transakcyjny — rollback w finally przywraca stan), wiec test
# nie zalezy od tego, czy migracja 009 jest juz zastosowana w bazie.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_i5_missing_table_raises_before_any_price_write(monkeypatch):
    try:
        conn = get_connection()
    except Exception as exc:
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return

    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS ingest_errors")

            cur.execute(
                "SELECT i.id, i.yahoo_symbol, i.currency FROM instruments i "
                "WHERE i.yahoo_symbol IS NOT NULL ORDER BY i.id LIMIT 1"
            )
            row = cur.fetchone()
        if row is None:
            pytest.skip("brak jakiegokolwiek zmapowanego instrumentu w biezacej bazie testowej")
        instrument_id, symbol, currency = row

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM prices_daily WHERE instrument_id = %s AND price_date = %s",
                (instrument_id, D0),
            )

        good_row = _row(D0)
        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda s, start, end: [good_row])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda s: currency)

        with pytest.raises(MissingIngestErrorsTableError):
            run_prices_fetch(conn, instrument_ids=[instrument_id], start=D0, end=D0, commit=False)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM prices_daily WHERE instrument_id = %s AND price_date = %s",
                (instrument_id, D0),
            )
            assert cur.fetchone()[0] == 0, "I5: zero zapisu cen przed rzuceniem bledu o brakujacej tabeli"
    finally:
        conn.rollback()
        conn.close()


# ---------------------------------------------------------------------------
# B-05 (Y5) — rewizje dostawcy (`provider_revision`) przy ponownym pobraniu.
# Dane syntetyczne (1990-01-0x), jedna transakcja, rollback w `finally`.
# ---------------------------------------------------------------------------


def _run_with_rows(conn, inst, monkeypatch, rows):
    monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: rows)
    monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])
    return run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False)


def _revisions(conn, instrument_id: int) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT price_date, source, detail, degraded_state FROM ingest_errors "
            "WHERE instrument_id = %s AND error_type = 'provider_revision' ORDER BY id",
            (instrument_id,),
        )
        return cur.fetchall()


def _stored(conn, instrument_id: int, price_date: date) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT open_split_adj, high_split_adj, low_split_adj, close_split_adj, volume_split_adj "
            "FROM prices_daily WHERE instrument_id = %s AND price_date = %s AND source = 'yahoo'",
            (instrument_id, price_date),
        )
        return cur.fetchone()


@pytest.mark.db
def test_b05_positive_changed_close_logs_one_revision_and_overwrites(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])
        first = _run_with_rows(conn, inst, monkeypatch, [_row(D0)])
        assert first.revisions_total == 0 and _revisions(conn, inst["id"]) == []

        second = _run_with_rows(conn, inst, monkeypatch, [_row(D0, close="11.25")])
        assert second.revisions_total == 1
        assert second.results[0].revisions == 1
        revs = _revisions(conn, inst["id"])
        assert len(revs) == 1
        assert revs[0] == (D0, "yahoo", "col=close_split_adj old=10.5 new=11.25", False)
        assert _stored(conn, inst["id"], D0)[3] == Decimal("11.25")
    finally:
        conn.rollback()


@pytest.mark.db
def test_b05_multiple_columns_detail_order_open_high_low_close(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])
        _run_with_rows(conn, inst, monkeypatch, [_row(D0)])
        _run_with_rows(conn, inst, monkeypatch, [_row(D0, high="12", close="11.25")])
        revs = _revisions(conn, inst["id"])
        assert [r[2] for r in revs] == ["col=high_split_adj old=11 new=12; col=close_split_adj old=10.5 new=11.25"]
    finally:
        conn.rollback()


@pytest.mark.db
def test_b05_negative_identical_rewrite_zero_revisions(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])
        _run_with_rows(conn, inst, monkeypatch, [_row(D0), _row(D_PREV)])
        # ta sama wartosc w innym zapisie dziesietnym (10.50 == 10.5) tez nie jest rewizja
        again = _run_with_rows(conn, inst, monkeypatch, [_row(D0, close="10.50"), _row(D_PREV)])
        assert again.revisions_total == 0
        assert _revisions(conn, inst["id"]) == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_b05_volume_only_change_is_not_a_revision(db_conn, test_instrument, monkeypatch):
    import dataclasses

    conn, inst = db_conn, test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])
        _run_with_rows(conn, inst, monkeypatch, [_row(D0)])
        changed = dataclasses.replace(_row(D0), volume_split_adj=Decimal("2000"))
        again = _run_with_rows(conn, inst, monkeypatch, [changed])
        assert again.revisions_total == 0
        assert _revisions(conn, inst["id"]) == []
        assert _stored(conn, inst["id"], D0)[4] == Decimal("2000")  # nadpisanie jak dzis
    finally:
        conn.rollback()


@pytest.mark.db
def test_b05_new_date_zero_revisions(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])
        _run_with_rows(conn, inst, monkeypatch, [_row(D_PREV)])
        again = _run_with_rows(conn, inst, monkeypatch, [_row(D_PREV), _row(D0)])  # D0 = nowa data
        assert again.revisions_total == 0
        assert _revisions(conn, inst["id"]) == []
        assert _stored(conn, inst["id"], D0) is not None
    finally:
        conn.rollback()


@pytest.mark.db
def test_b05_t12_rejected_row_no_revision_stored_value_unchanged(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    try:
        _clean_synthetic_rows(conn, inst["id"])
        _run_with_rows(conn, inst, monkeypatch, [_row(D_PREV), _row(D0)])
        again = _run_with_rows(conn, inst, monkeypatch, [_row(D_PREV), _row(D0, close=None)])
        assert again.revisions_total == 0
        assert _revisions(conn, inst["id"]) == []
        with conn.cursor() as cur:
            cur.execute("SELECT error_type FROM ingest_errors WHERE instrument_id = %s", (inst["id"],))
            assert [r[0] for r in cur.fetchall()] == ["row_missing_price"]
        assert _stored(conn, inst["id"], D0)[3] == Decimal("10.5")
    finally:
        conn.rollback()

"""B-76 (K2, testy bazodanowe): importer a wypełniacze dostawcy (a/b/c +
regresja 08.10) oraz `apply_manual_corrections`. Każdy test w jednej
transakcji z rollbackiem (fixture `db_conn`, wzorzec
`tests/test_prices_ingest_safety_db.py`); daty syntetyczne 1990-01-0x, ceny
syntetyczne, `fetch_ohlc`/`fetch_currency` podmienione (zero sieci)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mannaz.prices import (
    ManualCorrectionRejectedError,
    OhlcRow,
    apply_manual_corrections,
    run_prices_fetch,
)
from test_prices_ingest_safety_db import db_conn  # noqa: F401  (fixture)

D1 = date(1990, 1, 2)
D2 = date(1990, 1, 3)


def _yahoo_row(price_date, o, h, l, c, v):
    return OhlcRow(
        price_date=price_date,
        open_split_adj=Decimal(o),
        high_split_adj=Decimal(h),
        low_split_adj=Decimal(l),
        close_split_adj=Decimal(c),
        volume_split_adj=Decimal(v),
        adj_close_total_return=Decimal(c),
    )


@pytest.fixture
def b76_instrument(db_conn):  # noqa: F811
    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.yahoo_symbol, i.currency FROM instruments i
            WHERE i.yahoo_symbol IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM corporate_events c
                  WHERE c.instrument_id = i.id AND c.ratio IS NOT NULL
              )
              AND (SELECT count(*) FROM instruments j WHERE j.yahoo_symbol = i.yahoo_symbol) = 1
            ORDER BY i.id
            LIMIT 1
            """
        )
        row = cur.fetchone()
    if row is None:
        pytest.skip("brak jednoznacznego instrumentu bez zdarzen splitowych w bazie testowej")
    inst = {"id": row[0], "yahoo_symbol": row[1], "currency": row[2]}
    with db_conn.cursor() as cur:
        cur.execute(
            "DELETE FROM prices_daily WHERE instrument_id = %s AND price_date IN (%s, %s)",
            (inst["id"], D1, D2),
        )
        cur.execute("DELETE FROM ingest_errors WHERE instrument_id = %s", (inst["id"],))
    return inst


def _insert_stored(conn, inst, price_date, o, h, l, c, v):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO prices_daily (
                instrument_id, price_date, currency, source,
                open_raw, high_raw, low_raw, close_raw, volume_raw,
                open_split_adj, high_split_adj, low_split_adj, close_split_adj,
                volume_split_adj, adjustment_convention
            ) VALUES (%s, %s, %s, 'yahoo', %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, 'yahoo_split_adjusted')
            """,
            (inst["id"], price_date, inst["currency"], o, h, l, c, o, h, l, c, v),
        )


def _stored(conn, inst, price_date):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open_split_adj, high_split_adj, low_split_adj, close_split_adj, volume_split_adj,
                   adjustment_convention, close_raw
            FROM prices_daily WHERE instrument_id = %s AND price_date = %s AND source = 'yahoo'
            """,
            (inst["id"], price_date),
        )
        return cur.fetchone()


def _errors(conn, inst, error_type=None):
    with conn.cursor() as cur:
        if error_type is None:
            cur.execute("SELECT error_type FROM ingest_errors WHERE instrument_id = %s", (inst["id"],))
        else:
            cur.execute(
                "SELECT error_type FROM ingest_errors WHERE instrument_id = %s AND error_type = %s",
                (inst["id"], error_type),
            )
        return [r[0] for r in cur.fetchall()]


def _run(conn, inst, monkeypatch, rows):
    monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: rows)
    monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])
    return run_prices_fetch(conn, instrument_ids=[inst["id"]], start=D1, end=D2, commit=False)


@pytest.mark.db
def test_a_stored_filler_and_same_yahoo_filler_no_errors_row_unchanged(db_conn, b76_instrument, monkeypatch):  # noqa: F811
    conn, inst = db_conn, b76_instrument
    try:
        _insert_stored(conn, inst, D1, "7", "7", "7", "7", "0")
        before = _stored(conn, inst, D1)
        summary = _run(conn, inst, monkeypatch, [_yahoo_row(D1, "7", "7", "7", "7", "0")])
        assert _errors(conn, inst) == []
        assert _stored(conn, inst, D1) == before
        assert summary.results[0].filler_identical == 1
        assert summary.results[0].rows_inserted == 0
        assert summary.filler_kept_total == 0
    finally:
        conn.rollback()


@pytest.mark.db
def test_b_stored_different_and_yahoo_filler_row_kept_filler_kept_counted(db_conn, b76_instrument, monkeypatch):  # noqa: F811
    conn, inst = db_conn, b76_instrument
    try:
        _insert_stored(conn, inst, D1, "8", "9", "7.5", "8.5", "1234")  # np. po korekcie
        before = _stored(conn, inst, D1)
        summary = _run(conn, inst, monkeypatch, [_yahoo_row(D1, "7", "7", "7", "7", "0")])
        assert _stored(conn, inst, D1) == before
        assert _errors(conn, inst) == []
        assert summary.filler_kept_total == 1
        assert summary.instruments_filler_kept == 1
        assert summary.results[0].filler_kept == 1
    finally:
        conn.rollback()


@pytest.mark.db
def test_c_missing_row_and_yahoo_filler_not_stored_one_provider_filler_error(db_conn, b76_instrument, monkeypatch):  # noqa: F811
    conn, inst = db_conn, b76_instrument
    try:
        summary = _run(conn, inst, monkeypatch, [_yahoo_row(D1, "7", "7", "7", "7", "0")])
        assert _stored(conn, inst, D1) is None
        assert _errors(conn, inst) == ["provider_filler"]
        assert summary.filler_missing_total == 1
        assert summary.results[0].filler_missing == 1
        assert summary.results[0].rows_inserted == 0
    finally:
        conn.rollback()


@pytest.mark.db
def test_regression_0810_good_stored_row_not_overwritten_by_filler_no_revision(db_conn, b76_instrument, monkeypatch):  # noqa: F811
    conn, inst = db_conn, b76_instrument
    try:
        _insert_stored(conn, inst, D1, "10", "11", "9", "10.5", "5000")
        before = _stored(conn, inst, D1)
        summary = _run(conn, inst, monkeypatch, [_yahoo_row(D1, "10.5", "10.5", "10.5", "10.5", "0")])
        assert _stored(conn, inst, D1) == before
        assert _errors(conn, inst, "provider_revision") == []
        assert summary.revisions_total == 0
        assert summary.filler_kept_total == 1
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# apply_manual_corrections
# ---------------------------------------------------------------------------

NEW = (Decimal("8"), Decimal("9"), Decimal("7.5"), Decimal("8.5"), Decimal("1500"))


class _TxnProbe:
    """Cienki proxy połączenia: commit/rollback tylko liczone (no-op), żeby wiersze
    testowe wstawione w tej samej transakcji przeżyły wywołanie funkcji; prawdziwy
    rollback robi fixture `db_conn`."""

    def __init__(self, conn):
        self._conn = conn
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *a, **kw):
        return self._conn.cursor(*a, **kw)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _corr(inst, price_date=D1, vals=NEW):
    return (inst["yahoo_symbol"], price_date, *vals)


@pytest.mark.db
def test_manual_correction_dry_run_changes_nothing(db_conn, b76_instrument):  # noqa: F811
    conn, inst = db_conn, b76_instrument
    try:
        _insert_stored(conn, inst, D1, "7", "7", "7", "7", "0")
        before = _stored(conn, inst, D1)
        probe = _TxnProbe(conn)
        summary = apply_manual_corrections(probe, [_corr(inst)], dry_run=True)
        assert summary.rows_to_write == 1 and summary.rows_written == 0 and summary.rejected == []
        assert _stored(conn, inst, D1) == before
        assert _errors(conn, inst, "manual_correction") == []
        assert probe.commits == 0 and probe.rollbacks >= 1
    finally:
        conn.rollback()


@pytest.mark.db
def test_manual_correction_write_updates_values_convention_and_logs(db_conn, b76_instrument):  # noqa: F811
    conn, inst = db_conn, b76_instrument
    try:
        _insert_stored(conn, inst, D1, "7", "7", "7", "7", "0")
        probe = _TxnProbe(conn)
        summary = apply_manual_corrections(probe, [_corr(inst)], dry_run=False)
        assert summary.rows_written == 1
        assert probe.commits == 1
        row = _stored(conn, inst, D1)
        assert row[:5] == NEW
        assert row[5] == "tradingview_manual_b76"
        assert row[6] == NEW[3]  # brak splitow: raw == split_adj
        assert _errors(conn, inst, "manual_correction") == ["manual_correction"]
    finally:
        conn.rollback()


@pytest.mark.db
def test_manual_correction_non_filler_rejected_zero_changes_whole_transaction(db_conn, b76_instrument):  # noqa: F811
    conn, inst = db_conn, b76_instrument
    try:
        _insert_stored(conn, inst, D1, "7", "7", "7", "7", "0")  # filler
        _insert_stored(conn, inst, D2, "10", "11", "9", "10.5", "5000")  # nie filler
        before_1, before_2 = _stored(conn, inst, D1), _stored(conn, inst, D2)
        probe = _TxnProbe(conn)
        with pytest.raises(ManualCorrectionRejectedError) as ei:
            apply_manual_corrections(probe, [_corr(inst, D1), _corr(inst, D2)], dry_run=False)
        assert ei.value.summary.rejected == [(inst["yahoo_symbol"], D2, "nie filler")]
        assert ei.value.summary.rows_written == 0
        assert probe.commits == 0 and probe.rollbacks >= 1
        assert _stored(conn, inst, D1) == before_1  # filler z poprawnego wiersza tez NIETKNIETY
        assert _stored(conn, inst, D2) == before_2
        assert _errors(conn, inst, "manual_correction") == []
    finally:
        conn.rollback()

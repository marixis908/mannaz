"""B-48 (T46) — testy DB: świeca sesji niezamkniętej nie wchodzi do bazy.
Wzorzec jak `test_prices_ingest_safety_db.py` (fixtures stamtąd): jedna
transakcja, `conn.rollback()` w `finally`, `commit=False`, fetch_ohlc/
fetch_currency stubowane, daty 1990, zero sieci. `_session_close_utc` i
`EXCHANGE_TO_CALENDAR_CODE` podmieniane (zamknięcie sesji D0 = 1990-01-03
16:00 UTC)."""

from datetime import datetime, timedelta, timezone

import pytest

from mannaz.prices import run_prices_fetch
from test_prices_ingest_safety_db import (  # noqa: F401 — fixtures + helpery
    D0,
    D_PREV,
    _clean_synthetic_rows,
    _row,
    db_conn,
    test_instrument,
)

_CLOSE_D0 = datetime(1990, 1, 3, 16, 0, tzinfo=timezone.utc)
_CLOSE_PREV = datetime(1990, 1, 2, 16, 0, tzinfo=timezone.utc)


def _inst_with_exchange(conn, inst):
    with conn.cursor() as cur:
        cur.execute("SELECT exchange FROM instruments WHERE id = %s", (inst["id"],))
        return {**inst, "exchange": cur.fetchone()[0]}


def _setup(monkeypatch, inst, with_calendar: bool, rows):
    monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])
    monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: list(rows))
    if with_calendar:
        monkeypatch.setattr("mannaz.prices.EXCHANGE_TO_CALENDAR_CODE", {inst["exchange"]: "TESTCAL"})
        monkeypatch.setattr(
            "mannaz.prices._session_close_utc",
            lambda code, d: _CLOSE_D0 if d == D0 else _CLOSE_PREV,
        )
    else:
        monkeypatch.setattr("mannaz.prices.EXCHANGE_TO_CALENDAR_CODE", {})


def _saved_dates(conn, inst):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT price_date FROM prices_daily WHERE instrument_id = %s AND price_date IN (%s, %s)",
            (inst["id"], D_PREV, D0),
        )
        return {r[0] for r in cur.fetchall()}


def _ingest_error_count(conn, inst) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ingest_errors WHERE instrument_id = %s", (inst["id"],))
        return cur.fetchone()[0]


@pytest.mark.db
@pytest.mark.parametrize(
    "delta_min, expect_saved",
    [(-1, False), (30, True), (31, True)],
    ids=["close-1min-pomijana", "close+30min-granica-zapis", "close+31min-zapis"],
)
def test_b48_calendar_session_closed_boundaries(db_conn, test_instrument, monkeypatch, delta_min, expect_saved):
    conn = db_conn
    inst = _inst_with_exchange(conn, test_instrument)
    try:
        _clean_synthetic_rows(conn, inst["id"])
        _setup(monkeypatch, inst, True, [_row(D_PREV), _row(D0)])
        now = _CLOSE_D0 + timedelta(minutes=delta_min)
        summary = run_prices_fetch(
            conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False, now=now
        )
        result = summary.results[0]
        assert result.rows_fetched == 2
        if expect_saved:
            assert _saved_dates(conn, inst) == {D_PREV, D0}
            assert result.rows_inserted == 2
            assert result.rows_unclosed_session == 0
            assert summary.instruments_unclosed_session == 0
        else:
            assert _saved_dates(conn, inst) == {D_PREV}  # historia zamknieta bez zmian
            assert result.rows_inserted == 1
            assert result.rows_unclosed_session == 1
            assert summary.rows_unclosed_session_total == 1
            assert summary.instruments_unclosed_session == 1
        assert _ingest_error_count(conn, inst) == 0
        assert result.status == "ok"
        assert summary.instruments_ok == 1
    finally:
        conn.rollback()


@pytest.mark.db
def test_b48_no_calendar_today_skipped_previous_day_saved(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = _inst_with_exchange(conn, test_instrument)
    try:
        _clean_synthetic_rows(conn, inst["id"])
        _setup(monkeypatch, inst, False, [_row(D_PREV), _row(D0)])
        now = datetime(1990, 1, 3, 23, 59, tzinfo=timezone.utc)
        summary = run_prices_fetch(
            conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False, now=now
        )
        result = summary.results[0]
        assert _saved_dates(conn, inst) == {D_PREV}
        assert result.rows_unclosed_session == 1
        assert result.rows_inserted == 1
        assert _ingest_error_count(conn, inst) == 0
    finally:
        conn.rollback()


@pytest.mark.db
def test_b48_all_rows_unclosed_status_ok_zero_writes(db_conn, test_instrument, monkeypatch):
    conn = db_conn
    inst = _inst_with_exchange(conn, test_instrument)
    try:
        _clean_synthetic_rows(conn, inst["id"])
        _setup(monkeypatch, inst, True, [_row(D0)])
        now = _CLOSE_D0 - timedelta(minutes=1)
        summary = run_prices_fetch(
            conn, instrument_ids=[inst["id"]], start=D_PREV, end=D0, commit=False, now=now
        )
        result = summary.results[0]
        assert result.rows_fetched == 1
        assert result.rows_unclosed_session == 1
        assert result.rows_inserted == 0
        assert result.status == "ok"
        assert summary.instruments_ok == 1
        assert summary.instruments_empty_response == 0
        assert _saved_dates(conn, inst) == set()
        assert _ingest_error_count(conn, inst) == 0
    finally:
        conn.rollback()

"""B-49 — bledy sieci: jedna ponowna proba, potem wpis `connection_error`
(Yahoo per symbol, Frankfurter per waluta). Zero sieci, `sleep` zastubowany,
testy DB w jednej transakcji z `conn.rollback()` w `finally`, `commit=False`,
daty 1990."""

from datetime import date, datetime, timezone
from decimal import Decimal

import curl_cffi.requests.exceptions as curl_exc
import pytest
import requests

from mannaz import prices
from mannaz.cycle import _origin_note
from mannaz.fx import run_fx_fetch
from mannaz.prices import (
    NETWORK_ERRORS,
    NETWORK_RETRY_SLEEP_SECONDS,
    call_with_network_retry,
    describe_network_error,
    run_prices_fetch,
)
from test_prices_ingest_safety_db import (  # noqa: F401 — fixtures + helpery
    D0,
    D_PREV,
    _clean_synthetic_rows,
    _row,
    db_conn,
    test_instrument,
)

_URL = "https://query1.finance.yahoo.com/v8/chart/X?range=1d&crumb=SECRET"
_NOW = datetime(1990, 1, 4, 12, 0, tzinfo=timezone.utc)


def _curl_conn_error() -> Exception:
    return curl_exc.ConnectionError(f"Failed to perform, curl: (7) {_URL}", 7)


def _requests_conn_error() -> Exception:
    exc = requests.exceptions.ConnectionError(f"HTTPSConnectionPool: {_URL}")
    exc.__cause__ = ConnectionResetError(10054, "reset by peer")
    return exc


def _raiser(exc):
    def f():
        raise exc

    return f


# --- jednostkowe (bez bazy) ---------------------------------------------------
def test_network_errors_tuple_is_exactly_the_four_classes():
    assert set(NETWORK_ERRORS) == {
        curl_exc.ConnectionError,
        curl_exc.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
    }


def test_retry_succeeds_on_second_attempt_and_sleeps_once():
    calls, sleeps = [], []

    def func():
        calls.append(1)
        if len(calls) == 1:
            raise _curl_conn_error()
        return "ok"

    assert call_with_network_retry(func, sleeps.append) == "ok"
    assert len(calls) == 2 and sleeps == [NETWORK_RETRY_SLEEP_SECONDS] == [5]


def test_retry_two_failures_propagates_and_non_listed_does_not_retry():
    sleeps = []
    with pytest.raises(curl_exc.ConnectionError):
        call_with_network_retry(_raiser(_curl_conn_error()), sleeps.append)
    assert len(sleeps) == 1
    sleeps.clear()
    with pytest.raises(ValueError):
        call_with_network_retry(_raiser(ValueError("x")), sleeps.append)
    assert sleeps == []


@pytest.mark.parametrize(
    "exc, expected",
    [
        (_curl_conn_error(), "curl_cffi.requests.exceptions.ConnectionError code=7"),
        (_requests_conn_error(), "requests.exceptions.ConnectionError code=10054"),
        (requests.exceptions.Timeout("slow " + _URL), "requests.exceptions.Timeout code=brak"),
    ],
)
def test_describe_network_error_has_class_and_code_but_no_url(exc, expected):
    detail = describe_network_error(exc)
    assert detail == expected
    assert "http" not in detail and "?" not in detail and "SECRET" not in detail


def test_origin_note_points_to_mannaz_frame_and_last_frame():
    try:
        prices.session_closed("X", date(1990, 1, 3), datetime(1990, 1, 4))  # naive -> ValueError
    except ValueError as exc:
        note = _origin_note(exc)
    assert " (miejsce: mannaz/prices.py:" in note
    assert "; ostatnia ramka: mannaz/prices.py:" in note


def test_origin_note_without_mannaz_frame():
    try:
        int("x")
    except ValueError as exc:
        note = _origin_note(exc)
    assert note.startswith(" (miejsce: brak; ostatnia ramka: ")


# --- Yahoo (DB) ---------------------------------------------------------------
def _ie_rows(conn, inst_id, error_type="connection_error"):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source, price_date, detail FROM ingest_errors "
            "WHERE instrument_id = %s AND error_type = %s",
            (inst_id, error_type),
        )
        return cur.fetchall()


def _saved(conn, inst_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM prices_daily WHERE instrument_id = %s AND price_date IN (%s, %s)",
            (inst_id, D_PREV, D0),
        )
        return cur.fetchone()[0]


@pytest.mark.db
def test_b49_yahoo_first_failure_then_ok_saves_and_no_error(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    sleeps, calls = [], []
    try:
        _clean_synthetic_rows(conn, inst["id"])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda s: inst["currency"])

        def fake_ohlc(symbol, start, end):
            calls.append(symbol)
            if len(calls) == 1:
                raise _curl_conn_error()
            return [_row(D_PREV)]

        monkeypatch.setattr("mannaz.prices.fetch_ohlc", fake_ohlc)
        summary = run_prices_fetch(
            conn,
            instrument_ids=[inst["id"]],
            start=D_PREV,
            end=D0,
            commit=False,
            now=_NOW,
            sleep=sleeps.append,
        )
        assert len(calls) == 2 and sleeps == [5]
        assert summary.results[0].status == "ok" and summary.results[0].rows_inserted == 1
        assert summary.instruments_connection_error == 0
        assert _ie_rows(conn, inst["id"]) == []
        assert _saved(conn, inst["id"]) == 1
    finally:
        conn.rollback()


def _two_instruments(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, yahoo_symbol, currency FROM instruments WHERE yahoo_symbol IS NOT NULL ORDER BY id"
        )
        out, seen = [], set()
        for i, sym, cur_ in cur.fetchall():
            if sym not in seen:
                seen.add(sym)
                out.append({"id": i, "yahoo_symbol": sym, "currency": cur_})
            if len(out) == 2:
                return out
    pytest.skip("mniej niz 2 instrumenty z roznym yahoo_symbol")


@pytest.mark.db
def test_b49_yahoo_two_failures_logs_error_and_next_symbol_processed(db_conn, monkeypatch):
    conn = db_conn
    sleeps = []
    try:
        a, b = _two_instruments(conn)
        for x in (a, b):
            _clean_synthetic_rows(conn, x["id"])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda s: None)

        def fake_ohlc(symbol, start, end):
            if symbol == a["yahoo_symbol"]:
                raise _requests_conn_error()
            return [_row(D_PREV)]

        monkeypatch.setattr("mannaz.prices.fetch_ohlc", fake_ohlc)
        summary = run_prices_fetch(
            conn,
            instrument_ids=[a["id"], b["id"]],
            start=D_PREV,
            end=D0,
            commit=False,
            now=_NOW,
            sleep=sleeps.append,
        )
        assert sleeps == [5]
        ra, rb = summary.results
        assert ra.status == "connection_error" and ra.rows_inserted == 0
        assert summary.instruments_connection_error == 1
        rows = _ie_rows(conn, a["id"])
        assert len(rows) == 1
        source, price_date, detail = rows[0]
        assert source == "yahoo" and price_date is None
        assert detail == "requests.exceptions.ConnectionError code=10054"
        assert "http" not in detail and "?" not in detail
        assert _saved(conn, a["id"]) == 0
        assert rb.status == "ok" and rb.rows_inserted == 1 and _saved(conn, b["id"]) == 1
        assert _ie_rows(conn, b["id"]) == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_b49_yahoo_non_listed_exception_propagates(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    sleeps = []
    try:
        _clean_synthetic_rows(conn, inst["id"])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda s: inst["currency"])

        def boom(symbol, start, end):
            raise ValueError("nie-sieciowy")

        monkeypatch.setattr("mannaz.prices.fetch_ohlc", boom)
        with pytest.raises(ValueError):
            run_prices_fetch(
                conn,
                instrument_ids=[inst["id"]],
                start=D_PREV,
                end=D0,
                commit=False,
                now=_NOW,
                sleep=sleeps.append,
            )
        assert sleeps == []
    finally:
        conn.rollback()


# --- Frankfurter (DB) ---------------------------------------------------------
_FX_START, _FX_END = date(1990, 1, 2), date(1990, 1, 5)
_NBP = [(date(1990, 1, 2), Decimal("4.00")), (date(1990, 1, 3), Decimal("4.10"))]


def _fx_run(conn, monkeypatch, frankfurter, sleeps):
    monkeypatch.setattr("mannaz.fx.fetch_nbp_table_a", lambda cur, s, e: list(_NBP))
    monkeypatch.setattr("mannaz.fx.fetch_frankfurter_rate", frankfurter)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ingest_errors WHERE source = 'frankfurter' AND price_date < '1991-01-01'")
    return run_fx_fetch(
        conn, currencies=("USD",), start=_FX_START, end=_FX_END, commit=False, sleep=sleeps.append
    )


def _fx_errors(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT instrument_id, price_date, error_type, detail FROM ingest_errors "
            "WHERE source = 'frankfurter' AND price_date < '1991-01-01'"
        )
        return cur.fetchall()


@pytest.mark.db
def test_b49_frankfurter_first_failure_then_ok_control_counted(db_conn, monkeypatch):
    conn = db_conn
    sleeps, calls = [], []
    try:

        def ff(currency, d):
            calls.append(d)
            if len(calls) == 1:
                raise _requests_conn_error()
            return Decimal("4.00")

        summary = _fx_run(conn, monkeypatch, ff, sleeps)
        assert sleeps == [5] and len(calls) == 3  # 2 probki + 1 ponowienie
        res = summary.results[0]
        assert res.error == "" and res.rows_inserted == 2
        assert res.control is not None and res.control.n_sessions_compared == 2
        assert _fx_errors(conn) == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_b49_frankfurter_two_failures_logs_error_and_stage_ends_normally(db_conn, monkeypatch):
    conn = db_conn
    sleeps = []
    try:

        def ff(currency, d):
            raise _requests_conn_error()

        summary = _fx_run(conn, monkeypatch, ff, sleeps)
        assert sleeps == [5]  # po pierwszej porazce probki; probki przerwane
        res = summary.results[0]
        assert res.rows_inserted == 2 and res.error == ""
        assert res.control is not None and res.control.n_sessions_compared == 0
        rows = _fx_errors(conn)
        assert len(rows) == 1
        inst_id, price_date, error_type, detail = rows[0]
        assert inst_id is None and price_date == date(1990, 1, 2) and error_type == "connection_error"
        assert detail == "waluta=USD requests.exceptions.ConnectionError code=10054"
        assert "http" not in detail and "?" not in detail
    finally:
        conn.rollback()


@pytest.mark.db
def test_b49_frankfurter_non_listed_exception_propagates(db_conn, monkeypatch):
    conn = db_conn
    try:

        def ff(currency, d):
            raise ValueError("nie-sieciowy")

        with pytest.raises(ValueError):
            _fx_run(conn, monkeypatch, ff, [])
    finally:
        conn.rollback()

"""B-48 C2a — wyjatki yfinance nie sa polykane w `fetch_ohlc` (stub `yf.Ticker`,
nie `fetch_ohlc`): grupa A (siec + YFRateLimitError) -> ponowienie, potem
`connection_error`; grupa B (inne YFException) -> [] = `empty_response`, bez
ponowienia; grupa C (np. ValueError) propaguje. Zero sieci, `sleep` stub,
testy DB: jedna transakcja, `conn.rollback()` w `finally`, `commit=False`, daty 1990."""

import warnings
from datetime import datetime, timedelta, timezone

import curl_cffi.requests.exceptions as curl_exc
import pandas as pd
import pytest
from yfinance.config import YfConfig
from yfinance.exceptions import (
    YFNotImplementedError,
    YFPricesMissingError,
    YFRateLimitError,
    YFTzMissingError,
)

from mannaz.prices import (
    NETWORK_ERRORS,
    YAHOO_RETRYABLE_ERRORS,
    describe_network_error,
    fetch_ohlc,
    run_prices_fetch,
)
from test_prices_ingest_safety_db import (  # noqa: F401 — fixtures + helpery
    D0,
    D_PREV,
    _clean_synthetic_rows,
    db_conn,
    test_instrument,
)

_NOW = datetime(1990, 1, 4, 12, 0, tzinfo=timezone.utc)


def _frame() -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(D_PREV)])
    return pd.DataFrame(
        {
            "Open": [10.0],
            "High": [11.0],
            "Low": [9.0],
            "Close": [10.5],
            "Adj Close": [10.5],
            "Volume": [1000],
        },
        index=idx,
    )


def _stub_ticker(monkeypatch, behaviour, kwargs_log=None):
    """`behaviour(call_no)` zwraca ramke albo rzuca; `kwargs_log` zbiera kwargs."""
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            calls.append(self.symbol)
            if kwargs_log is not None:
                kwargs_log.append(kwargs)
            return behaviour(len(calls))

    monkeypatch.setattr("mannaz.prices.yf.Ticker", FakeTicker)
    return calls


def _raising(exc):
    def f(n):
        raise exc

    return f


def _ie_rows(conn, inst_id, error_type):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM ingest_errors WHERE instrument_id = %s AND error_type = %s",
            (inst_id, error_type),
        )
        return cur.fetchall()


# --- bez bazy -----------------------------------------------------------------
def test_yahoo_retryable_is_network_plus_rate_limit():
    assert YAHOO_RETRYABLE_ERRORS == NETWORK_ERRORS + (YFRateLimitError,)


def test_describe_rate_limit_has_class_and_no_code():
    assert describe_network_error(YFRateLimitError()) == "yfinance.exceptions.YFRateLimitError code=brak"


def test_fetch_ohlc_passes_raise_errors_true_and_keeps_params(monkeypatch):
    log = []
    _stub_ticker(monkeypatch, lambda n: _frame(), log)
    rows = fetch_ohlc("X", D_PREV, D0)
    assert len(rows) == 1 and rows[0].price_date == D_PREV
    kw = log[0]
    assert kw["raise_errors"] is True
    assert kw["auto_adjust"] is False and kw["actions"] is True
    assert kw["start"] == D_PREV.isoformat() and kw["end"] == (D0 + timedelta(days=1)).isoformat()


@pytest.mark.parametrize(
    "exc", [YFPricesMissingError("X", "1d"), YFTzMissingError("X")], ids=["prices_missing", "tz_missing"]
)
def test_fetch_ohlc_yfexception_other_than_rate_limit_returns_empty(monkeypatch, exc):
    _stub_ticker(monkeypatch, _raising(exc))
    assert fetch_ohlc("X", D_PREV, D0) == []


def test_fetch_ohlc_rate_limit_and_group_c_propagate(monkeypatch):
    _stub_ticker(monkeypatch, _raising(YFRateLimitError()))
    with pytest.raises(YFRateLimitError):
        fetch_ohlc("X", D_PREV, D0)
    for exc in (ValueError("x"), YFNotImplementedError("m")):
        _stub_ticker(monkeypatch, _raising(exc))
        with pytest.raises(type(exc)):
            fetch_ohlc("X", D_PREV, D0)


def test_fetch_ohlc_does_not_change_global_state(monkeypatch):
    before_flag = YfConfig.debug.hide_exceptions
    before_filters = list(warnings.filters)
    _stub_ticker(monkeypatch, lambda n: _frame())
    fetch_ohlc("X", D_PREV, D0)
    assert YfConfig.debug.hide_exceptions == before_flag
    assert warnings.filters == before_filters
    for exc in (YFRateLimitError(), YFPricesMissingError("X", "1d"), ValueError("x")):
        _stub_ticker(monkeypatch, _raising(exc))
        try:
            fetch_ohlc("X", D_PREV, D0)
        except Exception:
            pass
        assert YfConfig.debug.hide_exceptions == before_flag
        assert warnings.filters == before_filters


def test_fetch_ohlc_silences_deprecation_warning_locally(monkeypatch):
    def history_warns(n):
        warnings.warn("'raise_errors' deprecated", DeprecationWarning)
        return _frame()

    _stub_ticker(monkeypatch, history_warns)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        fetch_ohlc("X", D_PREV, D0)
    assert not [w for w in caught if "raise_errors" in str(w.message)]


# --- run_prices_fetch (DB) ----------------------------------------------------
def _run(conn, inst, sleeps):
    return run_prices_fetch(
        conn,
        instrument_ids=[inst["id"]],
        start=D_PREV,
        end=D0,
        commit=False,
        now=_NOW,
        sleep=sleeps.append,
    )


@pytest.mark.db
def test_c2a_group_a_network_error_then_ok_saves(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    sleeps = []
    try:
        _clean_synthetic_rows(conn, inst["id"])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda s: inst["currency"])

        def beh(n):
            if n == 1:
                raise curl_exc.ConnectionError("Failed to perform, curl: (7)", 7)
            return _frame()

        calls = _stub_ticker(monkeypatch, beh)
        summary = _run(conn, inst, sleeps)
        assert len(calls) == 2 and sleeps == [5]
        assert summary.results[0].status == "ok" and summary.results[0].rows_inserted == 1
        assert summary.instruments_connection_error == 0
        assert _ie_rows(conn, inst["id"], "connection_error") == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_c2a_group_a_rate_limit_twice_logs_error_and_next_symbol_processed(db_conn, monkeypatch):
    conn = db_conn
    sleeps = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, yahoo_symbol, currency FROM instruments "
                "WHERE yahoo_symbol IS NOT NULL ORDER BY id"
            )
            found, seen = [], set()
            for i, sym, c in cur.fetchall():
                if sym not in seen:
                    seen.add(sym)
                    found.append({"id": i, "yahoo_symbol": sym, "currency": c})
        if len(found) < 2:
            pytest.skip("mniej niz 2 instrumenty z roznym yahoo_symbol")
        a, b = found[:2]
        for x in (a, b):
            _clean_synthetic_rows(conn, x["id"])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda s: None)

        class FakeTicker:
            def __init__(self, symbol):
                self.symbol = symbol

            def history(self, **kwargs):
                if self.symbol == a["yahoo_symbol"]:
                    raise YFRateLimitError()
                return _frame()

        monkeypatch.setattr("mannaz.prices.yf.Ticker", FakeTicker)
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
        rows = _ie_rows(conn, a["id"], "connection_error")
        assert rows == [("yfinance.exceptions.YFRateLimitError code=brak",)]
        assert rb.status == "ok" and rb.rows_inserted == 1
    finally:
        conn.rollback()


@pytest.mark.db
@pytest.mark.parametrize(
    "exc_factory", [lambda: YFPricesMissingError("X", "1d"), lambda: YFTzMissingError("X")]
)
def test_c2a_group_b_empty_response_no_retry(db_conn, test_instrument, monkeypatch, exc_factory):
    conn, inst = db_conn, test_instrument
    sleeps = []
    try:
        _clean_synthetic_rows(conn, inst["id"])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda s: inst["currency"])
        calls = _stub_ticker(monkeypatch, _raising(exc_factory()))
        summary = _run(conn, inst, sleeps)
        assert sleeps == [] and len(calls) == 1
        assert summary.results[0].rows_fetched == 0 and summary.results[0].rows_inserted == 0
        assert summary.instruments_connection_error == 0
        assert len(_ie_rows(conn, inst["id"], "empty_response")) == 1
        assert _ie_rows(conn, inst["id"], "connection_error") == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_c2a_group_c_value_error_propagates(db_conn, test_instrument, monkeypatch):
    conn, inst = db_conn, test_instrument
    sleeps = []
    try:
        _clean_synthetic_rows(conn, inst["id"])
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda s: inst["currency"])
        _stub_ticker(monkeypatch, _raising(ValueError("nie-yahoo")))
        with pytest.raises(ValueError):
            _run(conn, inst, sleeps)
        assert sleeps == []
    finally:
        conn.rollback()

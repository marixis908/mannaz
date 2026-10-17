"""B-72: zapytania Yahoo w corp_actions — jedna ponowna proba po 5 s przy bledzie
sieci / limicie zapytan (jak B-49), druga porazka -> instrument_errors (DATA
FAILURE z tickerem). Testy bez bazy i sieci; yfinance.Ticker podmieniony."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
import requests
from yfinance.exceptions import YFRateLimitError

from mannaz import corp_actions
from mannaz.prices import NETWORK_RETRY_SLEEP_SECONDS

D = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)]


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Conn:
    def cursor(self):
        return _Cur()


def _hist():
    return pd.DataFrame({"Close": [10.0] * 5}, index=pd.DatetimeIndex([pd.Timestamp(d) for d in D]))


def _install(monkeypatch, history_failures=None, splits_failures=None):
    """history_failures / splits_failures: lista wyjatkow rzucanych w kolejnych wywolaniach."""
    calls = {"history": 0, "splits": 0}
    hf = list(history_failures or [])
    sf = list(splits_failures or [])

    class FakeTicker:
        def __init__(self, symbol):
            self.symbol = symbol

        @property
        def splits(self):
            calls["splits"] += 1
            if sf:
                raise sf.pop(0)
            return pd.Series(dtype=float)

        def history(self, **kwargs):
            calls["history"] += 1
            assert kwargs.get("raise_errors") is True  # jak prices.fetch_ohlc
            if hf:
                raise hf.pop(0)
            return _hist()

    monkeypatch.setattr(corp_actions.yf, "Ticker", FakeTicker)
    inst = {"id": 1, "broker_ticker": "AAA", "is_core": False, "rachunek": "AKCYJNY", "currency": "PLN"}
    monkeypatch.setattr(corp_actions, "_owned_instruments", lambda cur: [inst])
    monkeypatch.setattr(corp_actions, "_instrument_transactions", lambda cur, iid, ccy: [(d, Decimal("10")) for d in D])
    return calls


def test_first_history_network_error_second_ok(monkeypatch):
    calls = _install(monkeypatch, history_failures=[requests.exceptions.ConnectionError("x")])
    sleeps: list[float] = []
    s = corp_actions.collect_corp_action_candidates(_Conn(), sleep=sleeps.append)
    assert sleeps == [NETWORK_RETRY_SLEEP_SECONDS] == [5]
    assert calls["history"] == 2
    assert s.instrument_errors == []
    assert s.instruments_checked == 1


def test_first_splits_rate_limit_second_ok(monkeypatch):
    calls = _install(monkeypatch, splits_failures=[YFRateLimitError()])
    sleeps: list[float] = []
    s = corp_actions.collect_corp_action_candidates(_Conn(), sleep=sleeps.append)
    assert sleeps == [5]
    assert calls["splits"] == 2
    assert s.instrument_errors == []


def test_two_history_failures_instrument_error_with_ticker(monkeypatch):
    err = requests.exceptions.ConnectionError("x")
    calls = _install(monkeypatch, history_failures=[err, err])
    sleeps: list[float] = []
    s = corp_actions.collect_corp_action_candidates(_Conn(), sleep=sleeps.append)
    assert sleeps == [5]  # dokladnie jedna ponowna proba
    assert calls["history"] == 2
    assert [(e.broker_ticker, e.symbol, e.exc_type) for e in s.instrument_errors] == [
        ("AAA", "AAA.WA", "ConnectionError")
    ]
    assert s.candidates == []


def test_two_splits_failures_instrument_error_and_write_path_raises(monkeypatch):
    err = requests.exceptions.Timeout("x")
    _install(monkeypatch, splits_failures=[err, err])
    s = corp_actions.collect_corp_action_candidates(_Conn(), sleep=lambda _: None)
    assert [e.broker_ticker for e in s.instrument_errors] == ["AAA"]
    _install(monkeypatch, splits_failures=[err, err])
    with pytest.raises(corp_actions.CorpActionsInstrumentError) as ei:
        corp_actions.run_corp_actions(_Conn(), dry_run=True, sleep=lambda _: None)
    assert "AAA" in str(ei.value)


def test_non_network_error_not_retried(monkeypatch):
    calls = _install(monkeypatch, history_failures=[RuntimeError("boom")])
    sleeps: list[float] = []
    s = corp_actions.collect_corp_action_candidates(_Conn(), sleep=sleeps.append)
    assert sleeps == [] and calls["history"] == 1
    assert [e.exc_type for e in s.instrument_errors] == ["RuntimeError"]

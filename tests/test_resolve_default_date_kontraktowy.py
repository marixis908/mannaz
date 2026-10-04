"""B-36 (M77): domyślne D = ostatnia data kompletna JEDNOCZEŚNIE w cenach (T27)
i w rozliczeniu KONTRAKTOWY (gdy otwarte kontrakty). Dane syntetyczne, bez
bazy — podmieniamy pomocnicze funkcje modułu `risk`."""

from datetime import date

import pytest

from mannaz import risk

D1 = date(2026, 9, 29)
D2 = date(2026, 9, 30)


class _Cur:
    def __init__(self, dates):
        self._dates = dates

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return [(d,) for d in self._dates]


class _Conn:
    def __init__(self, dates):
        self._dates = dates

    def cursor(self):
        return _Cur(self._dates)


def _setup(monkeypatch, instrument_type, kontraktowy_max):
    monkeypatch.setattr(risk, "_open_positions_as_of", lambda cur, d: [{"instrument_type": instrument_type}])
    monkeypatch.setattr(risk, "_resolve_position_price_coverage", lambda cur, pos, d, fn: (object(), None))
    monkeypatch.setattr(
        risk,
        "_kontraktowy_rows",
        lambda cur, d: [{"transaction_date": min(kontraktowy_max, d)}],  # rozliczenie sięga tylko do kontraktowy_max
    )


def test_b36_open_futures_settlement_only_to_d1_picks_d1(monkeypatch):
    _setup(monkeypatch, "future", D1)
    assert risk.resolve_default_risk_date(_Conn([D2, D1]), today=D2) == D1


def test_b36_no_open_futures_same_prices_picks_d2(monkeypatch):
    _setup(monkeypatch, "equity", D1)
    assert risk.resolve_default_risk_date(_Conn([D2, D1]), today=D2) == D2


def test_b36_no_date_complete_in_both_returns_none(monkeypatch):
    _setup(monkeypatch, "future", date(2026, 9, 1))
    assert risk.resolve_default_risk_date(_Conn([D2, D1]), today=D2) is None


def test_b36_uses_same_rule_function_as_run_risk(monkeypatch):
    calls = []

    def fake(n_rows, max_date, has_open_futures, as_of):
        calls.append(as_of)
        if as_of == D2:
            raise RuntimeError("KONTRAKTOWY")

    _setup(monkeypatch, "future", D2)
    monkeypatch.setattr(risk, "check_kontraktowy_coverage", fake)
    assert risk.resolve_default_risk_date(_Conn([D2, D1]), today=D2) == D1
    assert calls == [D2, D1]

"""B-71: detektor zdarzen korporacyjnych a zamkniecia Yahoo NaN/inf/<=0 oraz
wyjatek jednego instrumentu. Testy not-db: yfinance mockowany, conn to prosty
fake (kolektor uzywa tylko cursor() jako context manager i dwoch funkcji
pomocniczych, ktore tez sa podmieniane). Zero sieci, zero bazy.

Test (a) celowo wola WYLACZNIE publiczne `collect_corp_action_candidates` i nie
importuje nowych symboli na poziomie modulu: na starym kodzie (bez B-71) musi
padac na decimal.InvalidOperation (kontrolka ujemna)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from mannaz import corp_actions

# --- fakes -------------------------------------------------------------------


class _Cur:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Tx:
    def __init__(self):
        self.entered = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # jak psycopg: Rollback wycofuje transakcje i jest polykany
        return exc_type is not None and issubclass(exc_type, corp_actions.psycopg.Rollback)


class _Conn:
    def __init__(self):
        self.commits = 0

    def cursor(self):
        return _Cur()

    def transaction(self):
        return _Tx()

    def commit(self):
        self.commits += 1


NAN = float("nan")


def _hist(closes: list[tuple[date, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        {"Close": [c for _, c in closes]},
        index=pd.DatetimeIndex([pd.Timestamp(d) for d, _ in closes]),
    )


class _FakeTicker:
    """Konfiguracja per symbol: {"closes": [...], "splits": [(date, ratio)], "raise": Exception|None}."""

    config: dict = {}

    def __init__(self, symbol):
        self.symbol = symbol
        self.cfg = self.config[symbol]

    @property
    def splits(self):
        sp = self.cfg.get("splits") or []
        if not sp:
            return pd.Series(dtype=float)
        return pd.Series([r for _, r in sp], index=pd.DatetimeIndex([pd.Timestamp(d) for d, _ in sp]))

    def history(self, **kwargs):
        exc = self.cfg.get("raise")
        if exc is not None:
            raise exc
        return _hist(self.cfg["closes"])


def _inst(i: int, ticker: str) -> dict:
    return {"id": i, "broker_ticker": ticker, "is_core": False, "rachunek": "AKCYJNY", "currency": "PLN"}


def _setup(monkeypatch, instruments, config, txns):
    """instruments: lista dictow; config: symbol -> cfg; txns: instrument_id -> [(date, Decimal)]."""
    _FakeTicker.config = config
    monkeypatch.setattr(corp_actions.yf, "Ticker", _FakeTicker)
    monkeypatch.setattr(corp_actions, "_owned_instruments", lambda cur: list(instruments))
    monkeypatch.setattr(corp_actions, "_instrument_transactions", lambda cur, iid, cur_ccy: list(txns[iid]))


D = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8), date(2026, 1, 9)]


def _flat_txns(price="10"):
    return [(d, Decimal(price)) for d in D]


# --- (a) ------------------------------------------------------------------------


def test_a_nan_close_skipped(monkeypatch):
    closes = [(D[0], 10.0), (D[1], 10.0), (D[2], NAN), (D[3], 10.0), (D[4], 10.0)]
    _setup(monkeypatch, [_inst(1, "AAA")], {"AAA.WA": {"closes": closes}}, {1: _flat_txns()})

    summary = corp_actions.collect_corp_action_candidates(_Conn())

    assert summary.ratio_detector_evaluated == 1
    assert summary.ratio_detector_clean_tickers == ["AAA"]
    assert summary.candidates == []
    assert summary.instruments_no_data == 0
    sk = getattr(summary, "skipped_closes")
    assert len(sk) == 1
    assert sk[0].broker_ticker == "AAA" and sk[0].symbol == "AAA.WA"
    assert sk[0].dates == [D[2]]
    assert getattr(summary, "instrument_errors") == []


# --- (b), (b2) ------------------------------------------------------------------


def test_b_zero_and_negative_skipped(monkeypatch):
    closes = [(D[0], 10.0), (D[1], 0.0), (D[2], -5.0), (D[3], 10.0), (D[4], 10.0)]
    _setup(monkeypatch, [_inst(1, "AAA")], {"AAA.WA": {"closes": closes}}, {1: _flat_txns()})
    summary = corp_actions.collect_corp_action_candidates(_Conn())
    assert summary.ratio_detector_evaluated == 1
    assert len(summary.skipped_closes) == 1
    assert summary.skipped_closes[0].dates == [D[1], D[2]]
    assert len(summary.skipped_closes[0].dates) == 2
    assert summary.instrument_errors == []


def test_b2_infinities_skipped(monkeypatch):
    closes = [(D[0], 10.0), (D[1], float("inf")), (D[2], float("-inf")), (D[3], 10.0), (D[4], 10.0)]
    _setup(monkeypatch, [_inst(1, "AAA")], {"AAA.WA": {"closes": closes}}, {1: _flat_txns()})
    summary = corp_actions.collect_corp_action_candidates(_Conn())
    assert summary.ratio_detector_evaluated == 1
    assert [len(s.dates) for s in summary.skipped_closes] == [2]
    assert summary.instrument_errors == []


# --- (c) ------------------------------------------------------------------------


def test_c_all_nan_is_no_data(monkeypatch):
    closes = [(d, NAN) for d in D]
    _setup(monkeypatch, [_inst(1, "AAA")], {"AAA.WA": {"closes": closes}}, {1: _flat_txns()})
    summary = corp_actions.collect_corp_action_candidates(_Conn())
    assert summary.instruments_no_data == 1
    assert summary.ratio_detector_evaluated == 0
    assert summary.candidates == []
    assert len(summary.skipped_closes) == 1
    assert len(summary.skipped_closes[0].dates) == len(D)
    assert summary.instrument_errors == []


# --- (d) kontrolka: seria bez NaN ------------------------------------------------


def test_d_clean_series_unchanged(monkeypatch):
    # broker price / close = 1 -> mediana 1, bez zdarzenia
    closes = [(d, 10.0) for d in D]
    _setup(monkeypatch, [_inst(1, "AAA")], {"AAA.WA": {"closes": closes}}, {1: _flat_txns("10")})
    s = corp_actions.collect_corp_action_candidates(_Conn())
    assert s.instruments_checked == 1
    assert s.ratio_detector_evaluated == 1
    assert s.ratio_detector_clean_tickers == ["AAA"]
    assert s.ratio_detector_hit_tickers == []
    assert s.candidates == [] and s.price_ratio_detected == []
    assert s.skipped_closes == [] and s.instrument_errors == []


def test_d_split_detected_identical_fields(monkeypatch):
    # cena brokera 20, close Yahoo 10 -> iloraz 2 dla wszystkich probek -> split 2:1
    closes = [(d, 10.0) for d in D]
    _setup(monkeypatch, [_inst(1, "AAA")], {"AAA.WA": {"closes": closes}}, {1: _flat_txns("20")})
    s = corp_actions.collect_corp_action_candidates(_Conn())
    assert s.skipped_closes == [] and s.instrument_errors == []
    assert s.ratio_detector_hit_tickers == ["AAA"]
    assert len(s.price_ratio_detected) == 1 and s.candidates == s.price_ratio_detected
    ev = s.price_ratio_detected[0]
    # wartosci referencyjne policzone recznie: median(20/10 x5) = 2
    assert (ev.event_type, ev.ratio, ev.n_samples, ev.median_ratio) == ("split", Decimal(2), 5, Decimal(2))
    assert ev.source == "price_ratio_detector" and ev.instrument_id == 1
    assert ev.date_source == "inferred_all" and ev.event_date == D[-1] + pd.Timedelta(days=1).to_pytimedelta()


def test_d_split_with_nan_gives_same_event_as_without(monkeypatch):
    # NaN w srodku nie zmienia pol zdarzenia poza n_samples == liczba txn (closest prior close)
    clean = [(d, 10.0) for d in D]
    nan = [(D[0], 10.0), (D[1], NAN), (D[2], 10.0), (D[3], 10.0), (D[4], 10.0)]
    res = []
    for closes in (clean, nan):
        _setup(monkeypatch, [_inst(1, "AAA")], {"AAA.WA": {"closes": closes}}, {1: _flat_txns("20")})
        res.append(corp_actions.collect_corp_action_candidates(_Conn()))
    a, b = (r.price_ratio_detected[0] for r in res)
    assert (a.event_type, a.ratio, a.n_samples, a.median_ratio) == (b.event_type, b.ratio, b.n_samples, b.median_ratio)
    assert len(res[1].skipped_closes) == 1 and res[0].skipped_closes == []


# --- (e) wyjatek jednego instrumentu ----------------------------------------------


def _two_instruments(monkeypatch, bbb_splits=None):
    config = {
        "AAA.WA": {"closes": [(d, 10.0) for d in D], "splits": [(date(2026, 2, 1), 2.0)]},
        "BBB.WA": {"closes": [], "splits": bbb_splits or [], "raise": RuntimeError("siec")},
    }
    _setup(monkeypatch, [_inst(1, "AAA"), _inst(2, "BBB")], config, {1: _flat_txns("10"), 2: _flat_txns("10")})


def test_e_instrument_error_isolated(monkeypatch):
    _two_instruments(monkeypatch, bbb_splits=[(date(2026, 3, 1), 4.0)])
    s = corp_actions.collect_corp_action_candidates(_Conn())
    assert s.ratio_detector_evaluated == 1
    assert [(e.broker_ticker, e.symbol, e.exc_type) for e in s.instrument_errors] == [("BBB", "BBB.WA", "RuntimeError")]
    # AAA zachowane (split z yfinance), zero czastkowych kandydatow BBB
    assert [(e.broker_ticker, e.source) for e in s.candidates] == [("AAA", "yfinance_splits")]
    assert [e.broker_ticker for e in s.yfinance_splits_imported] == ["AAA"]
    assert all(e.broker_ticker != "BBB" for e in s.candidates)
    assert s.instruments_checked == 1  # BBB nie liczony jako sprawdzony


def test_e_detect_new_events_carries_lists(monkeypatch):
    _two_instruments(monkeypatch)
    monkeypatch.setattr(corp_actions, "apply_candidates", lambda cur, cands: corp_actions.CorpEventsClassification())
    closes = [(D[0], 10.0), (D[1], NAN), (D[2], 10.0), (D[3], 10.0), (D[4], 10.0)]
    _FakeTicker.config["AAA.WA"]["closes"] = closes
    res = corp_actions.detect_new_corporate_events(_Conn())
    assert [(e.broker_ticker, e.exc_type) for e in res.instrument_errors] == [("BBB", "RuntimeError")]
    assert [(s.broker_ticker, s.dates) for s in res.skipped_closes] == [("AAA", [D[1]])]


def test_e_run_corp_actions_raises_and_does_not_apply(monkeypatch):
    _two_instruments(monkeypatch)
    calls = []
    monkeypatch.setattr(corp_actions, "apply_candidates", lambda cur, cands: calls.append(1))
    conn = _Conn()
    with pytest.raises(corp_actions.CorpActionsInstrumentError) as ei:
        corp_actions.run_corp_actions(conn, dry_run=False)
    assert "BBB" in str(ei.value) and "RuntimeError" in str(ei.value)
    assert calls == [] and conn.commits == 0


# --- (f) kontrolka in-repo: bez filtra NaN blad wraca (z tickerem) --------------


def test_f_old_behaviour_returns_instrument_error(monkeypatch):
    closes = [(D[0], 10.0), (D[1], 10.0), (D[2], NAN), (D[3], 10.0), (D[4], 10.0)]
    _setup(monkeypatch, [_inst(1, "AAA")], {"AAA.WA": {"closes": closes}}, {1: _flat_txns()})
    monkeypatch.setattr(corp_actions, "_usable_close", lambda c: Decimal(str(c)))
    s = corp_actions.collect_corp_action_candidates(_Conn())
    assert [(e.broker_ticker, e.symbol, e.exc_type) for e in s.instrument_errors] == [
        ("AAA", "AAA.WA", "InvalidOperation")
    ]
    assert s.ratio_detector_evaluated == 0 and s.candidates == []


# --- (e-cykl) renderowanie bez bazy ----------------------------------------------


def test_skipped_closes_rendered_and_not_a_data_failure():
    from datetime import datetime

    from mannaz.cycle import ReportState, _render_data_failure_section, has_data_failures

    state = ReportState(d=None, run_started_at=datetime.now())
    state.corp_events_skipped_closes = [
        corp_actions.SkippedCloses(broker_ticker="VRC", symbol="VRC.WA", dates=[date(2026, 10, 15)])
    ]
    assert not has_data_failures(state)
    text = "\n".join(_render_data_failure_section(state))
    assert "- informacja: detektor: pominięte zamknięcia Yahoo (NaN/≤0): VRC 1 (daty: 2026-10-15)" in text

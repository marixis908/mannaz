"""B-38B (C4): progi z prices_daily, rejestr 2 od T42, rejestr trwaly M89 i
wspolna regula emitenta — testy czyste (bez bazy i sieci). Testy bazodanowe:
tests/test_satellite_registry_db.py.

Dane wylacznie syntetyczne (os od 2026-08-03, kwoty umowne)."""

from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mannaz import risk
from mannaz import satellite as sat
from mannaz.satellite import InstrumentInfo

RUN_AT = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# pomocnicy
# ---------------------------------------------------------------------------


def _inst(iid, ticker, typ="equity", yahoo=None, base=None, core=False, qcur="EUR"):
    return InstrumentInfo(
        id=iid, ticker=ticker, instrument_type=typ, is_core=core, multiplier=None,
        yahoo_symbol=yahoo, quote_currency=qcur, calendar_code=None, base_symbol=base,
    )


def _inputs(instruments, prices, layer_events=None):
    return sat.Inputs(
        axis=[], compute_days=[], tx_rows=[], kontraktowy_rows=[], instruments=instruments,
        prices=prices, layer_events=layer_events or {}, fx={}, positions_by_day={},
    )


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _series(n: int = 45):
    """10 dni plasko, 25 dni spadku po 2% dziennie (ok. -40%), 10 dni plasko; progi plaskie.
    Daje alerty/naruszenia dla skladnikow abs, rel_sp500 i rel_spyi."""
    axis = _weekdays(date(2026, 8, 3), n)
    nav = []
    v = 100000.0
    for i in range(n):
        if 10 <= i < 35:
            v *= 0.98
        nav.append(v)
    flows = [0.0] * n
    bench = {"sp500": [100.0] * n, "spyi": [100.0] * n}
    return axis, nav, flows, bench


def _analysis(upto: date | None = None):
    axis, nav, flows, bench = _series()
    if upto is not None:
        k = axis.index(upto) + 1
        axis, nav, flows = axis[:k], nav[:k], flows[:k]
        bench = {key: b[:k] for key, b in bench.items()}
    return sat.analyze_series(axis, nav, flows, bench, True), axis


class FakeCursor:
    """Kursor-stub tabel M89: SELECT zwraca zapisane wiersze rejestru, INSERT je dopisuje
    (bez UPDATE/DELETE — te sciezki nie istnieja w kodzie, test (d) sprawdza to wprost)."""

    def __init__(self):
        self.events: list[tuple] = []  # (registry, component, event_date, level)
        self.viol: list[tuple] = []  # (registry, component, window_end)
        self.statements: list[str] = []
        self._last: list[tuple] = []

    def execute(self, sql, params=None):
        self.statements.append(sql.split()[0].upper())
        if sql.startswith("SELECT component, event_date, level FROM satellite_mandate_events"):
            reg = params[0]
            self._last = [(c, e, lv) for r, c, e, lv in self.events if r == reg]
        elif sql.startswith("SELECT component, window_end FROM satellite_mandate_violations"):
            reg = params[0]
            self._last = [(c, w) for r, c, w in self.viol if r == reg]
        elif sql.startswith("INSERT INTO satellite_mandate_events"):
            reg, comp, ev_date, level = params[:4]
            key = (reg, comp, date.fromisoformat(ev_date), level)
            if key not in self.events:
                self.events.append(key)
        elif sql.startswith("INSERT INTO satellite_mandate_violations"):
            reg, comp, _ws, w_end = params[:4]
            key = (reg, comp, date.fromisoformat(w_end))
            if key not in self.viol:
                self.viol.append(key)
        else:  # pragma: no cover - kod nie powinien wysylac nic innego
            raise AssertionError(f"nieoczekiwany SQL: {sql[:60]}")

    def fetchall(self):
        return self._last


def _persist(cur, an, d, write=True, registry=1):
    return sat.persist_mandate_registry(
        cur, registry, an["alerts"], an["violation_registry"], d, "niezweryfikowane", RUN_AT, "sha-test", write
    )


# ---------------------------------------------------------------------------
# (a) progi z prices_daily
# ---------------------------------------------------------------------------


def test_a_benchmark_series_takes_close_split_adj_and_split_25_stays_continuous():
    split = date(2026, 3, 10)
    days = _weekdays(date(2026, 3, 2), 10)
    # close_split_adj jest ciagla wstecz (dostawca) — brak skoku w dniu splitu
    spyi = [20.0 + 0.1 * i for i in range(10)]
    sxr8 = [500.0 + i for i in range(10)]
    inst = {
        3: _inst(3, "SPYI", "etf", "SPYI.DE", core=True),
        1331: _inst(1331, "SXR8", "etf", "SXR8.DE"),
    }
    events = {3: [{"date": split, "ratio": Decimal(25)}]}
    inp = _inputs(inst, {3: (days, spyi), 1331: (days, sxr8)}, events)
    out, why = sat.benchmark_series(inp)
    assert why == {}
    assert out["spyi"] == pytest.approx(dict(zip(days, spyi)))
    assert out["sp500"] == pytest.approx(dict(zip(days, sxr8)))
    vals = [out["spyi"][d] for d in days]
    assert max(b / a for a, b in zip(vals, vals[1:])) < 1.05  # brak skoku x25
    assert min(b / a for a, b in zip(vals, vals[1:])) > 0.95
    # kontrolka ujemna: mnozenie ceny przez layer_factor_after dalo by skok ~25x w dniu splitu
    raw = [v * float(risk.layer_factor_after(events[3], d)) for d, v in zip(days, vals)]
    k = days.index(split)
    assert raw[k - 1] / raw[k] > 20


def test_a_missing_benchmark_instrument_and_missing_prices_give_none_with_reason():
    days = _weekdays(date(2026, 3, 2), 3)
    inst = {3: _inst(3, "SPYI", "etf", "SPYI.DE", core=True)}  # brak SXR8 w instruments
    out, why = sat.benchmark_series(_inputs(inst, {3: (days, [1.0, 2.0, 3.0])}))
    assert out["sp500"] is None and "SXR8.DE" in why["sp500"] and "brak instrumentu" in why["sp500"]
    assert out["spyi"] is not None
    inst[1331] = _inst(1331, "SXR8", "etf", "SXR8.DE")  # instrument jest, cen brak
    out, why = sat.benchmark_series(_inputs(inst, {3: (days, [1.0, 2.0, 3.0])}))
    assert out["sp500"] is None and "brak cen" in why["sp500"]


# ---------------------------------------------------------------------------
# (b) rejestr 2 od T42
# ---------------------------------------------------------------------------


def test_b_registry2_window_from_t42_shorter_than_requested_and_no_verdict():
    axis, nav, flows, bench = _series()
    r2 = sat.analyze_registry2(axis, nav, flows, bench, verified_base=True)
    assert r2["status"] == "liczony"
    assert r2["S"] == "2026-09-30" and r2["T42"] == "2026-09-30"
    assert r2["verdict_label"] == "bez werdyktu do 2028-09-30"
    w24 = r2["windows"]["24m"]
    assert w24["from"] == "2026-09-30" and w24["shorter_than_requested"] is True
    assert r2["windows"]["od_S"]["from"] == "2026-09-30"
    for w in r2["windows"].values():
        assert w["verdict"]["label"] == "bez werdyktu do 2028-09-30"
        assert w["verdict"]["result"] == "bez werdyktu"
    k = axis.index(date(2026, 9, 30))
    assert w24["twr"] == pytest.approx(nav[-1] / nav[k] - 1.0)  # rejestr 2 liczony od T42, nie od S1
    # werdykt niezaleznie od danych: dane niezweryfikowane -> nadal ta sama etykieta
    r2u = sat.analyze_registry2(axis, nav, flows, bench, verified_base=False)
    assert r2u["windows"]["24m"]["verdict"]["label"] == "bez werdyktu do 2028-09-30"


def test_b_registry2_absent_before_t42():
    axis, nav, flows, bench = _series()
    k = axis.index(date(2026, 9, 29)) + 1
    r2 = sat.analyze_registry2(axis[:k], nav[:k], flows[:k], {key: b[:k] for key, b in bench.items()}, True)
    assert r2["status"] == "przed T42"
    assert r2["windows"] == {} and r2["alerts"] == {} and r2["violation_registry"] == []
    assert r2["verdict_label"] == "bez werdyktu do 2028-09-30"


# ---------------------------------------------------------------------------
# (c) zasilenie poczatkowe vs kolejny przebieg; (d) nieodtworzone; (j) kontrolka ujemna
# ---------------------------------------------------------------------------


def _expected_counts(an, d):
    week = d.isocalendar()[:2]
    n_ev = sum(len(a["events"]) for a in an["alerts"].values())
    n_vi = sum(
        len(e["violations"]) for e in an["violation_registry"]
        if date.fromisoformat(e["window_end"]).isocalendar()[:2] < week
    )
    return n_ev, n_vi


def test_c_initial_feed_then_second_run_is_idempotent():
    an, axis = _analysis()
    d = axis[-1]
    n_ev, n_vi = _expected_counts(an, d)
    assert n_ev > 0 and n_vi > 0  # dane testowe musza cos wywolywac
    cur = FakeCursor()
    p1 = _persist(cur, an, d)
    assert p1["initial_feed"] is True
    assert (len(p1["events"]["new"]), len(p1["violations"]["new"])) == (n_ev, n_vi)
    assert (len(cur.events), len(cur.viol)) == (n_ev, n_vi)
    assert "zasilenie poczatkowe: %d zdarzen, %d naruszen" % (n_ev, n_vi) in sat.persistence_summary_line(p1)
    p2 = _persist(cur, an, d)
    assert p2["initial_feed"] is False
    assert p2["events"]["new"] == [] and p2["violations"]["new"] == []
    assert p2["events"]["not_reproduced"] == [] and p2["violations"]["not_reproduced"] == []
    assert (p2["events"]["previous"], p2["violations"]["previous"]) == (n_ev, n_vi)
    assert (len(cur.events), len(cur.viol)) == (n_ev, n_vi)  # drugi przebieg nic nie dopisal


def test_c_write_false_inserts_nothing_but_reports_what_would_be_inserted():
    an, axis = _analysis()
    cur = FakeCursor()
    p = _persist(cur, an, axis[-1], write=False)
    assert "INSERT" not in cur.statements
    assert cur.events == [] and cur.viol == []
    assert p["write"] is False and len(p["events"]["new"]) > 0 and p["initial_feed"] is True


def test_d_stored_row_not_reproduced_is_listed_and_kept():
    an, axis = _analysis()
    d = axis[-1]
    cur = FakeCursor()
    _persist(cur, an, d)
    ghost_ev = (1, "abs", date(2026, 8, 12), "ostrzezenie")  # dzien w osi, ktorego przeliczenie nie odtwarza
    ghost_vi = (1, "rel_spyi", date(2026, 8, 14))
    cur.events.append(ghost_ev)
    cur.viol.append(ghost_vi)
    before = (list(cur.events), list(cur.viol))
    p = _persist(cur, an, d)
    assert {"component": "abs", "event_date": "2026-08-12", "level": "ostrzezenie"} in p["events"]["not_reproduced"]
    assert {"component": "rel_spyi", "window_end": "2026-08-14"} in p["violations"]["not_reproduced"]
    assert p["events"]["new"] == [] and p["violations"]["new"] == []
    assert (cur.events, cur.viol) == before  # wiersze zostaja
    assert not {"UPDATE", "DELETE"} & set(cur.statements)


def test_j_negative_control_changed_event_date_gives_one_new_one_not_reproduced():
    an, axis = _analysis()
    d = axis[-1]
    cur = FakeCursor()
    _persist(cur, an, d)
    an2 = copy.deepcopy(an)
    comp, a = next((c, x) for c, x in an2["alerts"].items() if x["events"])
    old = a["events"][0]["date"]
    new_day = next(x for x in axis if x.isoformat() not in {e["date"] for e in a["events"]} and x < d)
    a["events"][0]["date"] = new_day.isoformat()
    p = _persist(cur, an2, d)
    assert len(p["events"]["new"]) == 1 and p["events"]["new"][0]["event_date"] == new_day.isoformat()
    assert p["events"]["not_reproduced"] == [{"component": comp, "event_date": old, "level": a["events"][0]["level"]}]
    assert p["violations"]["new"] == [] and p["violations"]["not_reproduced"] == []


# ---------------------------------------------------------------------------
# (k) pelne tygodnie
# ---------------------------------------------------------------------------


def test_k_violations_only_from_full_weeks_written_next_week():
    d1 = date(2026, 9, 16)  # sroda
    d2 = d1 + timedelta(days=7)
    an1, axis1 = _analysis(d1)
    an2, axis2 = _analysis(d2)
    cur = FakeCursor()
    p1 = _persist(cur, an1, d1)
    week1 = d1.isocalendar()[:2]
    assert p1["violations"]["pending_current_week"] >= 1
    assert all(date.fromisoformat(r["window_end"]).isocalendar()[:2] < week1 for r in p1["violations"]["new"])
    assert all(w.isocalendar()[:2] < week1 for _, _, w in cur.viol)
    assert p1["events"]["not_reproduced"] == [] and p1["violations"]["not_reproduced"] == []
    stored_after_1 = len(cur.viol)

    p2 = _persist(cur, an2, d2)
    assert p2["events"]["not_reproduced"] == [] and p2["violations"]["not_reproduced"] == []
    assert p2["violations"]["previous"] == stored_after_1
    new_weeks = {date.fromisoformat(r["window_end"]).isocalendar()[:2] for r in p2["violations"]["new"]}
    assert week1 in new_weeks  # naruszenie z tygodnia pierwszego D zapisane dopiero teraz
    assert not p2["initial_feed"]


# ---------------------------------------------------------------------------
# (i) wspolna regula emitenta
# ---------------------------------------------------------------------------


def test_i_risk_emitent_key_rule_explicit_values():
    assert risk.emitent_key(7, "equity", None) == 7
    assert risk.emitent_key(7, "etf", None) == 7
    assert risk.emitent_key(7, "equity", 99) == 7  # baza dotyczy tylko kontraktow
    # ten sam instrument, dwie waluty rozliczenia -> ten sam klucz
    assert risk.emitent_key(7, "equity", None) == risk.emitent_key(7, "equity", None)
    assert risk.emitent_key(12, "future", 5) == 5  # kontrakt z baza -> id bazy
    assert risk.emitent_key(12, "future", None) == 12  # kontrakt bez bazy -> wlasny id


def test_i_satellite_emitent_key_for_values_unchanged():
    inst = {
        1: _inst(1, "AAA", "equity", "AAA.WA"),
        2: _inst(2, "FAAAZ26", "future", base="AAA.WA"),
        3: _inst(3, "FXXXZ26", "future"),  # bez base_symbol
        4: _inst(4, "FYYYZ26", "future", base="ZZZ.WA"),  # baza nieznaleziona
        5: _inst(5, "ETFX", "etf", "ETFX.WA"),
    }
    ymap = sat.build_yahoo_map(inst)
    expected = {1: (1, "AAA"), 2: (1, "AAA"), 3: (3, "FXXXZ26"), 4: (4, "FYYYZ26"), 5: (5, "ETFX"), 99: (99, "instrument 99")}
    for iid, exp in expected.items():
        assert sat.emitent_key_for(iid, inst, ymap) == exp
    assert [sat.is_future_no_base(i, inst, ymap) for i in (1, 2, 3, 4, 5)] == [False, False, True, True, False]

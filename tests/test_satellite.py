"""Testy modulu oceny satelity (brief CC-OS2, E3; §21.6 dokumentu projektowego).
WYLACZNIE dane syntetyczne, bez bazy i sieci."""

import math
from datetime import date, timedelta

import pytest

from mannaz import satellite as sat
from mannaz.risk import CalendarFacts
from mannaz.satellite import (
    FLOW_CLASS,
    DeltaWResult,
    Inputs,
    InstrumentInfo,
    PositionInput,
    TxRow,
    UnknownRowTypeError,
    classify,
    delta_w,
    external_flow_pln,
    nav_for_day,
    twr,
)

ONE = lambda c, d: 1.0  # noqa: E731  (FX = 1: wiersze w PLN)


def _row(row_type, amount, d=date(2024, 1, 10), is_core=False, **kw):
    return TxRow(d, "AKCYJNY TEST", "PLN", row_type, amount, is_core=is_core, **kw)


def _bench_portfolio(bench, v_a, flows, extra_cost=None):
    """Portfel = kopia progu: jednostki U_t = V_a/B_a + sum F/B; opcjonalnie
    koszt {indeks: kwota} zmniejsza jednostki od tego dnia."""
    units = v_a / bench[0]
    nav = [v_a]
    for i in range(1, len(bench)):
        units += flows[i] / bench[i]
        if extra_cost and i in extra_cost:
            units -= extra_cost[i] / bench[i]
        nav.append(units * bench[i])
    return nav


BENCH = [100.0, 104.0, 101.0, 108.0, 112.0, 110.0]
FLOWS = [0.0, 0.0, 500.0, 0.0, -300.0, 0.0]


# --- 1-2: delta_w -----------------------------------------------------------


def test_delta_w_zero_for_benchmark_copy():
    nav = _bench_portfolio(BENCH, 1000.0, FLOWS)
    res = delta_w(nav, FLOWS, BENCH, 0, len(BENCH) - 1)
    assert abs(res.value) <= 1e-6
    assert res.infeasible is False


def test_delta_w_known_commission():
    x, t = 40.0, 3
    nav = _bench_portfolio(BENCH, 1000.0, FLOWS, extra_cost={t: x})
    res = delta_w(nav, FLOWS, BENCH, 0, len(BENCH) - 1)
    assert res.value == pytest.approx(-x * BENCH[-1] / BENCH[t], abs=1e-6)


def test_delta_w_window_start_inside_series():
    nav = _bench_portfolio(BENCH, 1000.0, FLOWS)
    res = delta_w(nav, FLOWS, BENCH, 2, 5)
    assert abs(res.value) <= 1e-6


def test_delta_w_none_on_missing_flow():
    nav = _bench_portfolio(BENCH, 1000.0, FLOWS)
    flows = list(FLOWS)
    flows[3] = None
    res = delta_w(nav, flows, BENCH, 0, 5)
    assert res.value is None and res.infeasible is False


def test_delta_w_infeasible_flag():
    # wyplata 2000 przy V_a = 1000: jednostki benchmarku ujemne
    bench = [100.0, 100.0, 100.0]
    flows = [0.0, -2000.0, 0.0]
    nav = [1000.0, -1000.0, -1000.0]
    res = delta_w(nav, flows, bench, 0, 2)
    assert res.infeasible is True
    assert res.value == pytest.approx(0.0, abs=1e-9)  # nie dopisuje sie kredytu


# --- 3: TWR -----------------------------------------------------------------


def test_twr_neutral_to_flow_timing():
    # wplata 50 PO ruchu ceny (koniec dnia 1) vs PRZED ruchem (koniec dnia 0)
    after = twr([100.0, 160.0], [0.0, 50.0])[0][1]
    before = twr([150.0, 165.0], [0.0, 0.0])[0][1]
    assert after == pytest.approx(0.10, abs=1e-12)
    assert before == pytest.approx(0.10, abs=1e-12)


def test_twr_index_starts_at_first_positive_nav_and_breaks_on_gap():
    nav = [0.0, 0.0, 100.0, 110.0, None, 121.0, 130.0]
    flows = [0.0] * 7
    r, idx = twr(nav, flows)
    assert idx[:2] == [None, None]
    assert idx[2] == 1.0 and idx[3] == pytest.approx(1.1)
    assert r[4] is None and r[5] is None  # dzien niepelny i nastepny
    assert idx[4] is None and idx[5] is None and idx[6] is None  # przerwanie
    assert r[6] == pytest.approx(130 / 121 - 1)  # r pojedynczego dnia nadal okreslone


def test_window_index_rebased_and_none_on_gap():
    r = [None, 0.1, 0.1, None, 0.1]
    assert sat.window_index(r, 0, 2) == pytest.approx([1.0, 1.1, 1.21])
    assert sat.window_index(r, 0, 4) is None
    assert sat.window_index(r, 3, 4) == pytest.approx([1.0, 1.1])


# --- 4: klasyfikacja zmienia znak dW ---------------------------------------


def test_misclassification_dividend_flips_sign():
    bench = [100.0, 150.0, 200.0]  # B_T/B_t = 4/3 w dniu dywidendy
    nav = [10000.0, 15000.0 + 3000.0, 20000.0 + 3000.0]  # kopia progu + dywidenda 3000
    div = [_row("dywidenda_netto", 3000.0)]
    assert classify("dywidenda_netto") == "dochod"
    f_ok = external_flow_pln(div, ONE).value
    assert f_ok == 0.0
    ok = delta_w(nav, [0.0, f_ok, 0.0], bench, 0, 2).value
    assert ok == pytest.approx(3000.0, abs=1e-6)
    wrong_rows = [_row("przelew_zewnetrzny", 3000.0)]  # ta sama kwota jako wplata
    f_bad = external_flow_pln(wrong_rows, ONE).value
    assert f_bad == 3000.0
    bad = delta_w(nav, [0.0, f_bad, 0.0], bench, 0, 2).value
    assert bad == pytest.approx(-1000.0, abs=1e-6)
    assert math.copysign(1, ok) != math.copysign(1, bad)


def test_misclassification_withholding_tax_flips_sign():
    bench = [100.0, 150.0, 300.0]  # B_T/B_t = 2
    nav = [10000.0, 15000.0 - 1000.0, 30000.0 - 1000.0]  # kopia progu - podatek 1000
    tax = [_row("podatek_dywidenda", -1000.0)]
    assert classify("podatek_dywidenda") == "koszt"
    f_ok = external_flow_pln(tax, ONE).value
    assert f_ok == 0.0
    ok = delta_w(nav, [0.0, f_ok, 0.0], bench, 0, 2).value
    assert ok == pytest.approx(-1000.0, abs=1e-6)
    f_bad = external_flow_pln([_row("przelew_zewnetrzny", -1000.0)], ONE).value  # wyplata
    bad = delta_w(nav, [0.0, f_bad, 0.0], bench, 0, 2).value
    assert bad == pytest.approx(1000.0, abs=1e-6)
    assert math.copysign(1, ok) != math.copysign(1, bad)


# --- 5: obsuniecie wzgledne w obu kierunkach -------------------------------


def test_relative_drawdown_direction():
    i = [1.0 + 0.2 * k / 10 for k in range(11)]  # rosnacy 1 -> 1,2
    b = [1.0] * 11
    q = sat.relative_index(i, b)
    assert sat.max_drawdown(q) == pytest.approx(0.0, abs=1e-12)
    inv = sat.relative_index(b, i)  # B / I
    assert sat.max_drawdown(inv) == pytest.approx(1 - 1 / 1.2, abs=1e-12)
    assert sat.max_drawdown(inv) == pytest.approx(0.166667, abs=1e-6)


# --- 6: status mandatu ------------------------------------------------------

LIM = {"abs": 0.35, "rel_sp": 0.20, "rel_spyi": 0.20}


def test_mandate_violated_with_missing_component():
    st = sat.mandate_status({"abs": 0.40, "rel_sp": None, "rel_spyi": 0.05}, LIM)
    assert st.status == "naruszony"
    assert st.violations == ["abs"] and st.missing == ["rel_sp"]
    status, viol, miss = st  # krotka (status, naruszenia, braki)
    assert (status, viol, miss) == ("naruszony", ["abs"], ["rel_sp"])


def test_mandate_met_and_unassessed():
    assert sat.mandate_status({"abs": 0.35, "rel_sp": 0.1, "rel_spyi": 0.2}, LIM).status == "spelniony"  # rowne limitowi: brak przekroczenia
    st = sat.mandate_status({"abs": 0.10, "rel_sp": None, "rel_spyi": 0.05}, LIM)
    assert st.status == "nieoceniony" and st.violations == [] and st.missing == ["rel_sp"]


# --- 7: eskalacja alertu ----------------------------------------------------


def _series(values):
    d0 = date(2025, 1, 1)
    return [(d0 + timedelta(days=i), v) for i, v in enumerate(values)]


def test_alert_escalation_without_rearm():
    # limit 35%: ostrzezenie od 26,25%, alert od 35%; nie schodzi ponizej 17,5%
    s = _series([0.10, 0.28, 0.30, 0.36, 0.30, 0.37])
    events, state = sat.alert_states(s, 0.35)
    assert [lv for _, lv in events] == ["ostrzezenie", "alert"]
    assert events[0][0] == s[1][0] and events[1][0] == s[3][0]
    assert state == "alert"


def test_alert_repeat_only_after_rearm():
    s = _series([0.10, 0.28, 0.20, 0.30])  # 0,20 >= 17,5% -> brak ponowienia
    events, state = sat.alert_states(s, 0.35)
    assert [lv for _, lv in events] == ["ostrzezenie"]
    assert state == "ostrzezenie"
    s2 = _series([0.10, 0.28, 0.10, 0.30])  # zejscie ponizej 17,5% -> ponowienie
    events2, _ = sat.alert_states(s2, 0.35)
    assert [lv for _, lv in events2] == ["ostrzezenie", "ostrzezenie"]
    s3 = _series([0.36, 0.30, 0.36, 0.10, 0.40])  # alert ponowiony dopiero po rearm
    events3, _ = sat.alert_states(s3, 0.35)
    assert [lv for _, lv in events3] == ["alert", "alert"]
    assert events3[1][0] == s3[4][0]


def test_alert_jump_straight_to_alert_and_none_skipped():
    events, state = sat.alert_states(_series([0.05, None, 0.40]), 0.35)
    assert [lv for _, lv in events] == ["alert"]
    assert sat.alert_states(_series([None, None]), 0.35) == ([], "ok")


# --- 8: nieznany row_type, slownik, nadpisanie ------------------------------


def test_unknown_row_type_raises():
    with pytest.raises(UnknownRowTypeError):
        classify("unknown")
    with pytest.raises(UnknownRowTypeError):
        classify("cos_nowego", is_core=True)
    with pytest.raises(UnknownRowTypeError):
        external_flow_pln([_row("cos_nowego", 1.0)], ONE)


def test_flow_class_complete_21_types():
    assert len(FLOW_CLASS) == 21
    assert {v for v in FLOW_CLASS.values()} == {"zewnetrzny", "wewnetrzny", "handel", "dochod", "koszt"}
    expected = {
        "zewnetrzny": {"przelew_do_domu_maklerskiego", "przelew_zewnetrzny", "bilans_otwarcia"},
        "wewnetrzny": {"przelew_wewnetrzny"},
        "handel": {"kupno", "sprzedaz", "zamiana_akcji", "zamiana_przyjecie", "zamiana_wydanie",
                   "wykup_certyfikatow", "depozyt_doplata", "depozyt_zwrot"},
        "dochod": {"dywidenda_gpw", "dywidenda_netto", "dywidenda_brutto"},
        "koszt": {"podatek_dywidenda", "oplata_rachunek", "oplata_prowadzenie_rachunku", "oplata_przechowanie",
                  "oplata_transakcyjna", "prowizja_wygasniecie"},
    }
    for cls, types in expected.items():
        for t in types:
            assert classify(t) == cls
    assert sum(len(v) for v in expected.values()) == 21


def test_is_core_override_makes_everything_external():
    for t in FLOW_CLASS:
        assert classify(t, is_core=True) == "zewnetrzny"
    # zakup instrumentu rdzenia ze srodkow satelity = wyplata przez granice
    assert external_flow_pln([_row("kupno", -800.0, is_core=True)], ONE).value == -800.0
    assert external_flow_pln([_row("kupno", -800.0)], ONE).value == 0.0
    # opcjonalnie: oplaty rowniez
    assert external_flow_pln([_row("oplata_transakcyjna", -3.0, is_core=True)], ONE).value == -3.0


def test_external_flow_fx_and_noncash():
    fx = lambda c, d: {"PLN": 1.0, "EUR": 4.5}.get(c)  # noqa: E731
    rows = [
        TxRow(date(2024, 1, 10), "ZAGRANICZNY T", "EUR", "przelew_do_domu_maklerskiego", 100.0),
        _row("przelew_wewnetrzny", 999.0),
        _row("dywidenda_netto", 50.0),
        TxRow(date(2024, 1, 10), "ZAGRANICZNY T", "USD", "przelew_zewnetrzny", 10.0),
    ]
    res = external_flow_pln(rows, fx)
    assert res.value == pytest.approx(450.0)
    assert res.missing == [("przelew_zewnetrzny:USD", "brak FX")]
    bo = _row("bilans_otwarcia", 0.0, instrument_id=7, qty=10.0)
    ok = external_flow_pln([bo], fx, lambda r: r.qty * 12.0)
    assert ok.value == 120.0 and ok.missing == []
    miss = external_flow_pln([bo], fx, lambda r: None)
    assert miss.value == 0.0 and len(miss.missing) == 1
    # niegotowkowe wiersze rdzenia i kontraktow nie wnosza nic do NAV satelity
    assert external_flow_pln([_row("bilans_otwarcia", 0.0, is_core=True, instrument_id=1, qty=1.0)], fx).value == 0.0
    assert external_flow_pln([_row("zamiana_przyjecie", 0.0, is_core=True)], fx).value == 0.0
    assert external_flow_pln([_row("bilans_otwarcia", 0.0, instrument_id=2, qty=1.0, instrument_type="future")], fx).value == 0.0


# --- NAV --------------------------------------------------------------------


def _pos(**kw):
    base = dict(rachunek="AKCYJNY TEST", currency="PLN", ticker="AAA", qty=10.0, layer_factor=1.0,
                price=100.0, price_status="ok", price_reason=None, quote_fx=1.0, settle_fx=1.0)
    base.update(kw)
    return PositionInput(**base)


def test_nav_for_day_complete():
    nav = nav_for_day(
        [_pos(), _pos(ticker="BBB", qty=2.0, layer_factor=3.0, price=10.0, quote_fx=4.0, currency="USD", settle_fx=4.0,
                      rachunek="ZAGRANICZNY T", price_status="stale")],
        {("AKCYJNY", "PLN"): 500.0, ("ZAGRANICZNY", "USD"): 10.0},
        {"USD": 4.0},
        kontraktowy_pln=250.0,
    )
    assert nav.complete and nav.missing == []
    assert nav.value == pytest.approx(1000 + 240 + 500 + 40 + 250)
    assert nav.stale == ["BBB"]
    assert nav.by_account_pln[("KONTRAKTOWY", "PLN")] == 250.0
    assert nav.by_account_native[("ZAGRANICZNY", "USD")] == pytest.approx(60 + 10.0)


def test_nav_missing_price_is_incomplete_not_zero():
    nav = nav_for_day(
        [_pos(), _pos(ticker="CCC", price=None, price_status="incomplete", price_reason="brak cen")],
        {("AKCYJNY", "PLN"): 500.0},
        {},
    )
    assert nav.complete is False
    assert nav.value is None  # nigdy cichego zera ani wartosci czesciowej jako NAV
    assert nav.missing == [("CCC", "brak cen")]
    assert nav.partial_value == pytest.approx(1500.0)


def test_nav_missing_fx_and_kontraktowy_error_and_core_separate():
    nav = nav_for_day([], {("ZAGRANICZNY", "EUR"): 5.0}, {"EUR": None}, kontraktowy_error="brak mnoznika")
    assert not nav.complete
    assert ("gotowka ZAGRANICZNY/EUR", "brak FX") in nav.missing and ("KONTRAKTOWY", "brak mnoznika") in nav.missing
    core = _pos(ticker="SPYI", is_core=True, rachunek="ZAGRANICZNY T", currency="EUR", settle_fx=4.0, quote_fx=4.0)
    nav2 = nav_for_day([core, _pos()], {}, {})
    assert nav2.value == pytest.approx(1000.0)  # rdzen poza NAV satelity
    assert nav2.core_by_account_native[("ZAGRANICZNY", "EUR")] == pytest.approx(1000 * 4 / 4)
    nav3 = nav_for_day([_pos(ticker="SPYI", is_core=True, price=None, price_status="incomplete")], {}, {})
    assert nav3.complete and not nav3.core_complete  # brak ceny rdzenia nie psuje NAV satelity


# --- bramka M78 -------------------------------------------------------------


def test_m78_gate():
    nav = nav_for_day([_pos(), _pos(ticker="SPYI", is_core=True, qty=1.0, price=50.0)], {("AKCYJNY", "PLN"): 100.0}, {})
    d = date(2024, 1, 10)
    AV = sat.AccountValue
    # papiery = satelita 1000 + rdzen 50; srodki = 100
    ok = sat.m78_gate([AV(d, "AKCYJNY", "PLN", 1050.0, 100.0)], {d: nav})
    assert ok["ok"] is True and ok["result"] == "zgodne" and ok["n_zgodny"] == 2
    # roznica 0,02% > T39 (0,01%) w papierach
    bad = sat.m78_gate([AV(d, "AKCYJNY", "PLN", 1050.0 * 1.0002, 100.0)], {d: nav})
    assert bad["result"] == "rozjazd" and bad["rows"][0]["status"] == "niezgodny"
    assert bad["rows"][0]["diff"] == pytest.approx(-1050.0 * 0.0002) and bad["rows"][0]["diff_pln"] == pytest.approx(-1050.0 * 0.0002)
    # w granicy tolerancji
    assert sat.m78_gate([AV(d, "AKCYJNY", "PLN", 1050.0 * 1.00005, 100.0)], {d: nav})["ok"] is True
    # "brak" u brokera -> brak danych, nie zero i nie zgodnosc
    part = sat.m78_gate([AV(d, "AKCYJNY", "PLN", 1050.0, None)], {d: nav})
    assert part["result"] == "brak danych" and part["ok"] is False
    assert part["missing_components"] == ["AKCYJNY/PLN/srodki"]
    assert sat.m78_gate([AV(d, "AKCYJNY", "PLN", 1.0, 1.0)], {})["rows"][0]["status"] == "niepelny"
    assert sat.m78_gate([], {})["ok"] is False


# --- obsuniecia, epizody ----------------------------------------------------


def test_drawdown_and_top_episodes():
    d0 = date(2025, 1, 1)
    dates = [d0 + timedelta(days=i) for i in range(12)]
    idx = [1.0, 1.1, 1.0, 0.88, 1.0, 1.1, 1.2, 1.14, 1.08, 1.3, 1.25, 1.24]
    dd = sat.drawdown_series(idx)
    assert dd[3] == pytest.approx(1 - 0.88 / 1.1)
    assert sat.max_drawdown(idx) == pytest.approx(0.2)
    eps = sat.top_episodes(dates, idx, n=3)
    assert len(eps) == 3
    assert eps[0].peak == dates[1] and eps[0].trough == dates[3]
    assert eps[0].depth == pytest.approx(0.2) and eps[0].recovery == dates[5]
    assert eps[1].peak == dates[6] and eps[1].trough == dates[8]
    assert eps[1].depth == pytest.approx(1 - 1.08 / 1.2) and eps[1].recovery == dates[9]
    assert eps[2].peak == dates[9] and eps[2].trough == dates[11] and eps[2].recovery is None
    assert eps[2].depth == pytest.approx(1 - 1.24 / 1.3)
    assert sat.current_drawdown(idx) == pytest.approx(1 - 1.24 / 1.3)
    assert sat.top_episodes(dates, idx, n=1) == eps[:1]
    assert sat.top_episodes(dates[:3], [1.0, 1.1, 1.2]) == []


# --- werdykt ----------------------------------------------------------------


def test_verdict_statuses():
    va = 100000.0  # T40 = 500
    assert sat.verdict({"sp": 2000.0, "spyi": 900.0}, va).result == "przewaga"
    assert sat.verdict({"sp": -2000.0, "spyi": -900.0}, va).result == "strata"
    assert sat.verdict({"sp": 400.0, "spyi": -499.0}, va).result == "remis"
    part = sat.verdict({"sp": 2000.0, "spyi": -900.0}, va)
    assert part.result == "czesciowo" and part.winners == ["sp"]
    assert sat.verdict({"sp": 500.0, "spyi": 501.0}, va).signs == {"sp": "=", "spyi": "+"}  # |dW| <= 0,5% V_a to remis
    unver = sat.verdict({"sp": 2000.0, "spyi": 900.0}, va, data_verified=False)
    assert unver.result == "brak (dane niezweryfikowane)"
    assert unver.signs == {"sp": "+", "spyi": "+"}  # znaki tylko informacyjnie
    assert sat.verdict({"sp": 2000.0, "spyi": None}, va).result == "brak (dane niezweryfikowane)"


def test_verdict_label_with_violated_mandate():
    v = sat.verdict({"sp": 2000.0, "spyi": 900.0}, 100000.0)
    assert sat.verdict_label(v, "naruszony") == "przewaga z naruszeniem mandatu"
    assert sat.verdict_label(v, "spelniony") == "przewaga"


# --- regresja tygodniowa ----------------------------------------------------


def _weekly_dates(n):
    d0 = date(2024, 1, 5)  # piatek
    return [d0 + timedelta(weeks=k) for k in range(n)]


def test_weekly_regression_known_beta():
    rb = [0.01, -0.02, 0.015, 0.03, -0.01, 0.005, -0.03, 0.02, 0.01, -0.005, 0.012, 0.004]
    dates = _weekly_dates(len(rb) + 1)
    b = [100.0]
    i = [1.0]
    for x in rb:
        b.append(b[-1] * (1 + x))
        i.append(i[-1] * (1 + 1.3 * x))
    reg = sat.weekly_regression(dates, i, b)
    assert reg.n == len(rb)
    assert reg.beta == pytest.approx(1.3, abs=1e-9)
    assert reg.alpha == pytest.approx(0.0, abs=1e-9)
    assert reg.se_beta == pytest.approx(0.0, abs=1e-9)
    # TE = std(0,3 r_B) * sqrt(52)
    import statistics
    assert reg.te == pytest.approx(0.3 * statistics.stdev(rb) * math.sqrt(52), rel=1e-9)
    assert reg.se_te == pytest.approx(reg.te / math.sqrt(2 * (len(rb) - 1)), rel=1e-12)
    assert reg.ir is not None and reg.se_ir > 0


def test_weekly_regression_skips_incomplete_weeks_and_small_n():
    dates = _weekly_dates(5)
    i = [1.0, 1.01, None, 1.03, 1.05]
    b = [1.0, 1.0, 1.0, 1.0, 1.0]
    rs, rb = sat.weekly_returns(dates, i, b)
    assert len(rs) == 2  # pary (0,1) i (3,4); pary z None pominiete
    assert sat.weekly_regression(dates, i, b).beta is None  # n < 3


def test_weekly_returns_use_last_axis_day_of_iso_week():
    dates = [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 12), date(2024, 1, 15), date(2024, 1, 19)]
    i = [1.0, 1.1, 1.2, 1.3, 1.5]
    rs, rb = sat.weekly_returns(dates, i, [1.0] * 5)
    assert rs == pytest.approx([1.5 / 1.2 - 1.0])  # tydzien 1: koniec 12.01 (1,2); tydzien 2: koniec 19.01 (1,5)
    assert len(rs) == 1


# --- progi, daty, pomocnicze ------------------------------------------------


def test_bench_on_axis_flags_stale_days():
    axis = [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10)]
    prices = {date(2024, 1, 8): 10.0, date(2024, 1, 10): 12.0}
    b, stale = sat.bench_on_axis(axis, prices, lambda d: 4.0)
    assert b == [40.0, 40.0, 48.0]
    assert stale == [False, True, False]
    b2, _ = sat.bench_on_axis([date(2024, 1, 1)], prices, lambda d: 4.0)
    assert b2 == [None]
    b3, _ = sat.bench_on_axis(axis, prices, lambda d: None)
    assert b3 == [None, None, None]


def test_minus_months():
    assert sat.minus_months(date(2026, 3, 31), 24) == date(2024, 3, 31)
    assert sat.minus_months(date(2024, 2, 29), 12) == date(2023, 2, 28)
    assert sat.minus_months(date(2026, 1, 15), 2) == date(2025, 11, 15)


def test_fx_on_uses_last_rate_not_after_date():
    fx = {"EUR": ([date(2024, 1, 8), date(2024, 1, 10)], [4.3, 4.4])}
    assert sat.fx_on(fx, "EUR", date(2024, 1, 9)) == 4.3
    assert sat.fx_on(fx, "EUR", date(2024, 1, 10)) == 4.4
    assert sat.fx_on(fx, "EUR", date(2024, 1, 7)) is None
    assert sat.fx_on(fx, "USD", date(2024, 1, 9)) is None
    assert sat.fx_on(fx, "PLN", date(2024, 1, 9)) == 1.0


def test_compare_with_p3_series(tmp_path):
    p = tmp_path / "p3.csv"
    p.write_text(
        "date,SP500_EUR,SPYI_EUR,EURPLN,FX_date,SP500_V_PLN,SPYI_V_PLN,SP500_DD,SPYI_DD\n"
        "2024-01-08,1,1,1,2024-01-08,100.0,10.0,0,0\n2024-01-09,1,1,1,2024-01-09,110.0,11.0,0,0\n",
        encoding="utf-8",
    )
    res = sat.compare_with_p3_series(
        {"sp500": {date(2024, 1, 8): 100.0, date(2024, 1, 9): 110.0}, "spyi": {date(2024, 1, 8): 10.1, date(2024, 1, 11): 1.0}},
        p,
    )
    assert res["sp500"] == {"n": 2, "max_rel_diff": 0.0}
    assert res["spyi"]["n"] == 1 and res["spyi"]["max_rel_diff"] == pytest.approx(0.01)


def test_load_account_values_groups_accounts(tmp_path):
    p = tmp_path / "av.csv"
    p.write_text("date,account,currency,positions_value,cash_total\n"
                 "2024-01-10,KONTRAKTOWY 123,PLN,brak,176249.67\n2024-01-10,AKCYJNY,PLN,,brak\n", encoding="utf-8")
    assert sat.load_account_values(p) == [
        sat.AccountValue(date(2024, 1, 10), "AKCYJNY", "PLN", None, None),
        sat.AccountValue(date(2024, 1, 10), "KONTRAKTOWY", "PLN", None, 176249.67),
    ]


# --- skladanie NAV/przeplywow z Inputs (syntetyczne) ------------------------

AXIS = [date(2024, 1, 8), date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11), date(2024, 1, 12), date(2024, 1, 15)]
ALL_OPEN = lambda code, d: CalendarFacts(is_session_on_d=True, sessions_before=[d - timedelta(days=1), d - timedelta(days=2)])  # noqa: E731


def _inputs(price_days=None):
    price_days = AXIS if price_days is None else price_days
    closes = {AXIS[0]: 100.0, AXIS[1]: 100.0, AXIS[2]: 102.0, AXIS[3]: 104.0, AXIS[4]: 106.0, AXIS[5]: 110.0}
    pos = [{"rachunek": "AKCYJNY TEST", "instrument_id": 1, "currency": "PLN", "qty": 5}]
    return Inputs(
        axis=list(AXIS),
        compute_days=list(AXIS),
        tx_rows=[
            TxRow(AXIS[0], "AKCYJNY TEST", "PLN", "przelew_do_domu_maklerskiego", 1000.0),
            TxRow(AXIS[1], "AKCYJNY TEST", "PLN", "kupno", -500.0, 1, 5.0, 100.0, False, "equity"),
            TxRow(date(2024, 1, 13), "AKCYJNY TEST", "PLN", "przelew_zewnetrzny", 200.0),  # sobota
        ],
        kontraktowy_rows=[],
        instruments={1: InstrumentInfo(1, "AAA", "equity", False, None, "AAA.WA", "PLN", "XWAR")},
        prices={1: ([d for d in price_days], [closes[d] for d in price_days])},
        layer_events={},
        fx={},
        positions_by_day={d: (pos if d >= AXIS[1] else []) for d in AXIS},
    )


def test_compute_daily_nav_flows_and_weekend_rollup():
    res = sat.compute_daily(_inputs(), ALL_OPEN)
    navs = [res.navs[d].value for d in AXIS]
    assert navs == pytest.approx([1000.0, 1000.0, 1010.0, 1020.0, 1030.0, 1250.0])
    assert [res.flows[d] for d in AXIS] == [0.0, 0.0, 0.0, 0.0, 0.0, 200.0]  # sobotnia wplata na poniedzialek
    r, idx = twr(navs, [res.flows[d] for d in AXIS])
    assert r[5] == pytest.approx((1250.0 - 200.0) / 1030.0 - 1)
    assert idx[5] == pytest.approx(1.0 * (1010 / 1000) * (1020 / 1010) * (1030 / 1020) * ((1250.0 - 200.0) / 1030.0))


def test_compute_daily_missing_price_makes_day_incomplete():
    days = [d for d in AXIS if d != AXIS[3]]  # brak ceny na D przy otwartej sesji
    res = sat.compute_daily(_inputs(days), ALL_OPEN)
    n = res.navs[AXIS[3]]
    assert n.complete is False and n.value is None
    assert n.missing == [("AAA", "market_open_no_price")]
    assert res.navs[AXIS[2]].complete


def test_compute_daily_unknown_row_type_marks_flow_none():
    inp = _inputs()
    inp.tx_rows.append(TxRow(AXIS[2], "AKCYJNY TEST", "PLN", "nowy_typ", 1.0))
    inp.tx_rows.sort(key=lambda r: r.date)
    res = sat.compute_daily(inp, ALL_OPEN)
    assert res.flows[AXIS[2]] is None
    assert res.unknown_row_types == ["nowy_typ"]


def test_compute_daily_bilans_otwarcia_valued_at_market():
    inp = _inputs()
    inp.tx_rows.append(TxRow(AXIS[2], "AKCYJNY TEST", "PLN", "bilans_otwarcia", 0.0, 1, 3.0, 50.0, False, "equity"))
    inp.tx_rows.sort(key=lambda r: r.date)
    res = sat.compute_daily(inp, ALL_OPEN)
    assert res.flows[AXIS[2]] == pytest.approx(3.0 * 102.0)  # +qty x cena(D) x FX
    # bez ceny na dzien transferu -> przeplyw None (dzien niepelny)
    inp2 = _inputs([d for d in AXIS if d != AXIS[2]])
    inp2.tx_rows.append(TxRow(AXIS[2], "AKCYJNY TEST", "PLN", "bilans_otwarcia", 0.0, 1, 3.0, 50.0, False, "equity"))
    inp2.tx_rows.sort(key=lambda r: r.date)
    res2 = sat.compute_daily(inp2, ALL_OPEN)
    assert res2.flows[AXIS[2]] is None


def test_compute_daily_instrument_without_prices_reports_reason():
    inp = _inputs()
    inp.prices = {}
    res = sat.compute_daily(inp, ALL_OPEN)
    assert res.navs[AXIS[2]].missing == [("AAA", "brak cen")]
    inp.instruments[1].ticker = "INTLXYZ"
    res2 = sat.compute_daily(inp, ALL_OPEN)
    assert res2.navs[AXIS[2]].missing == [("INTLXYZ", "certyfikat bez wyceny")]


def test_compute_daily_stale_price_and_layer_factor():
    inp = _inputs()
    inp.layer_events = {1: [{"date": date(2024, 1, 11), "ratio": 2}]}  # split po D=10.01 -> czynnik 2
    facts = lambda code, d: CalendarFacts(is_session_on_d=False, sessions_before=[d - timedelta(days=1), d - timedelta(days=2)])  # noqa: E731
    inp.prices = {1: ([AXIS[1]], [100.0])}  # tylko cena z 9.01; 10.01 rynek zamkniety -> stale (<= 2 sesje)
    res = sat.compute_daily(inp, facts)
    n = res.navs[AXIS[2]]
    assert n.complete and n.stale == ["AAA"]
    assert n.value == pytest.approx(500.0 + 5 * 2 * 100.0)  # qty x czynnik warstwy x cena split_adj


def test_analyze_series_end_to_end_smoke():
    res = sat.compute_daily(_inputs(), ALL_OPEN)
    nav = [res.navs[d].value for d in AXIS]
    flows = [res.flows[d] for d in AXIS]
    bench = {"sp500": [100.0, 101.0, 102.0, 101.0, 103.0, 104.0], "spyi": [50.0, 50.5, 50.0, 51.0, 52.0, 52.0]}
    out = sat.analyze_series(AXIS, nav, flows, bench, data_verified=False)
    assert out["S"] == AXIS[0].isoformat()
    win = out["windows"]["od_S"]
    assert win["complete"] and win["shorter_than_requested"] is False
    assert win["verdict"]["result"] == "brak (dane niezweryfikowane)"
    assert win["mandate"]["status"] == "spelniony"
    assert win["bench"]["sp500"]["delta_w_pln"] is not None
    assert out["windows"]["24m"]["shorter_than_requested"] is True
    # to samo z weryfikacja: werdykt wynikowy
    out2 = sat.analyze_series(AXIS, nav, flows, bench, data_verified=True)
    assert out2["windows"]["od_S"]["verdict"]["result"] in {"przewaga", "czesciowo", "strata", "remis"}
    # dzien niepelny w oknie: dW i mandat niedostepne, nigdy cichego zera
    nav_gap = list(nav)
    nav_gap[3] = None
    out3 = sat.analyze_series(AXIS, nav_gap, flows, bench, data_verified=True)
    w3 = out3["windows"]["od_S"]
    assert w3["complete"] is False and w3["twr"] is None
    # dW zalezy tylko od V_a, V_T i przeplywow (nie od NAV posrednich) — liczone informacyjnie, werdykt blokowany
    assert w3["bench"]["sp500"]["delta_w_pln"] is not None
    assert w3["mandate"]["status"] == "nieoceniony"
    assert w3["verdict"]["result"].startswith("brak")


def test_analyze_series_no_start_when_nav_never_positive():
    out = sat.analyze_series(AXIS, [0.0] * 6, [0.0] * 6, {"sp500": [1.0] * 6, "spyi": [1.0] * 6}, data_verified=True)
    assert out["S"] is None and out["windows"] == {}


# --- CLI --------------------------------------------------------------------


def test_cli_satellite_parser():
    from mannaz.run_p3 import build_parser

    args = build_parser().parse_args(["satellite", "--from", "2023-10-02", "--to", "2026-09-30", "--out", "x"])
    assert args.date_from == date(2023, 10, 2) and args.date_to == date(2026, 9, 30)
    assert args.account_values is None and args.symbol_map is None
    assert callable(args.func)

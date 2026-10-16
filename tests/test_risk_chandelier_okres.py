"""Rew. 4.19 (brief CC-CH2, C4): Chandelier okresu posiadania (A), kotwica 2N
kontraktow w cenach bazy (B1), kontekst trendu (A8), wariant poprzedniej reguly
(A5). Testy bez bazy: funkcje PRODUKCYJNE (`risk.compute_position_stop` z
`RULE_419` / `RULE_PREV`, `cycle.*`) na danych syntetycznych. Wartosci oczekiwane
to stale policzone recznie (komentarze/docstringi), nie wynik zadnej funkcji
testowanej. Kalendarz: dni robocze; RATCHET_INIT_DATE = 2026-09-24, wiec seria
jest zakotwiczona tak, by data wejscia byla >= tej daty (zapadka od wejscia)
albo — gdzie test tego wymaga (Z9) — wczesniejsza.

Przeniesienie z prototypu (T1-T10): proto Rule(True,'B1'|'trade') ->
`RULE_419` / `StopRule(True, False)`, stara regula -> `RULE_PREV`. T9 prototypu
(wariant B2) NIE jest przenoszony: B2 odrzucony (O-51), nie ma go w kodzie;
jego sens (brak cichego fallbacku przy braku ceny) pokrywa T15 (B1:
`BaseAnchorMissingError`)."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from mannaz import risk as R
from mannaz.cycle import (
    STOP_ORDER_CONTRACT_NOTE,
    ReportState,
    RiskReportAggregates,
    StopEventRow,
    TrendContextRow,
    _render_risk_section,
    _render_stop_orders_section,
    _render_stop_section,
    build_stop_order_rows,
    is_rule_change_only,
    rule_change_direction,
    rule_change_note,
    stop_events,
    trend_context_entries,
)
from mannaz.risk import (
    CHANDELIER_WINDOW,
    RULE_419,
    RULE_PREV,
    BaseAnchorMissingError,
    StopRule,
    chandelier_series,
    compute_position_stop,
    entry_against_trend,
    is_breached,
    prev_rule_needed,
    wilder_atr_series,
)

D = Decimal
HOLD_ONLY = StopRule(True, False)  # tylko A
B1_ONLY = StopRule(False, True)  # tylko B1

E = 30  # indeks sesji wejscia w wiekszosci testow
OCT1 = date(2026, 10, 1)  # czwartek
INIT = date(2026, 9, 24)  # RATCHET_INIT_DATE, czwartek


# ---------------------------------------------------------------- pomocnicze
def calendar(n, idx, d0):
    """n sesji (dni robocze), dates[idx] == d0."""
    out = [None] * n
    out[idx] = d0
    d = d0
    for i in range(idx - 1, -1, -1):
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        out[i] = d
    d = d0
    for i in range(idx + 1, n):
        d += timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        out[i] = d
    return out


def closes_bars(closes):
    """TR = 2 w kazdej sesji (hi=c+1, lo=c-1, |krok|<=1) -> produkcyjne ATR20=ATR22=2."""
    closes = [D(c) for c in closes]
    return [c + 1 for c in closes], [c - 1 for c in closes], closes


def ohlc_bars(rows):
    return [D(r[0]) for r in rows], [D(r[1]) for r in rows], [D(r[2]) for r in rows]


def mirror(bars, pivot=200):
    p = D(pivot)
    h, lo, c = bars
    return [p - x for x in lo], [p - x for x in h], [p - x for x in c]


def case(bars, lots, *, is_short, rule, anchor_idx=E, anchor_date=OCT1, atr=None, contract=False,
         d_idx=None, ticker="FKTEST", atr20=None, atr22=None):
    """Wywolanie produkcyjnego `compute_position_stop`. lots: [(idx, qty, price)];
    d = ostatnia sesja serii (po przycieciu do d_idx). atr: skalar wstrzykniety
    jako ATR20 i ATR22; None -> produkcyjny Wilder na seriach."""
    highs, lows, closes = (list(x) for x in bars)
    if d_idx is not None:
        highs, lows, closes = highs[: d_idx + 1], lows[: d_idx + 1], closes[: d_idx + 1]
    n = len(closes)
    dates = calendar(n, anchor_idx, anchor_date)
    if atr is not None:
        a20 = [D(atr)] * n
        a22 = [D(atr)] * n
    else:
        a20 = wilder_atr_series(highs, lows, [c for c in closes], R.ATR20_PERIOD)
        a22 = wilder_atr_series(highs, lows, [c for c in closes], R.ATR22_PERIOD)
    if atr20 is not None:
        a20 = [None if x is None else D(x) for x in atr20][:n]
    if atr22 is not None:
        a22 = [None if x is None else D(x) for x in atr22][:n]
    kind = "sprzedaz" if is_short else "kupno"
    rows = [{"date": dates[i], "type": kind, "qty": D(q), "price": D(p)} for i, q, p in lots]
    chand = chandelier_series(highs, lows, a22, is_short)
    return compute_position_stop(
        rows, [], dates, highs, lows, closes, a20, a22, chand, D(1), is_short, is_short,
        dates[-1], contract, ticker, rule,
    )


def res(ps):
    return ps.stop_effective, ps.stop_source


def short_lot(price=100):
    return [(E, 1, price)]


def long_lot(price=100):
    return [(E, 1, price)]


def injected_t1(extra_h=(), extra_l=()):
    n = E + 1
    return [D(101)] * n + [D(x) for x in extra_h], [D(99)] * E + [D(90)] + [D(x) for x in extra_l], [D(100)] * (n + len(extra_h))


# ---------------------------------------------------------------- T1
def test_t1_short_entry_session_low_before_trade_does_not_set_chandelier():
    """Short @100, ATR=2 (wstrzykniety). Dolek sesji wejscia 90 (przed transakcja):
    stara regula 90+6 = 96 (kontrolka dodatnia: stara reguła go uzywa), nowa:
    sesja wejscia wylaczona -> 2N = 100+4 = 104."""
    bars = injected_t1()
    assert res(case(bars, short_lot(), is_short=True, rule=RULE_PREV, atr=2)) == (D(96), "chandelier")
    assert res(case(bars, short_lot(), is_short=True, rule=RULE_419, atr=2)) == (D(104), "two_n")
    # nastepna sesja: dolek wejscia nadal wylaczony (99+6 = 105 > 2N)
    bars2 = injected_t1([101], [99])
    assert res(case(bars2, short_lot(), is_short=True, rule=RULE_419, atr=2)) == (D(104), "two_n")


# ---------------------------------------------------------------- T2
def _t2(after):
    bars = closes_bars([100] * 30 + [100] + after)
    a20 = wilder_atr_series(*bars, R.ATR20_PERIOD)
    assert a20[-1] == 2  # produkcyjny ATR = 2
    return bars


def test_t2_two_n_binds_below_threshold():
    """Chandelier = L+6 = 99+6 = 105 > 2N 104 -> 2N."""
    assert res(case(_t2([100]), short_lot(), is_short=True, rule=RULE_419)) == (D(104), "two_n")


def test_t2_boundary_equality_tie_goes_to_two_n():
    """L=98 -> 104 == 2N: remis dla 2N (stop_effective listuje two_n pierwszy)."""
    assert res(case(_t2([99]), short_lot(), is_short=True, rule=RULE_419)) == (D(104), "two_n")


def test_t2_chandelier_binds_above_threshold():
    assert res(case(_t2([99, 98]), short_lot(), is_short=True, rule=RULE_419)) == (D(103), "chandelier")
    assert res(case(_t2([99, 98, 97]), short_lot(), is_short=True, rule=RULE_419)) == (D(102), "chandelier")


# ---------------------------------------------------------------- T3
T3_ROWS = [(101, 99, 100)] * (E + 1) + [(100, 98, 99), (99, 97, 98), (98, 96, 97)]


def test_t3_atr_rise_after_takeover_does_not_loosen():
    """Chandelier przejmuje stop (L=96 -> 102); potem ATR rosnie (szerokie high),
    dolki bez zmian: nieratchetowany poziom 96+3*ATR22 > 102, a stop zostaje 102
    i sciezka nigdy nie idzie w gore (short)."""
    b0 = ohlc_bars(T3_ROWS)
    assert res(case(b0, short_lot(), is_short=True, rule=RULE_419)) == (D(102), "chandelier")
    rows2 = T3_ROWS + [(107, 96, 97)] * 3
    b = ohlc_bars(rows2)
    d = len(rows2) - 1
    a22 = wilder_atr_series(*b, R.ATR22_PERIOD)
    assert a22[d] > 2
    assert D(96) + 3 * a22[d] > 102
    assert res(case(b, short_lot(), is_short=True, rule=RULE_419)) == (D(102), "chandelier")
    path = [case(b, short_lot(), is_short=True, rule=RULE_419, d_idx=t).stop_effective for t in range(E, d + 1)]
    assert all(path[i + 1] <= path[i] for i in range(len(path) - 1))


# ---------------------------------------------------------------- T5 (long, lustro)
@pytest.mark.parametrize("after,exp,src", [
    ([100], D(96), "two_n"),
    ([99], D(96), "two_n"),
    ([99, 98], D(97), "chandelier"),
    ([99, 98, 97], D(98), "chandelier"),
])
def test_t5_long_mirror_of_t2(after, exp, src):
    m = mirror(_t2(after))  # pivot 200: 100 zostaje 100, ATR identyczny
    assert res(case(m, long_lot(), is_short=False, rule=RULE_419)) == (exp, src)


def test_t5_long_mirror_of_t1_and_t3():
    m1 = mirror(injected_t1())
    assert res(case(m1, long_lot(), is_short=False, rule=RULE_PREV, atr=2)) == (D(104), "chandelier")  # 110-6
    assert res(case(m1, long_lot(), is_short=False, rule=RULE_419, atr=2)) == (D(96), "two_n")
    rows = T3_ROWS + [(107, 96, 97)] * 3
    m3 = mirror(ohlc_bars(rows))
    assert res(case(m3, long_lot(), is_short=False, rule=RULE_419)) == (D(98), "chandelier")  # 200-102


# ---------------------------------------------------------------- T6
def test_t6_entry_session_level_is_2n_chandelier_from_first_full_session():
    """Dolki: wejscie 90, potem 99, 98, 97 (ATR22=2). Sciezka stopu po sesjach
    e..e+3: 104 (2N), 104 (99+6=105 nie wiaze), 104 (98+6 = 104, remis -> 2N),
    103 (97+6, chandelier); dolek 90 wylaczony."""
    lows = [99] * E + [90, 99, 98, 97]
    n = len(lows)
    bars = ([D(101)] * n, [D(x) for x in lows], [D(100)] * n)
    path = [case(bars, short_lot(), is_short=True, rule=RULE_419, atr=2, d_idx=t) for t in range(E, n)]
    assert [res(p) for p in path] == [
        (D(104), "two_n"), (D(104), "two_n"), (D(104), "two_n"), (D(103), "chandelier"),
    ]


def test_t6_ratchet_start_after_entry_uses_first_full_window_from_ratchet_start():
    """Data wejscia przed RATCHET_INIT_DATE: zapadka startuje w INIT (indeks e+3),
    Chandelier tylko z okna [e+1..t] liczony na D=INIT: min(99,98,97)+6 = 103."""
    lows = [99] * E + [90, 99, 98, 97]
    n = len(lows)
    bars = ([D(101)] * n, [D(x) for x in lows], [D(100)] * n)
    ps = case(bars, short_lot(), is_short=True, rule=RULE_419, atr=2, anchor_idx=E + 3, anchor_date=INIT)
    assert (ps.ratchet_start_idx, ps.entry_idx) == (E + 3, E)
    assert res(ps) == (D(103), "chandelier")


# ---------------------------------------------------------------- T4 / Z9
R_IDX = 52


def z9_case(e, d=55):
    """short, ATR=2. Sesja wejscia e: dolek 80; sesje po wejsciu: dolek 95
    (Chandelier 101); wczesniej 99. Zapadka od INIT (indeks 52)."""
    n = d + 1
    lows = [D(99)] * n
    for i in range(e + 1, n):
        lows[i] = D(95)
    lows[e] = D(80)
    return ([D(101)] * n, lows, [D(100)] * n), [(e, 1, 100)]


def z9_same(e):
    bars, lots = z9_case(e)
    kw = dict(is_short=True, atr=2, anchor_idx=R_IDX, anchor_date=INIT)
    old = res(case(bars, lots, rule=RULE_PREV, **kw))
    new = res(case(bars, lots, rule=HOLD_ONLY, **kw))
    return old, new


def test_t4_z9_satisfied_identical_and_one_session_beyond_differs():
    """e <= R-22: sesja wejscia poza kazdym starym oknem od R -> A nie zmienia stopu.
    e = R-21: stare okno w R = [31..52] zawiera sesje wejscia (dolek 80 -> 86)."""
    old, new = z9_same(R_IDX - 22)
    assert old == new == (D(101), "chandelier")
    old, new = z9_same(R_IDX - 21)
    assert old == (D(86), "chandelier")  # 80+6 trzymane zapadka
    assert new == (D(101), "chandelier")


def test_t4_z9_boundary_matches_brute_force():
    for e in range(R_IDX - 30, R_IDX):
        old, new = z9_same(e)
        assert (old == new) == (e <= R_IDX - CHANDELIER_WINDOW), e


# ---------------------------------------------------------------- T7
def _contract_bars():
    return closes_bars([100] * 21 + [101, 102, 103, 104, 105] + [105] * 35)  # 61 sesji, close(e=20) = 100


def _t7(rule, contract=True):
    return case(_contract_bars(), [(20, 1, 105)], is_short=True, rule=rule, anchor_idx=50, anchor_date=INIT,
                contract=contract)


def test_t7_old_contract_b1_changes_stop_despite_age():
    """Short kontrakt @105, close bazy w sesji wejscia 100, ATR=2, e=20 <= R-22 (Z9):
    stara 105+4 = 109; B1 100+4 = 104; A sam nie zmienia (wiek poza Z9)."""
    assert res(_t7(RULE_PREV)) == (D(109), "two_n")
    assert res(_t7(B1_ONLY)) == (D(104), "two_n")
    assert res(_t7(RULE_419)) == (D(104), "two_n")
    assert res(_t7(HOLD_ONLY)) == (D(109), "two_n")


def test_t7_non_contract_keeps_trade_price_under_419():
    assert res(_t7(RULE_419, contract=False)) == (D(109), "two_n")
    assert _t7(RULE_419, contract=False).entry_price == D(105)
    assert _t7(RULE_419, contract=True).entry_price == D(100)


# ---------------------------------------------------------------- T8
def _t8_positions():
    b1, l1 = z9_case(R_IDX - 22)
    p1 = dict(bars=b1, lots=l1, is_short=True, atr=2, anchor_idx=R_IDX, anchor_date=INIT)
    p2 = dict(bars=_contract_bars(), lots=[(20, 1, 105)], is_short=True, anchor_idx=50, anchor_date=INIT, contract=True)
    n = E + 2
    p3 = dict(bars=([D(101)] * E + [D(105), D(101)], [D(99)] * n, [D(100)] * n), lots=long_lot(), is_short=False, atr=2)
    p4 = dict(bars=closes_bars([100] * 40), lots=[(30, 1, 95)], is_short=False, anchor_idx=30, anchor_date=INIT,
              contract=True)
    return {"P1": p1, "P2": p2, "P3": p3, "P4": p4}


def test_t8_rule_change_direction_only_for_differing_positions():
    """P1 identyczne ('0'); P2 short kontrakt 109 -> 104 (zaostrzenie); P3 long, high
    wejscia 105, nastepne 101: stara 99 -> nowa 96 (poluzowanie); P4 long kontrakt,
    cena 95 vs close bazy 100: stara max(2N 91, Chandelier 95) = 95, B1 2N = 96
    wygrywa -> 96 (zaostrzenie)."""
    got = {}
    for pid, p in _t8_positions().items():
        p = dict(p)
        bars, lots = p.pop("bars"), p.pop("lots")
        prev = case(bars, lots, rule=RULE_PREV, **p)
        new = case(bars, lots, rule=RULE_419, **p)
        kind = "short" if p["is_short"] else "long"
        got[pid] = (prev.stop_effective, new.stop_effective, rule_change_direction(kind, new.stop_effective, prev.stop_effective))
    assert got["P1"] == (D(101), D(101), "0")
    assert got["P2"] == (D(109), D(104), "zaostrzenie")
    assert got["P3"] == (D(99), D(96), "poluzowanie")
    assert got["P4"] == (D(95), D(96), "zaostrzenie")


# ---------------------------------------------------------------- T10
def test_t10_b1_add_on_contract_replaces_historical_2n_held_anchor():
    """Short kontrakt: 1 @95 w dniu 0 (= RATCHET_START 2026-09-24), +1 @105 w dniu 2;
    close bazy 100 kazdego dnia, ATR20=2, Chandelier 120 (nie wiaze). B1: biezace
    2N = 100+4 = 104; punkt historyczny dnia 0: 1 lot @100 -> 104, nie scisle lepszy
    -> 'two_n', stop 104. Kontrolka: stara regula (ceny kontraktu) -> historyczny
    punkt 95+4 = 99 -> 'two_n_held' 99 (to utrzymuje wariant mieszany)."""
    bars = ([D(121)] * 3, [D(114)] * 3, [D(100)] * 3)
    lots = [(0, 1, 95), (2, 1, 105)]
    kw = dict(is_short=True, atr=2, anchor_idx=0, anchor_date=INIT, contract=True)
    b1 = case(bars, lots, rule=RULE_419, **kw)
    assert (b1.stop_2n, b1.stop_2n_held) == (D(104), D(104))
    assert res(b1) == (D(104), "two_n")
    # risk: close_D = 101, |qty| = 2, mnoznik 10: (104-101)*2*10 = 60, bez przekroczenia
    assert R.compute_risk_native(D(101), b1.stop_effective, D(2), D(10), True) == (D(60), False)
    prev = case(bars, lots, rule=RULE_PREV, **kw)
    assert prev.stop_2n == D(104)  # srednia 100 -> 104
    assert res(prev) == (D(99), "two_n_held")


# ---------------------------------------------------------------- T11 (m1)
def test_t11_entry_session_high_not_binding_under_a_old_rule_flips_state():
    """Long @100, ATR20=ATR22=2 (wstrzykniete; ATR22 istnieje w kazdej sesji).
    Sesje 0..24: H101/L99/C100; sesja wejscia (25): H110, C105. Zamkniecie sesji
    wejscia: stara reguła 110-6 = 104 wiaze (>2N 96); A: 2N = 96 (Chandelier brak).
    Nastepna sesja (26): H105, L100, C103: A = 105-6 = 99 (C103 -> NORMAL),
    stara: okno nadal z H110 -> 104 (C103 -> HIGH)."""
    e = 25
    h = [D(101)] * e + [D(110), D(105)]
    lo = [D(99)] * e + [D(99), D(100)]
    c = [D(100)] * e + [D(105), D(103)]
    bars = (h, lo, c)
    kw = dict(is_short=False, atr=2, anchor_idx=e, anchor_date=OCT1)
    old0 = case(bars, [(e, 1, 100)], rule=RULE_PREV, d_idx=e, **kw)
    new0 = case(bars, [(e, 1, 100)], rule=RULE_419, d_idx=e, **kw)
    assert res(old0) == (D(104), "chandelier")
    assert res(new0) == (D(96), "two_n")
    old1 = case(bars, [(e, 1, 100)], rule=RULE_PREV, d_idx=e + 1, **kw)
    new1 = case(bars, [(e, 1, 100)], rule=RULE_419, d_idx=e + 1, **kw)
    assert res(old1) == (D(104), "chandelier")
    assert res(new1) == (D(99), "chandelier")
    close = D(103)
    assert is_breached(close, old1.stop_effective, False) is True  # HIGH wg starej
    assert is_breached(close, new1.stop_effective, False) is False  # NORMAL wg 4.19


# ---------------------------------------------------------------- T12 (m4)
def _ser(prev_rule_state, ticker="FKTEST"):
    return StopEventRow(
        broker_ticker=ticker, rachunek="AKCYJNY 000001", instrument_id=1, instrument_type="equity",
        is_core=False, below_stop=True, price_date_used=date(2026, 10, 15), close_d=D("90"),
        stop_effective=D("100"), stop_source="two_n", price_source_symbol="XYZ", currency="USD",
        settlement_currency="USD", risk_state_prev_rule=prev_rule_state,
    )


@pytest.mark.parametrize("prev_state,expected", [("NORMAL", True), ("HIGH", False), (None, False)])
def test_t12_stop_events_rule_change_only_flag_and_render(prev_state, expected):
    assert is_rule_change_only(prev_state) is expected
    events = stop_events([_ser(prev_state)], {})
    assert len(events) == 1 and events[0].rule_change_only is expected
    state = ReportState(d=date(2026, 10, 15), run_started_at=datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc))
    state.stop_events = events
    text = "\n".join(_render_stop_section(state))
    assert ("(wyłącznie zmiana reguły)" in text) is expected
    assert "FKTEST" in text  # kontrolka: wiersz jest w tabeli w kazdym wariancie


# ---------------------------------------------------------------- T13 (A8)
def test_t13_entry_against_trend_long_and_short():
    ch = [None, D(105), None]
    assert entry_against_trend(D(100), ch, 1, False) is True  # long: kotwica < Chandelier
    assert entry_against_trend(D(106), ch, 1, False) is False
    assert entry_against_trend(D(105), ch, 1, False) is False  # rownosc: nie
    assert entry_against_trend(D(110), ch, 1, True) is True  # short: kotwica > Chandelier
    assert entry_against_trend(D(100), ch, 1, True) is False
    assert entry_against_trend(D(105), ch, 1, True) is False
    assert entry_against_trend(D(100), ch, 0, False) is None  # brak Chandeliera w e
    assert entry_against_trend(D(100), ch, 2, True) is None
    assert entry_against_trend(D(100), ch, None, False) is None  # brak e
    assert entry_against_trend(None, ch, 1, False) is None  # brak kotwicy


# ---------------------------------------------------------------- T14 (m3)
def _t14(rule):
    """Long kontrakt @104 (lot w sesji 25), close bazy 100, ATR=2. Sesja wejscia: H112
    (stary Chandelier 112-6 = 106 wiaze), potem H101 (A: 101-6 = 95).
    PREV: max(2N 104-4=100, 106) = 106; A: max(100, 95) = 100; B1: max(96, 106) = 106;
    RULE_419: max(2N 100-4=96, 95) = 96."""
    e = 25
    n = e + 4
    h = [D(101)] * e + [D(112)] + [D(101)] * 3
    bars = (h, [D(99)] * n, [D(100)] * n)
    return case(bars, [(e, 1, 104)], is_short=False, rule=rule, anchor_idx=e, anchor_date=OCT1, atr=2,
                contract=True, ticker="FKTEST")


def test_t14_a_exposes_2n_and_b1_changes_it_result_differs_from_every_partial_rule():
    assert res(_t14(RULE_PREV)) == (D(106), "chandelier")
    assert res(_t14(HOLD_ONLY)) == (D(100), "two_n")
    assert res(_t14(B1_ONLY)) == (D(106), "chandelier")
    assert res(_t14(RULE_419)) == (D(96), "two_n")
    r = res(_t14(RULE_419))
    assert r != res(_t14(HOLD_ONLY)) and r != res(_t14(B1_ONLY)) and r != res(_t14(RULE_PREV))


# ---------------------------------------------------------------- T15 (B1)
def test_t15_missing_base_close_raises_with_ticker_for_contract_b1():
    e = 25
    n = e + 2
    bars_ok = ([D(101)] * n, [D(99)] * n, [D(100)] * n)
    kw = dict(is_short=False, atr=2, anchor_idx=e, anchor_date=OCT1, contract=True, ticker="FKTEST")
    # brak zamkniecia bazy w sesji lota (None)
    closes_none = [D(100)] * n
    closes_none[e] = None
    with pytest.raises(BaseAnchorMissingError) as ei:
        case((bars_ok[0], bars_ok[1], closes_none), [(e, 1, 104)], rule=RULE_419, **kw)
    assert "FKTEST" in str(ei.value) and ei.value.ticker == "FKTEST"
    assert ei.value.session_date == calendar(n, e, OCT1)[e]
    # data lota po ostatniej dacie serii (brak sesji >= data lota)
    dates = calendar(n, e, OCT1)
    row = {"date": dates[-1] + timedelta(days=7), "type": "kupno", "qty": D(1), "price": D(104)}
    with pytest.raises(BaseAnchorMissingError, match="FKTEST"):
        R.base_anchor_rows([row], dates, bars_ok[2], "FKTEST")
    # kontrolki: ten sam brak, ale nie kontrakt albo reguła poprzednia -> bez wyjatku
    kw_eq = dict(kw, contract=False)
    assert case((bars_ok[0], bars_ok[1], closes_none), [(e, 1, 104)], rule=RULE_419, **kw_eq).stop_2n == D(100)
    assert case((bars_ok[0], bars_ok[1], closes_none), [(e, 1, 104)], rule=RULE_PREV, **kw).stop_2n == D(100)
    # kontrolka dodatnia: komplet danych -> B1 liczy sie (2N = 100-4)
    assert case(bars_ok, [(e, 1, 104)], rule=RULE_419, **kw).stop_2n == D(96)


def test_t15b_lot_session_missing_from_series_is_data_failure_not_next_record():
    """Recenzja Codexa R1, M1: `_price_series` pomija rekordy bez zamkniecia, wiec
    brak sesji lota (wt. 2026-09-29) w serii nie moze zostac zastapiony nastepnym
    rekordem (sr. 2026-09-30, close 120). Kontrprzyklad Codexa: kotwica 120,
    stop 116, HIGH przy close 110 zamiast DATA FAILURE."""
    lot = {"date": date(2026, 9, 29), "type": "kupno", "qty": D(1), "price": D(105)}
    dates_gap = [date(2026, 9, 25), date(2026, 9, 30)]
    closes_gap = [D(100), D(120)]
    session_is_lot_date = lambda d: d  # noqa: E731 - sesja rynku = data lota (dzien sesyjny)
    # bez kalendarza (czyste testy): stary wybor _index_on_or_after -> nastepny rekord
    assert R.base_anchor_rows([lot], dates_gap, closes_gap, "FKTEST")[0]["price"] == D(120)
    # z kalendarzem: brak rekordu sesji lota -> DATA FAILURE z tickerem
    with pytest.raises(BaseAnchorMissingError, match="FKTEST") as ei:
        R.base_anchor_rows([lot], dates_gap, closes_gap, "FKTEST", session_is_lot_date)
    assert ei.value.session_date == date(2026, 9, 29)
    # ta sama sciezka przez compute_position_stop (run_risk przekazuje lot_session)
    n = len(dates_gap)
    with pytest.raises(BaseAnchorMissingError, match="FKTEST"):
        compute_position_stop(
            [lot], [], dates_gap, [D(121), D(121)], [D(99), D(99)], closes_gap, [D(2)] * n, [D(2)] * n,
            [None] * n, D(1), False, False, dates_gap[-1], True, "FKTEST", RULE_419, session_is_lot_date,
        )
    # kontrolka dodatnia: rekord sesji lota jest -> kotwica = jego zamkniecie
    dates_ok = [date(2026, 9, 25), date(2026, 9, 29), date(2026, 9, 30)]
    closes_ok = [D(100), D(110), D(120)]
    assert R.base_anchor_rows([lot], dates_ok, closes_ok, "FKTEST", session_is_lot_date)[0]["price"] == D(110)


def test_t15c_lot_session_resolver_uses_exchange_calendar():
    """lot_session_resolver: pierwsza sesja kalendarza gieldy >= data lota (XWAR)."""
    resolve = R.lot_session_resolver(R.EXCHANGE_TO_CALENDAR_CODE["GPW"], date(2026, 10, 15))
    assert resolve(date(2026, 9, 29)) == date(2026, 9, 29)  # wtorek, sesja
    assert resolve(date(2026, 10, 3)) == date(2026, 10, 5)  # sobota -> poniedzialek
    assert resolve(date(2026, 10, 16)) is None  # po as_of: brak sesji w zakresie
    assert R.lot_session_resolver(None, date(2026, 10, 15))(date(2026, 9, 29)) is None


# ---------------------------------------------------------------- T16 (A8, zmiana specyfikacji)
def _tcr(ticker, flag, hps):
    return TrendContextRow(
        broker_ticker=ticker, rachunek="AKCYJNY 000001", position_kind="long", holding_period_start=hps,
        anchor=D(100), chandelier_rolling_at_entry=D(105), chandelier_rolling_ctx=D(104), currency="USD",
        entry_against_trend=flag,
    )


PREV_D = date(2026, 10, 8)


def _risk_state(trend):
    state = ReportState(d=date(2026, 10, 15), run_started_at=datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc))
    state.risk_aggregates = RiskReportAggregates(
        heat_pct_total=D("2.5"), heat_pct_zagraniczny=D("1.1"), level1_max_ticker="AAA", level1_max_pct=D("0.8"),
        level1_breach_tickers=[], theme_ranking=[("tech", D("2.0"), False)], level3_breach=False,
        capital_satelite_positions_pln=D("10000"), kontraktowy_account_value_pln=D("5000"),
        capital_satelite_pln=D("15000"), stale_tickers=[],
    )
    state.trend_context = trend
    return state


def test_t16_trend_context_only_new_positions_with_flag():
    rows = [
        _tcr("OLDT", True, date(2026, 9, 30)),  # stara (<= prev) z flaga T -> nie
        _tcr("EQDT", True, PREV_D),  # rowna prev: nie "po" -> nie
        _tcr("NEWT", True, date(2026, 10, 12)),  # nowa z flaga T -> tak
        _tcr("NEWF", False, date(2026, 10, 12)),  # kontrolka: nowa, flaga F -> nie
        _tcr("NEWN", None, date(2026, 10, 12)),  # kontrolka: nowa, flaga None -> nie
        _tcr("NOHP", True, None),  # brak poczatku okresu -> nie
    ]
    out = trend_context_entries(rows, PREV_D)
    assert [r.broker_ticker for r in out] == ["NEWT"]
    # brak poprzedniej oceny -> wszystkie z flaga True
    assert [r.broker_ticker for r in trend_context_entries(rows, None)] == ["OLDT", "EQDT", "NEWT", "NOHP"]
    text = "\n".join(_render_risk_section(_risk_state(out)))
    assert "### Kontekst trendu (nie jest stopem)" in text
    assert "| NEWT |" in text
    for t in ("OLDT", "EQDT", "NEWF", "NEWN", "NOHP"):
        assert f"| {t} |" not in text
    # nic nowego -> "brak" pod naglowkiem
    empty = trend_context_entries(rows[:2] + rows[3:], PREV_D)
    assert empty == []
    lines = _render_risk_section(_risk_state(empty))
    i = lines.index("### Kontekst trendu (nie jest stopem)")
    assert lines[i + 1] == "brak"


# ---------------------------------------------------------------- Codex m1 / m2
def test_codex_m1_two_n_uses_atr20_from_last_remaining_lot_session_not_d():
    """Dwa loty @100: sesje 25 i 27; ATR20: 2 (sesja 25), 3 (27), 9 (D=29). 2N z
    ATR20 ostatniego pozostalego lotu: 100 - 2*3 = 94 (nie 96 z ATR sesji 25,
    nie 82 z ATR na D)."""
    n = 30
    bars = ([D(101)] * n, [D(99)] * n, [D(100)] * n)
    atr20 = [2] * n
    atr20[27] = 3
    atr20[29] = 9
    for rule in (RULE_PREV, RULE_419):
        ps = case(bars, [(25, 1, 100), (27, 1, 100)], is_short=False, rule=rule, anchor_idx=25,
                  anchor_date=OCT1, atr=2, atr20=atr20)
        assert ps.stop_2n == D(94)


def test_codex_m2_two_n_change_masked_by_chandelier_effective_unchanged():
    """Dane T14: B1 vs stara kotwica przy wiazacym Chandelierze (stara reguła A
    wylaczona): stop efektywny 106 bez zmiany, zrodlo 'chandelier', ale skladnik
    stop_2n 96 vs 100 i kotwica 100 vs 104."""
    prev, b1 = _t14(RULE_PREV), _t14(B1_ONLY)
    assert res(prev) == res(b1) == (D(106), "chandelier")
    assert (prev.stop_2n, b1.stop_2n) == (D(100), D(96))
    assert (prev.entry_price, b1.entry_price) == (D(104), D(100))


# ---------------------------------------------------------------- krotkie
def test_prev_rule_needed():
    assert prev_rule_needed(set()) is False
    assert prev_rule_needed({None}) is True
    assert prev_rule_needed({R.STOP_RULE_VERSION}) is False
    assert prev_rule_needed({"4.19"}) is False
    assert prev_rule_needed({"4.19", None}) is True
    assert prev_rule_needed({"4.18"}) is True


def test_rule_change_direction_and_note():
    assert rule_change_direction("long", D(100), D(99)) == "zaostrzenie"
    assert rule_change_direction("long", D(98), D(99)) == "poluzowanie"
    assert rule_change_direction("short", D(98), D(99)) == "zaostrzenie"
    assert rule_change_direction("short", D(100), D(99)) == "poluzowanie"
    assert rule_change_direction("long", D(99), D(99)) == "0"
    assert rule_change_direction("short", D(99), D(99)) == "0"
    assert rule_change_direction("long", D(99), None) is None
    assert rule_change_direction("long", None, D(99)) is None
    assert rule_change_note(None) is None
    assert rule_change_note("zaostrzenie") == "zmiana reguły rew. 4.19: zaostrzenie"


def _item(ticker, stop_prev_rule=None, is_contract=None, stop_d=D("100"), stop_prev=D("98")):
    base = (ticker, "AKCYJNY 000001", "long", stop_d, "USD", "USD", stop_prev, None, D("2"), False)
    if stop_prev_rule is None and is_contract is None:
        return base
    return base + (stop_prev_rule, bool(is_contract))


def _orders_text(items):
    state = ReportState(d=date(2026, 10, 15), run_started_at=datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc))
    state.prev_risk_date = date(2026, 10, 14)
    state.stop_order_rows = build_stop_order_rows(items)
    return "\n".join(_render_stop_orders_section(state))


def test_m62_rule_change_note_only_with_stop_prev_rule():
    with_prev = _orders_text([_item("FKTEST", stop_prev_rule=D("95"), is_contract=False)])
    assert "zmiana reguły rew. 4.19: zaostrzenie" in with_prev  # long: 100 > 95
    without = _orders_text([_item("FKTEST")])
    assert "zmiana reguły" not in without
    assert "FKTEST" in without
    none_prev = _orders_text([_item("FKTEST", stop_prev_rule=None, is_contract=False)])
    assert "zmiana reguły" not in none_prev


def test_m62_contract_note_only_when_contract_present():
    assert STOP_ORDER_CONTRACT_NOTE not in _orders_text([_item("FKTEST", stop_prev_rule=D("95"), is_contract=False)])
    assert STOP_ORDER_CONTRACT_NOTE not in _orders_text([_item("FKTEST")])
    mixed = _orders_text([
        _item("FKTEST", stop_prev_rule=D("95"), is_contract=False),
        _item("FKCONT", stop_prev_rule=D("95"), is_contract=True),
    ])
    assert STOP_ORDER_CONTRACT_NOTE in mixed
    assert mixed.count(STOP_ORDER_CONTRACT_NOTE) == 1

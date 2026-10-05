"""Testy czyste (bez bazy) B-52 — dokupienie poniżej stopu (brief CC-B52 rew. 3).

Daty syntetyczne (1990). Zero zależności od bazy/sieci."""

from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal

from mannaz.cycle import (
    STAGE_RISK,
    BuyBelowStopCandidate,
    ReportState,
    buy_below_stop_events,
    has_data_failures,
    quantity_timeline,
    render_report,
)
from mannaz.risk import convert_entry_rows_to_quote

PREV = date(1990, 1, 5)
D = date(1990, 1, 12)
TXN = date(1990, 1, 9)
HOLD = date(1989, 12, 1)


def _cand(**kw) -> BuyBelowStopCandidate:
    base = dict(
        broker_ticker="AAA",
        rachunek="AKCYJNY 900001",
        instrument_id=1,
        settlement_currency="USD",
        quote_currency="USD",
        instrument_type="equity",
        is_core=False,
        row_type="kupno",
        txn_date=TXN,
        qty=Decimal("10"),
        price_quote=Decimal("90"),
        fx_missing=False,
        qty_before=Decimal("5"),
        holding_period_start=HOLD,
        prior_evals=(
            (date(1989, 12, 29), Decimal("95"), "chandelier"),
            (date(1990, 1, 5), Decimal("100"), "two_n"),
        ),
        stop_d=Decimal("101"),
        stop_d_source="chandelier",
    )
    base.update(kw)
    return BuyBelowStopCandidate(**base)


def test_event_below_stop_has_correct_fields():
    ev = buy_below_stop_events([_cand()], PREV, D)
    assert len(ev) == 1
    e = ev[0]
    assert e.broker_ticker == "AAA"
    assert e.rachunek == "AKCYJNY 900001"
    assert e.settlement_currency == "USD"
    assert e.quote_currency == "USD"
    assert e.txn_date == TXN
    assert e.qty == Decimal("10")
    assert e.price_quote == Decimal("90")
    assert e.fx_missing is False
    assert (e.stop_before, e.stop_before_date, e.stop_before_source) == (
        Decimal("100"), date(1990, 1, 5), "two_n")
    assert (e.stop_d, e.stop_d_source) == (Decimal("101"), "chandelier")


def test_price_equal_or_above_stop_no_event():
    assert buy_below_stop_events([_cand(price_quote=Decimal("100"))], PREV, D) == []
    assert buy_below_stop_events([_cand(price_quote=Decimal("100.01"))], PREV, D) == []


def test_opening_position_no_event():
    assert buy_below_stop_events([_cand(qty_before=Decimal("0"), holding_period_start=None)], PREV, D) == []


def test_no_eval_row_in_holding_period_no_event():
    c = _cand(holding_period_start=date(1990, 1, 6), prior_evals=((date(1990, 1, 5), Decimal("100"), "x"),))
    assert buy_below_stop_events([c], PREV, D) == []
    # ocena z dnia transakcji nie liczy się (risk_date < d)
    c2 = _cand(prior_evals=((TXN, Decimal("100"), "x"),))
    assert buy_below_stop_events([c2], PREV, D) == []
    # ostatnia ocena z NULL stopem -> brak
    c3 = _cand(prior_evals=((date(1990, 1, 5), None, None),))
    assert buy_below_stop_events([c3], PREV, D) == []


def test_window_boundaries():
    assert buy_below_stop_events([_cand(txn_date=PREV)], PREV, D) == []
    assert buy_below_stop_events([_cand(txn_date=date(1990, 1, 13))], PREV, D) == []
    assert len(buy_below_stop_events([_cand(txn_date=D)], PREV, D)) == 1
    assert len(buy_below_stop_events([_cand(txn_date=date(1990, 1, 6))], PREV, D)) == 1
    assert buy_below_stop_events([_cand()], None, D) == []


def test_core_contract_and_sale_excluded():
    assert buy_below_stop_events([_cand(is_core=True)], PREV, D) == []
    assert buy_below_stop_events([_cand(instrument_type="future")], PREV, D) == []
    assert buy_below_stop_events([_cand(row_type="sprzedaz")], PREV, D) == []


def test_etf_included_and_sorted_by_date_then_ticker():
    cs = [
        _cand(broker_ticker="ZZZ", txn_date=date(1990, 1, 8)),
        _cand(broker_ticker="BBB", instrument_type="etf", txn_date=date(1990, 1, 9)),
        _cand(broker_ticker="AAA", txn_date=date(1990, 1, 9)),
    ]
    ev = buy_below_stop_events(cs, PREV, D)
    assert [(e.txn_date.day, e.broker_ticker) for e in ev] == [(8, "ZZZ"), (9, "AAA"), (9, "BBB")]


def test_cross_currency_compare_uses_converted_price_and_helper():
    # EUR rozliczenie, CAD notowanie (jak CSU): przeliczenie kursem z dnia transakcji.
    rates = {("EUR", TXN): Decimal("4"), ("CAD", TXN): Decimal("3")}
    rows = [{"date": TXN, "price": Decimal("75")}]
    conv = convert_entry_rows_to_quote(rows, "EUR", "CAD", lambda c, d: rates.get((c, d)))
    assert conv[0]["price"] == Decimal("100")  # 75 * 4 / 3
    # cena przeliczona 100 vs stop 100 -> brak (ostra nierównosc), 99 -> jest
    base = dict(settlement_currency="EUR", quote_currency="CAD")
    assert buy_below_stop_events([_cand(price_quote=conv[0]["price"], **base)], PREV, D) == []
    ev = buy_below_stop_events([_cand(price_quote=Decimal("99"), **base)], PREV, D)
    assert len(ev) == 1 and ev[0].settlement_currency == "EUR" and ev[0].quote_currency == "CAD"


def test_fx_missing_shown_without_comparison():
    rows = [{"date": TXN, "price": Decimal("75")}]
    conv = convert_entry_rows_to_quote(rows, "EUR", "CAD", lambda c, d: None)
    assert conv[0]["price"] is None and conv[0]["fx_missing"] is True
    ev = buy_below_stop_events(
        [_cand(settlement_currency="EUR", quote_currency="CAD", price_quote=None, fx_missing=True)], PREV, D)
    assert len(ev) == 1 and ev[0].price_quote is None and ev[0].fx_missing is True
    # bez stopu przed nawet brak kursu nie tworzy zdarzenia
    assert buy_below_stop_events([_cand(price_quote=None, fx_missing=True, prior_evals=())], PREV, D) == []


def _t(day, rt, qty):
    return {"date": date(1990, 1, day), "row_type": rt, "qty": Decimal(qty)}


def test_quantity_timeline_close_to_zero_and_reopen():
    txns = [
        _t(1, "kupno", "10"),
        _t(2, "kupno", "5"),
        _t(3, "sprzedaz", "15"),
        _t(4, "kupno", "7"),
        _t(5, "kupno", "3"),
    ]
    tl = quantity_timeline(txns)
    assert tl[0] == (Decimal("0"), None)
    assert tl[1] == (Decimal("10"), date(1990, 1, 1))
    assert tl[2] == (Decimal("15"), date(1990, 1, 1))
    assert tl[3] == (Decimal("0"), None)
    # po ponownym otwarciu okres posiadania zaczyna sie od 4 stycznia
    assert tl[4] == (Decimal("7"), date(1990, 1, 4))
    assert quantity_timeline(txns + [_t(6, "kupno", "1")])[5] == (Decimal("10"), date(1990, 1, 4))


def test_quantity_timeline_same_day_order_and_split_ignored():
    txns = [
        _t(1, "kupno", "10"),
        _t(1, "sprzedaz", "10"),
        _t(1, "kupno", "4"),
        {"date": date(1990, 1, 2), "row_type": "split", "qty": Decimal("0")},
        _t(2, "kupno", "1"),
    ]
    tl = quantity_timeline(txns)
    assert tl[0][0] == 0
    assert tl[1] == (Decimal("10"), date(1990, 1, 1))
    assert tl[2] == (Decimal("0"), None)  # kupno po zamknieciu tego samego dnia = otwarcie
    assert tl[3] == (Decimal("4"), date(1990, 1, 1))
    assert tl[4] == (Decimal("4"), date(1990, 1, 1))


def _state(**kw) -> ReportState:
    s = ReportState(d=D, run_started_at=datetime(1990, 1, 14, 8, 0, tzinfo=timezone.utc), prev_risk_date=PREV)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_render_section_right_after_stop_section_and_row_content():
    ev = buy_below_stop_events([_cand(price_quote=Decimal("90.5"))], PREV, D)
    text = render_report(_state(buy_below_stop_events=ev))
    i_stop = text.index("## 🔴 STOP")
    i_bbs = text.index("## Dokupienia poniżej stopu (B-52)")
    i_next = text.index("## Zlecenia stop do aktualizacji u brokera (M62)")
    assert i_stop < i_bbs < i_next
    section = text[i_bbs:i_next]
    assert "| ticker | rachunek | waluta rozliczenia | data | ilość | cena (waluta notowania) |" in section
    assert "stop przed (data oceny, źródło) | stop na D (źródło) |" in section
    assert "90,50 USD" in section
    assert "100,00 USD (1990-01-05, two_n)" in section
    assert "101,00 USD (chandelier)" in section
    assert "kontrakty poza zakresem sygnału (stop na bazie)." in section
    assert f"D={D.isoformat()}" in section and f"poprzednia ocena={PREV.isoformat()}" in section
    assert "AKCYJNY" in section and "900001" not in text


def test_render_brak_when_empty_and_nie_wykonano_when_risk_stage_missing():
    text = render_report(_state())
    sec = text[text.index("## Dokupienia poniżej stopu (B-52)"):text.index("## Zlecenia stop do aktualizacji")]
    assert sec.rstrip().endswith("brak")
    text2 = render_report(_state(stage_not_executed={STAGE_RISK}))
    sec2 = text2[text2.index("## Dokupienia poniżej stopu (B-52)"):text2.index("## Zlecenia stop do aktualizacji")]
    assert "nie wykonano (cykl zatrzymany na etapie" in sec2


def test_render_fx_missing_label():
    ev = buy_below_stop_events([_cand(price_quote=None, fx_missing=True)], PREV, D)
    text = render_report(_state(buy_below_stop_events=ev))
    assert "brak kursu FX" in text


def test_has_data_failures_unaffected_by_events():
    ev = buy_below_stop_events([_cand()], PREV, D)
    assert ev
    assert has_data_failures(_state(buy_below_stop_events=ev)) is False

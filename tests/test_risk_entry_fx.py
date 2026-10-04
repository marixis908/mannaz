"""Testy B-41 (brief CC-W): przeliczenie ceny lotow settlement != quote kursem NBP
z dnia transakcji. Dane syntetyczne, czyste funkcje, bez bazy."""

from datetime import date
from decimal import Decimal

from mannaz.risk import (
    compute_weighted_entry_price,
    convert_entry_rows_to_quote,
    resolve_fx_rate,
    two_n_stop,
)

D = Decimal


def _lookup_from(table):
    """fx_lookup oparty na liscie (date, rate) per waluta przez resolve_fx_rate."""

    def f(cur, d):
        return resolve_fx_rate(table.get(cur, []), cur, d)[0]

    return f


EUR = [(date(2026, 3, 2), D("4.30")), (date(2026, 3, 9), D("4.40")), (date(2026, 3, 13), D("4.50"))]
USD = [(date(2026, 3, 2), D("3.90")), (date(2026, 3, 9), D("4.00")), (date(2026, 3, 13), D("4.10"))]
TABLE = {"EUR": EUR, "USD": USD}


def _buy(d, qty, price):
    return {"date": d, "type": "kupno", "qty": D(qty), "price": D(price)}


def _sell(d, qty, price):
    return {"date": d, "type": "sprzedaz", "qty": D(qty), "price": D(price)}


def test_cross_lot_eur_on_usd_instrument_converted_and_stop_from_converted_entry():
    rows = [_buy(date(2026, 3, 9), 10, "100")]
    conv = convert_entry_rows_to_quote(rows, "EUR", "USD", _lookup_from(TABLE))
    res = compute_weighted_entry_price(conv, [], allow_short=False)
    expected = D("100") * D("4.40") / D("4.00")
    assert res.entry_price == expected
    assert res.fx_missing is False
    atr = D("3")
    assert two_n_stop(res.entry_price, atr, False) == expected - 2 * atr


def test_two_lots_different_days_each_converted_with_own_rate_then_weighted():
    rows = [_buy(date(2026, 3, 2), 10, "100"), _buy(date(2026, 3, 9), 30, "120")]
    conv = convert_entry_rows_to_quote(rows, "EUR", "USD", _lookup_from(TABLE))
    p1 = D("100") * D("4.30") / D("3.90")
    p2 = D("120") * D("4.40") / D("4.00")
    assert conv[0]["price"] == p1 and conv[1]["price"] == p2
    res = compute_weighted_entry_price(conv, [], allow_short=False)
    assert res.entry_price == (D(10) * p1 + D(30) * p2) / D(40)
    assert res.qty == D(40)


def test_same_currency_returns_rows_unchanged_and_identical_result():
    for cur in ("USD", "PLN"):
        rows = [_buy(date(2026, 3, 2), 10, "100.5"), _buy(date(2026, 3, 9), 5, "99"), _sell(date(2026, 3, 13), 4, "101")]
        conv = convert_entry_rows_to_quote(rows, cur, cur, _lookup_from({}))
        assert conv is rows and conv == rows
        res = compute_weighted_entry_price(conv, [], False)
        assert res == compute_weighted_entry_price(rows, [], False)
        # FIFO: sprzedaż 4 zdejmuje z lotu 10 @ 100.5 -> 6 @ 100.5 + 5 @ 99
        assert res.entry_price == (D(6) * D("100.5") + D(5) * D("99")) / D(11)
        assert res.fx_missing is False


def test_missing_fx_on_remaining_lot_gives_none_entry_and_flag_no_exception():
    # kurs EUR dopiero od 2026-03-02; lot z 2026-03-01 bez kursu
    rows = [_buy(date(2026, 3, 1), 10, "100")]
    conv = convert_entry_rows_to_quote(rows, "EUR", "USD", _lookup_from(TABLE))
    assert conv[0]["price"] is None and conv[0]["fx_missing"] is True
    res = compute_weighted_entry_price(conv, [], allow_short=False)
    assert res.entry_price is None
    assert res.fx_missing is True
    assert res.qty == D(10)
    assert res.first_remaining_date == date(2026, 3, 1)


def test_missing_fx_only_on_fully_closed_lot_entry_still_computed():
    rows = [
        _buy(date(2026, 3, 1), 10, "100"),  # brak kursu, w pelni zamkniety nizej
        _sell(date(2026, 3, 2), 10, "105"),
        _buy(date(2026, 3, 9), 5, "120"),
    ]
    conv = convert_entry_rows_to_quote(rows, "EUR", "USD", _lookup_from(TABLE))
    res = compute_weighted_entry_price(conv, [], allow_short=False)
    assert res.fx_missing is False
    assert res.entry_price == D("120") * D("4.40") / D("4.00")
    assert res.qty == D(5)


def test_fx_lookup_weekend_uses_last_fixing_on_or_before_date():
    lk = _lookup_from(TABLE)
    # 2026-03-14 to sobota, 2026-03-15 niedziela; ostatni fixing 2026-03-13 (piatek)
    assert date(2026, 3, 14).weekday() == 5
    assert lk("EUR", date(2026, 3, 14)) == D("4.50")
    assert lk("USD", date(2026, 3, 15)) == D("4.10")
    rows = [_buy(date(2026, 3, 14), 2, "50")]
    conv = convert_entry_rows_to_quote(rows, "EUR", "USD", lk)
    assert conv[0]["price"] == D("50") * D("4.50") / D("4.10")


def test_short_position_cross_currency_entry_converted_stop_plus_2atr():
    rows = [_sell(date(2026, 3, 9), 3, "100")]
    conv = convert_entry_rows_to_quote(rows, "EUR", "USD", _lookup_from(TABLE))
    res = compute_weighted_entry_price(conv, [], allow_short=True)
    expected = D("100") * D("4.40") / D("4.00")
    assert res.entry_price == expected
    assert res.qty == D(-3)
    atr = D("2.5")
    assert two_n_stop(res.entry_price, atr, True) == expected + 2 * atr


def test_pln_settlement_non_pln_quote_uses_unit_rate_for_pln():
    rows = [_buy(date(2026, 3, 9), 1, "400")]
    conv = convert_entry_rows_to_quote(rows, "PLN", "USD", _lookup_from(TABLE))
    assert conv[0]["price"] == D("400") * D(1) / D("4.00")

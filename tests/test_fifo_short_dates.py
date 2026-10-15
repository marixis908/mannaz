"""Testy dat wejscia FIFO dla pozycji krotkich (CC-SH, C2): sprzedaz otwierajaca
lub powiekszajaca lot ujemny (allow_short=True) wyznacza first/last_entry_date.
WYLACZNIE funkcje czyste, dane syntetyczne."""

from datetime import date
from decimal import Decimal

from mannaz.fifo import compute_position

D1 = date(2026, 10, 13)
D2 = date(2026, 10, 14)
D3 = date(2026, 10, 15)


def _row(d, typ, qty, amount):
    return {"date": d, "type": typ, "qty": Decimal(qty), "amount": Decimal(amount)}


def test_a_short_opened_by_sells_fpknz26():
    rows = [
        _row(D1, "sprzedaz", 2, "307.80"),
        _row(D3, "sprzedaz", 1, "152.66"),
        _row(D3, "sprzedaz", 1, "152.66"),
        _row(D3, "sprzedaz", 1, "154.30"),
    ]
    res = compute_position(rows, [], allow_short=True)
    assert res.qty == Decimal(-5)
    assert res.first_entry_date == D1
    assert res.last_entry_date == D3
    assert res.oversell_events == 0


def test_b_short_enlarged_moves_last_only():
    rows = [_row(D1, "sprzedaz", 1, "150"), _row(D2, "sprzedaz", 1, "151")]
    res = compute_position(rows, [], allow_short=True)
    assert res.qty == Decimal(-2)
    assert res.first_entry_date == D1
    assert res.last_entry_date == D2


def test_c_flip_long_to_short_in_one_transaction():
    rows = [
        _row(D1, "kupno", 2, "200"),
        _row(D2, "kupno", 1, "100"),
        _row(D3, "sprzedaz", 5, "500"),
    ]
    res = compute_position(rows, [], allow_short=True)
    assert res.qty == Decimal(-2)
    assert res.first_entry_date == D1
    assert res.last_entry_date == D3


def test_d_control_long_without_sales():
    rows = [_row(D1, "kupno", 2, "200"), _row(D2, "kupno", 1, "100")]
    for allow in (False, True):
        res = compute_position(rows, [], allow_short=allow)
        assert res.qty == Decimal(3)
        assert res.first_entry_date == D1
        assert res.last_entry_date == D2


def test_d_control_partial_closing_sale_does_not_move_last():
    rows = [
        _row(D1, "kupno", 3, "300"),
        _row(D3, "sprzedaz", 1, "110"),
    ]
    res_long = compute_position(rows, [], allow_short=False)
    res_short_ok = compute_position(rows, [], allow_short=True)
    for res in (res_long, res_short_ok):
        assert res.qty == Decimal(2)
        assert res.first_entry_date == D1
        assert res.last_entry_date == D1
    assert res_long == res_short_ok


def test_d_control_full_close_sale_does_not_move_last():
    rows = [_row(D1, "kupno", 2, "200"), _row(D3, "sprzedaz", 2, "220")]
    res = compute_position(rows, [], allow_short=True)
    assert res.qty == Decimal(0)
    assert res.first_entry_date == D1
    assert res.last_entry_date == D1


def test_e_oversell_without_allow_short_unchanged():
    rows = [_row(D1, "sprzedaz", 2, "200")]
    res = compute_position(rows, [], allow_short=False)
    assert res.qty == Decimal(-2)
    assert res.first_entry_date is None
    assert res.last_entry_date is None
    assert res.oversell_events == 1


def test_e_covering_buy_of_short_sets_dates_as_before():
    rows = [_row(D1, "sprzedaz", 2, "300"), _row(D3, "kupno", 1, "140")]
    res = compute_position(rows, [], allow_short=True)
    assert res.qty == Decimal(-1)
    # kupno pokrywajace krotka nadal jest 'kupno': ustawia daty jak dotad;
    # sprzedaz otwierajaca krotka rowniez (D1), kupno dolicza D3 do last.
    assert res.first_entry_date == D1
    assert res.last_entry_date == D3

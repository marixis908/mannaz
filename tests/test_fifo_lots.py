"""B-24 (brief CC-B24, C1): `PositionResult.lots` — końcowy stan kolejki FIFO.
Testy czyste (bez bazy), daty syntetyczne 1990, Decimal.

Niezmienniki D1 (na wyniku `compute_position`):
  sum(l.qty for l in lots) == qty
  sum(l.qty * l.unit_cost for l in lots if l.qty > 0) == residual_cost"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from mannaz.fifo import Lot, compute_position, resolve_position_as_of

D = Decimal


def _buy(d, qty, amount):
    return {"date": d, "type": "kupno", "qty": D(qty), "amount": D(amount)}


def _sell(d, qty, amount):
    return {"date": d, "type": "sprzedaz", "qty": D(qty), "amount": D(amount)}


def _assert_d1(pos):
    assert sum((lot.qty for lot in pos.lots), D(0)) == pos.qty
    assert sum((lot.qty * lot.unit_cost for lot in pos.lots if lot.qty > 0), D(0)) == pos.residual_cost


def test_a_two_buys_partial_sell_one_lot_left():
    rows = [
        _buy(date(1990, 1, 2), 10, 100),   # 10 @ 10
        _buy(date(1990, 2, 1), 10, 200),   # 10 @ 20
        _sell(date(1990, 3, 1), 15, 999),  # zjada lot 1 i 5 z lotu 2
    ]
    pos = compute_position(rows, [])
    assert pos.lots == (Lot(qty=D(5), unit_cost=D(20), entry_date=date(1990, 2, 1), unknown_cost=False),)
    assert pos.qty == D(5)
    assert pos.residual_cost == D(100)
    _assert_d1(pos)


def test_a2_lots_order_oldest_first():
    rows = [_buy(date(1990, 2, 1), 4, 40), _buy(date(1990, 1, 2), 6, 30)]
    pos = compute_position(rows, [])
    assert [lot.entry_date for lot in pos.lots] == [date(1990, 1, 2), date(1990, 2, 1)]
    _assert_d1(pos)


def test_b_split_between_buys_scales_first_lot_only():
    rows = [_buy(date(1990, 1, 2), 10, 100), _buy(date(1990, 3, 1), 5, 100)]
    events = [{"date": date(1990, 2, 1), "ratio": D(2)}]
    pos = compute_position(rows, events)
    first, second = pos.lots
    assert first == Lot(qty=D(20), unit_cost=D(5), entry_date=date(1990, 1, 2), unknown_cost=False)
    assert second == Lot(qty=D(5), unit_cost=D(20), entry_date=date(1990, 3, 1), unknown_cost=False)
    assert pos.qty == D(25)
    assert pos.residual_cost == D(200)
    _assert_d1(pos)


def test_c_short_contract_real_cost_not_unknown():
    pos = compute_position([_sell(date(1990, 1, 2), 3, 30)], [], allow_short=True)
    assert pos.lots == (Lot(qty=D(-3), unit_cost=D(10), entry_date=date(1990, 1, 2), unknown_cost=False),)
    assert pos.qty == D(-3)
    assert pos.residual_cost == D(0)  # tylko loty dodatnie
    assert pos.oversell_events == 0
    _assert_d1(pos)


def test_d_oversell_on_equity_account_unknown_cost():
    rows = [_buy(date(1990, 1, 2), 2, 20), _sell(date(1990, 2, 1), 5, 50)]
    pos = compute_position(rows, [], allow_short=False)
    assert pos.lots == (Lot(qty=D(-3), unit_cost=D(0), entry_date=date(1990, 2, 1), unknown_cost=True),)
    assert pos.oversell_events == 1
    assert pos.qty == D(-3)
    assert pos.residual_cost == D(0)
    _assert_d1(pos)


def test_e_contract_expiry_closes_lots():
    rows = [
        {"date": date(1990, 1, 2), "row_type": "kupno", "qty": D(2), "amount": D(-20)},
    ]
    before = compute_position(
        [{"date": date(1990, 1, 2), "type": "kupno", "qty": D(2), "amount": D(20)}], []
    )
    _assert_d1(before)
    assert len(before.lots) == 1

    pos, eff, closed = resolve_position_as_of(
        rows, [], date(1990, 6, 1), "future", date(1990, 3, 1), [], allow_short=True
    )
    assert closed is True
    assert eff == D(0)
    assert pos.lots == ()
    assert pos.qty == before.qty
    assert pos.residual_cost == before.residual_cost

    # kontrolka ujemna: kontrakt jeszcze nie wygasł -> loty zostają
    pos2, eff2, closed2 = resolve_position_as_of(
        rows, [], date(1990, 2, 1), "future", date(1990, 3, 1), [], allow_short=True
    )
    assert closed2 is False and eff2 == D(2)
    assert pos2.lots == before.lots


def test_e_certificate_redemption_closes_lots():
    rows = [{"date": date(1990, 1, 2), "row_type": "kupno", "qty": D(4), "amount": D(-40)}]
    before = compute_position(
        [{"date": date(1990, 1, 2), "type": "kupno", "qty": D(4), "amount": D(40)}], []
    )
    _assert_d1(before)

    pos, eff, closed = resolve_position_as_of(
        rows, [], date(1990, 6, 1), "certificate", None, [date(1990, 3, 1)]
    )
    assert closed is True
    assert eff == D(0)
    assert pos.lots == ()
    assert pos.qty == before.qty == D(4)
    assert pos.residual_cost == before.residual_cost == D(40)

    # kontrolka ujemna: brak wykupu -> loty zostają
    pos2, eff2, closed2 = resolve_position_as_of(rows, [], date(1990, 6, 1), "certificate", None, [])
    assert closed2 is False and eff2 == D(4)
    assert pos2.lots == before.lots


def test_g_literal_regression_five_fields():
    """Pola qty/residual_cost/first/last/oversell_events — wartości policzone ręcznie."""
    d1, d2, d3, d4 = date(1990, 1, 2), date(1990, 2, 1), date(1990, 3, 1), date(1990, 4, 2)

    # S1: 10@10, 10@20, sprzedaż 15 -> 5 @ 20
    p = compute_position([_buy(d1, 10, 100), _buy(d2, 10, 200), _sell(d3, 15, 1)], [])
    assert (p.qty, p.residual_cost, p.first_entry_date, p.last_entry_date, p.oversell_events) == (
        D(5), D(100), d1, d2, 0,
    )

    # S2: split x2 między kupnami: 20@5 + 5@20
    p = compute_position([_buy(d1, 10, 100), _buy(d3, 5, 100)], [{"date": d2, "ratio": D(2)}])
    assert (p.qty, p.residual_cost, p.first_entry_date, p.last_entry_date, p.oversell_events) == (
        D(25), D(200), d1, d3, 0,
    )

    # S3: krótki kontrakt
    p = compute_position([_sell(d1, 3, 30)], [], allow_short=True)
    assert (p.qty, p.residual_cost, p.first_entry_date, p.last_entry_date, p.oversell_events) == (
        D(-3), D(0), None, None, 0,
    )

    # S4: nadwyżka sprzedaży na rachunku akcyjnym
    p = compute_position([_buy(d1, 2, 20), _sell(d2, 5, 50)], [])
    assert (p.qty, p.residual_cost, p.first_entry_date, p.last_entry_date, p.oversell_events) == (
        D(-3), D(0), d1, d1, 1,
    )

    # S5: pełne zamknięcie, potem nowe kupno; first/last po WSZYSTKICH kupnach
    p = compute_position([_buy(d1, 5, 50), _sell(d2, 5, 60), _buy(d4, 1, 7)], [])
    assert (p.qty, p.residual_cost, p.first_entry_date, p.last_entry_date, p.oversell_events) == (
        D(1), D(7), d1, d4, 0,
    )
    assert p.lots == (Lot(qty=D(1), unit_cost=D(7), entry_date=d4, unknown_cost=False),)
    _assert_d1(p)

    # S6: pusty wejściowy zbiór
    p = compute_position([], [])
    assert (p.qty, p.residual_cost, p.first_entry_date, p.last_entry_date, p.oversell_events) == (
        D(0), D(0), None, None, 0,
    )
    assert p.lots == ()

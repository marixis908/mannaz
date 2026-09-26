"""Testy czystej logiki FIFO — wyłącznie syntetyczne dane. Brief CC-P P2.5
+ poprawka P2.5 (kontrakty ze znakiem, wygasanie)."""

from datetime import date
from decimal import Decimal

from mannaz.fifo import compute_position, resolve_effective_qty_after_expiry


def test_simple_buy_then_partial_sell():
    rows = [
        {"date": date(2024, 1, 1), "type": "kupno", "qty": Decimal(10), "amount": Decimal(100)},
        {"date": date(2024, 2, 1), "type": "sprzedaz", "qty": Decimal(4), "amount": Decimal(50)},
    ]
    pos = compute_position(rows, events=[])
    assert pos.qty == Decimal(6)
    assert pos.residual_cost == Decimal(60)  # 6 jednostek * koszt jedn. 10
    assert pos.first_entry_date == date(2024, 1, 1)
    assert pos.last_entry_date == date(2024, 1, 1)
    assert pos.oversell_events == 0


def test_two_buys_fifo_order_consumed_oldest_first():
    rows = [
        {"date": date(2024, 1, 1), "type": "kupno", "qty": Decimal(5), "amount": Decimal(50)},   # koszt jedn. 10
        {"date": date(2024, 1, 10), "type": "kupno", "qty": Decimal(5), "amount": Decimal(100)},  # koszt jedn. 20
        {"date": date(2024, 2, 1), "type": "sprzedaz", "qty": Decimal(5), "amount": Decimal(75)},
    ]
    pos = compute_position(rows, events=[])
    assert pos.qty == Decimal(5)
    assert pos.residual_cost == Decimal(100)  # zostaje tylko drugi lot (koszt jedn. 20 * 5)


def test_full_close_gives_zero_qty_and_cost():
    rows = [
        {"date": date(2024, 1, 1), "type": "kupno", "qty": Decimal(10), "amount": Decimal(100)},
        {"date": date(2024, 2, 1), "type": "sprzedaz", "qty": Decimal(10), "amount": Decimal(150)},
    ]
    pos = compute_position(rows, events=[])
    assert pos.qty == Decimal(0)
    assert pos.residual_cost == Decimal(0)


def test_oversell_without_prior_buy_creates_negative_qty_and_is_counted():
    rows = [
        {"date": date(2024, 1, 1), "type": "sprzedaz", "qty": Decimal(3), "amount": Decimal(30)},
    ]
    pos = compute_position(rows, events=[])
    assert pos.qty == Decimal(-3)
    assert pos.oversell_events == 1
    assert pos.residual_cost == Decimal(0)  # nie liczymy kosztu dla lotów ujemnych


def test_split_ratio_applied_before_fifo_scales_prior_lots():
    rows = [
        {"date": date(2024, 1, 1), "type": "kupno", "qty": Decimal(10), "amount": Decimal(1000)},  # koszt jedn. 100
    ]
    events = [{"date": date(2024, 6, 1), "ratio": Decimal(25)}]  # split 25:1
    pos = compute_position(rows, events)
    assert pos.qty == Decimal(250)  # 10 * 25
    assert pos.residual_cost == Decimal(1000)  # koszt całkowity się nie zmienia, tylko jednostkowy


def test_event_after_all_transactions_has_no_effect():
    rows = [
        {"date": date(2024, 1, 1), "type": "kupno", "qty": Decimal(10), "amount": Decimal(100)},
        {"date": date(2024, 2, 1), "type": "sprzedaz", "qty": Decimal(10), "amount": Decimal(150)},
    ]
    events = [{"date": date(2024, 6, 1), "ratio": Decimal(2)}]  # po zamknięciu pozycji
    pos = compute_position(rows, events)
    assert pos.qty == Decimal(0)


# ---------------------------------------------------------------------------
# allow_short — rachunek KONTRAKTOWY: sprzedaż bez pozycji = świadome
# otwarcie krótkiej (brief CC-P, poprawka P2.5)
# ---------------------------------------------------------------------------


def test_short_open_without_prior_buy_not_counted_as_oversell():
    rows = [
        {"date": date(2024, 1, 1), "type": "sprzedaz", "qty": Decimal(3), "amount": Decimal(300)},
    ]
    pos = compute_position(rows, events=[], allow_short=True)
    assert pos.qty == Decimal(-3)
    assert pos.oversell_events == 0  # świadomy short, nie anomalia


def test_short_open_gets_real_unit_cost_not_zero():
    rows = [
        {"date": date(2024, 1, 1), "type": "sprzedaz", "qty": Decimal(2), "amount": Decimal(200)},  # 100/szt.
        {"date": date(2024, 2, 1), "type": "kupno", "qty": Decimal(2), "amount": Decimal(160)},  # pokrycie 80/szt.
    ]
    pos = compute_position(rows, events=[], allow_short=True)
    assert pos.qty == Decimal(0)  # short w pełni pokryty
    assert pos.oversell_events == 0


def test_short_partially_covered_by_later_buy_nets_correctly():
    rows = [
        {"date": date(2024, 1, 1), "type": "sprzedaz", "qty": Decimal(5), "amount": Decimal(500)},
        {"date": date(2024, 2, 1), "type": "kupno", "qty": Decimal(3), "amount": Decimal(240)},
    ]
    pos = compute_position(rows, events=[], allow_short=True)
    assert pos.qty == Decimal(-2)
    assert pos.oversell_events == 0


def test_without_allow_short_same_scenario_is_still_flagged_as_oversell():
    rows = [
        {"date": date(2024, 1, 1), "type": "sprzedaz", "qty": Decimal(3), "amount": Decimal(300)},
    ]
    pos = compute_position(rows, events=[], allow_short=False)
    assert pos.qty == Decimal(-3)
    assert pos.oversell_events == 1
    assert pos.residual_cost == Decimal(0)


# ---------------------------------------------------------------------------
# resolve_effective_qty_after_expiry — kontrakty wygasłe bez transakcji
# zamykającej (brief CC-P, poprawka P2.5)
# ---------------------------------------------------------------------------


def test_expired_future_with_open_qty_is_force_closed():
    qty, closed = resolve_effective_qty_after_expiry(
        qty=Decimal(-1),
        instrument_type="future",
        contract_expiry=date(2026, 9, 18),
        as_of=date(2026, 9, 26),
    )
    assert qty == Decimal(0)
    assert closed is True


def test_future_not_yet_expired_stays_open():
    qty, closed = resolve_effective_qty_after_expiry(
        qty=Decimal(-1),
        instrument_type="future",
        contract_expiry=date(2026, 12, 18),
        as_of=date(2026, 9, 26),
    )
    assert qty == Decimal(-1)
    assert closed is False


def test_non_future_instrument_never_force_closed():
    qty, closed = resolve_effective_qty_after_expiry(
        qty=Decimal(-1),
        instrument_type="equity",
        contract_expiry=None,
        as_of=date(2026, 9, 26),
    )
    assert qty == Decimal(-1)
    assert closed is False


def test_future_with_unknown_expiry_stays_open():
    qty, closed = resolve_effective_qty_after_expiry(
        qty=Decimal(5),
        instrument_type="future",
        contract_expiry=None,
        as_of=date(2026, 9, 26),
    )
    assert qty == Decimal(5)
    assert closed is False


def test_expired_future_already_flat_is_not_counted_as_closure():
    qty, closed = resolve_effective_qty_after_expiry(
        qty=Decimal(0),
        instrument_type="future",
        contract_expiry=date(2026, 9, 18),
        as_of=date(2026, 9, 26),
    )
    assert qty == Decimal(0)
    assert closed is False

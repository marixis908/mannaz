"""Testy czystej logiki FIFO — wyłącznie syntetyczne dane. Brief CC-P P2.5
+ poprawka P2.5 (kontrakty ze znakiem, wygasanie)."""

from datetime import date
from decimal import Decimal

from mannaz.fifo import compute_position, resolve_effective_qty_after_expiry, resolve_position_as_of


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


# ---------------------------------------------------------------------------
# resolve_position_as_of — brief CC-S, S2 (naprawa F1: look-ahead w run_risk;
# F2: wykup certyfikatu bez filtra as_of w podzapytaniu max(transaction_date))
# ---------------------------------------------------------------------------


def test_resolve_position_as_of_sold_after_d_is_open_on_d_and_closed_on_sale_date():
    rows = [
        {"date": date(2024, 1, 1), "row_type": "kupno", "qty": Decimal(10), "amount": Decimal(100)},
        {"date": date(2024, 3, 1), "row_type": "sprzedaz", "qty": Decimal(10), "amount": Decimal(150)},
    ]
    # D przed sprzedaza -> pozycja wciaz otwarta (F1: bez look-ahead na sprzedaz)
    pos, effective_qty, closed = resolve_position_as_of(
        rows, events=[], as_of=date(2024, 2, 1), instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert effective_qty == Decimal(10)
    assert closed is False

    # data sprzedazy wlacznie -> pozycja domknieta
    pos, effective_qty, closed = resolve_position_as_of(
        rows, events=[], as_of=date(2024, 3, 1), instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert effective_qty == Decimal(0)


def test_resolve_position_as_of_split_after_d_does_not_scale_qty_on_d():
    rows = [
        {"date": date(2024, 1, 1), "row_type": "kupno", "qty": Decimal(10), "amount": Decimal(1000)},
    ]
    events = [{"date": date(2024, 6, 1), "ratio": Decimal(10)}]  # split 10:1, po D

    pos, effective_qty, closed = resolve_position_as_of(
        rows, events, as_of=date(2024, 3, 1), instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert effective_qty == Decimal(10)  # split jeszcze nie mial miejsca "z perspektywy D"
    assert closed is False

    pos, effective_qty, closed = resolve_position_as_of(
        rows, events, as_of=date(2024, 7, 1), instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert effective_qty == Decimal(100)  # po dacie splitu -> juz przeskalowane


def test_resolve_position_as_of_share_exchange_flips_on_exchange_date():
    exchange_date = date(2024, 5, 1)
    # instrument X (wydawany w zamianie) — otwarty od 2024-01-01, wydanie na exchange_date
    rows_x = [
        {"date": date(2024, 1, 1), "row_type": "kupno", "qty": Decimal(10), "amount": Decimal(100)},
        {"date": exchange_date, "row_type": "zamiana_wydanie", "qty": Decimal(10), "amount": Decimal(120)},
    ]
    # instrument Y (przyjmowany w zamianie) — istnieje tylko od exchange_date
    rows_y = [
        {"date": exchange_date, "row_type": "zamiana_przyjecie", "qty": Decimal(10), "amount": Decimal(120)},
    ]

    day_before = date(2024, 4, 30)

    _, qty_x_before, closed_x_before = resolve_position_as_of(
        rows_x, events=[], as_of=day_before, instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert qty_x_before == Decimal(10) and closed_x_before is False

    _, qty_y_before, closed_y_before = resolve_position_as_of(
        rows_y, events=[], as_of=day_before, instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert qty_y_before == Decimal(0)  # Y jeszcze nie istnieje

    _, qty_x_on, closed_x_on = resolve_position_as_of(
        rows_x, events=[], as_of=exchange_date, instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert qty_x_on == Decimal(0)  # X domkniety wydaniem

    _, qty_y_on, closed_y_on = resolve_position_as_of(
        rows_y, events=[], as_of=exchange_date, instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert qty_y_on == Decimal(10) and closed_y_on is False  # Y otwarty


def test_resolve_position_as_of_bilans_otwarcia_absent_before_balance_date():
    balance_date = date(2024, 1, 1)
    rows = [
        {"date": balance_date, "row_type": "bilans_otwarcia", "qty": Decimal(5), "amount": Decimal(500)},
    ]

    _, qty_before, _ = resolve_position_as_of(
        rows, events=[], as_of=date(2023, 12, 31), instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert qty_before == Decimal(0)

    _, qty_on, _ = resolve_position_as_of(
        rows, events=[], as_of=balance_date, instrument_type="equity",
        contract_expiry=None, certificate_redemption_dates=[],
    )
    assert qty_on == Decimal(5)


def test_resolve_position_as_of_certificate_redemption_after_d_does_not_close_on_d():
    rows = [
        {"date": date(2024, 1, 1), "row_type": "kupno", "qty": Decimal(10), "amount": Decimal(1000)},
    ]
    redemption_date = date(2024, 6, 1)

    _, effective_qty, closed = resolve_position_as_of(
        rows, events=[], as_of=date(2024, 3, 1), instrument_type="certificate",
        contract_expiry=None, certificate_redemption_dates=[redemption_date],
    )
    assert effective_qty == Decimal(10)
    assert closed is False

    # na/po dacie wykupu (bez kolejnej transakcji) -> domkniete
    _, effective_qty, closed = resolve_position_as_of(
        rows, events=[], as_of=redemption_date, instrument_type="certificate",
        contract_expiry=None, certificate_redemption_dates=[redemption_date],
    )
    assert effective_qty == Decimal(0)
    assert closed is True


def test_resolve_position_as_of_f2_later_repurchase_after_as_of_does_not_mask_redemption():
    """F2: stary kod liczyl max(transaction_date) bez filtra <= as_of — pozniejszy
    odkup (data > as_of, ale <= 'dzis') falszywie maskowal wykup widoczny na D."""
    redemption_date = date(2024, 6, 1)
    rows = [
        {"date": date(2024, 1, 1), "row_type": "kupno", "qty": Decimal(10), "amount": Decimal(1000)},
        {"date": date(2024, 9, 1), "row_type": "kupno", "qty": Decimal(5), "amount": Decimal(500)},  # po D
    ]
    d = date(2024, 7, 1)  # miedzy wykupem a pozniejszym odkupem

    _, effective_qty, closed = resolve_position_as_of(
        rows, events=[], as_of=d, instrument_type="certificate",
        contract_expiry=None, certificate_redemption_dates=[redemption_date],
    )
    assert closed is True
    assert effective_qty == Decimal(0)

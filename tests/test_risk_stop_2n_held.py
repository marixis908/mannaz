"""B-52 (brief CC-B52): zapadka 2N w stopie efektywnym — `stop_2n_held` i
`stop_effective` z kandydatem 'two_n_held'. Daty syntetyczne (1990), ATR
podawany wprost (seria `atr20` = 1 dla kazdej sesji, chyba ze test mowi inaczej)."""

from datetime import date, timedelta
from decimal import Decimal

from mannaz.risk import (
    holding_period_start,
    stop_2n_held,
    stop_effective,
    two_n_stop,
)

D = Decimal
BASE = date(1990, 1, 1)


def day(n: int) -> date:
    return BASE + timedelta(days=n)


DATES = [day(i) for i in range(40)]
ATR1 = [D(1)] * 40


def buy(n, qty, price, **kw):
    return {"date": day(n), "type": "kupno", "qty": D(qty), "price": D(price), **kw}


def sell(n, qty, price):
    return {"date": day(n), "type": "sprzedaz", "qty": D(qty), "price": D(price)}


def run(rows, as_of_n, is_short=False, allow_short=False, events=None, atr=ATR1, lf=D(1), start_n=None):
    events = events or []
    start = day(start_n) if start_n is not None else holding_period_start(rows, events)
    from mannaz.risk import compute_weighted_entry_price, layer_factor_after

    ew = compute_weighted_entry_price(rows, events, allow_short)
    idx = DATES.index(ew.last_remaining_date) if ew.last_remaining_date else None
    cur = two_n_stop(
        ew.entry_price / (layer_factor_after(events, day(as_of_n)) * lf) if ew.entry_price is not None else None,
        atr[idx] if idx is not None else None,
        is_short,
    )
    held = stop_2n_held(rows, events, DATES, atr, start, day(as_of_n), lf, is_short, allow_short, cur)
    return cur, held


def test_buy_below_entry_keeps_stop_two_n_held():
    cur, held = run([buy(1, 10, 100), buy(5, 10, 80)], 10)
    assert cur == D(88) and held == D(98)
    assert stop_effective(cur, D(50), False, held) == (D(98), "two_n_held")


def test_buy_above_entry_two_n_rises():
    cur, held = run([buy(1, 10, 100), buy(5, 10, 120)], 10)
    assert cur == D(108) and held == D(108)
    assert stop_effective(cur, D(50), False, held) == (D(108), "two_n")


def test_chandelier_above_two_n_wins():
    cur, held = run([buy(1, 10, 100), buy(5, 10, 80)], 10)
    assert stop_effective(cur, D(200), False, held) == (D(200), "chandelier")


def test_close_and_reopen_resets_ratchet():
    rows = [buy(1, 10, 100), sell(3, 10, 110), buy(6, 10, 50), buy(8, 10, 40)]
    assert holding_period_start(rows, []) == day(6)
    cur, held = run(rows, 12)
    # punkt = d6 (po zamknieciu): 50 - 2 = 48; wczesniejsze 98 nie wchodzi
    assert held == D(48)
    assert cur == D("43")  # (50+40)/2 = 45 -> 43


def test_short_mirror():
    rows_a = [sell(1, 10, 100), sell(5, 10, 120)]  # srednia 110 -> 112, punkt d1 -> 102
    cur, held = run(rows_a, 10, is_short=True, allow_short=True)
    assert cur == D(112) and held == D(102)
    assert stop_effective(cur, D(300), True, held) == (D(102), "two_n_held")
    rows_b = [sell(1, 10, 100), sell(5, 10, 80)]  # srednia 90 -> 92 < 102
    cur, held = run(rows_b, 10, is_short=True, allow_short=True)
    assert cur == D(92) and held == D(92)
    assert stop_effective(cur, D(300), True, held) == (D(92), "two_n")
    assert stop_effective(cur, D(50), True, held) == (D(50), "chandelier")


def test_no_atr_behaves_as_before():
    atr_none = [None] * 40
    cur, held = run([buy(1, 10, 100), buy(5, 10, 80)], 10, atr=atr_none)
    assert cur is None and held is None
    assert stop_effective(cur, D(50), False, held) == (D(50), "chandelier")
    assert stop_effective(None, None, False, None) == (None, None)


def test_fx_missing_points_skipped():
    rows = [buy(1, 10, 100, fx_missing=True), buy(5, 10, 80, fx_missing=True)]
    cur = D(77)
    held = stop_2n_held(rows, [], DATES, ATR1, day(1), day(10), D(1), False, False, cur)
    assert held == cur
    assert stop_effective(cur, None, False, held) == (D(77), "two_n")


def test_partial_sale_lowering_weighted_price_keeps_stop():
    rows = [buy(1, 10, 100), buy(3, 10, 80), sell(5, 10, 90)]  # FIFO zdejmuje lot 100
    cur, held = run(rows, 10)
    assert cur == D(78)
    assert held == D(98)
    assert stop_effective(cur, None, False, held) == (D(98), "two_n_held")


def test_no_transactions_in_window_returns_same_value():
    rows = [buy(1, 10, 100)]
    cur = D(98)
    held = stop_2n_held(rows, [], DATES, ATR1, day(1), day(10), D(1), False, False, cur)
    assert held is cur
    assert stop_effective(cur, D(90), False, held) == (D(98), "two_n")
    assert stop_effective(cur, D(90), False) == (D(98), "two_n")  # 3 argumenty jak dzis


def test_stop_effective_held_not_better_not_listed():
    assert stop_effective(D(10), None, False, D(10)) == (D(10), "two_n")
    assert stop_effective(None, D(5), False, D(7)) == (D(7), "two_n_held")


def test_split_in_window_after_point_layer_adjusted():
    events = [{"date": day(4), "ratio": D(2)}]
    rows = [buy(1, 10, 100), buy(6, 10, 40)]
    cur, held = run(rows, 10, events=events)
    assert held == D(48)  # 100/2 - 2*1
    # przyszly split (po as_of) -> dodatkowy czynnik warstwy
    held_f = stop_2n_held(rows, events, DATES, ATR1, day(1), day(10), D(2), False, False, D(0))
    assert held_f == D(23)  # 100/(2*2) - 2

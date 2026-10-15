"""Pomiar (CC-SH, C3): krotka pozycja na kontrakcie (qty -5, mnoznik 10) przez
funkcje risk.py skladane tak jak w run_risk. Dane syntetyczne, funkcje czyste,
bez bazy. Tylko pomiar — risk.py bez zmian."""

from datetime import date, timedelta
from decimal import Decimal

from mannaz import risk
from mannaz.risk import (
    ATR20_PERIOD,
    ATR22_PERIOD,
    aggregate_risk_budgets,
    chandelier_series,
    compute_risk_native,
    compute_weighted_entry_price,
    emitent_key,
    level1_by_name,
    ratchet_extreme,
    rolling_min,
    stop_2n_held,
    stop_effective,
    two_n_stop,
    wilder_atr_series,
)

D = Decimal
MULT = D(10)
QTY = D(-5)
LAST_DAY = date(2026, 10, 15)
SELLS = [
    {"date": date(2026, 10, 13), "type": "sprzedaz", "qty": D(2), "price": D("153.90")},
    {"date": date(2026, 10, 15), "type": "sprzedaz", "qty": D(1), "price": D("152.66")},
    {"date": date(2026, 10, 15), "type": "sprzedaz", "qty": D(1), "price": D("152.66")},
    {"date": date(2026, 10, 15), "type": "sprzedaz", "qty": D(1), "price": D("154.30")},
]
EXPECTED_ENTRY = (D(2) * D("153.90") + D("152.66") + D("152.66") + D("154.30")) / D(5)


def _sessions(n: int, last: date) -> list[date]:
    out: list[date] = []
    d = last
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def _series(n: int = 60):
    """Cena spada do dna w sesji 30, potem rosnie (min(low,22) rosnie po dnie)."""
    dates = _sessions(n, LAST_DAY)
    closes: list[Decimal] = []
    for i in range(n):
        if i <= 30:
            c = D(170) - D(i)  # 170 -> 140
        else:
            c = D(140) + D(i - 30) * D("0.5")  # 140 -> 154.5 -> przesuniete nizej
        closes.append(c)
    # zamkniecie ostatniej sesji ponizej stopu (short zarabia)
    closes[-1] = D("146.00")
    highs = [c + D("1.0") for c in closes]
    lows = [c - D("1.0") for c in closes]
    return dates, highs, lows, closes


def _compose():
    dates, highs, lows, closes = _series()
    atr20 = wilder_atr_series(highs, lows, closes, ATR20_PERIOD)
    atr22 = wilder_atr_series(highs, lows, closes, ATR22_PERIOD)
    chand = chandelier_series(highs, lows, atr22, True)
    return dates, highs, lows, closes, atr20, atr22, chand


def _entry():
    return compute_weighted_entry_price(SELLS, [], allow_short=True)


def test_entry_price_weighted_average_of_sell_prices_positive():
    e = _entry()
    assert e.qty == QTY
    assert e.entry_price == EXPECTED_ENTRY == D("153.484")
    assert e.entry_price > 0
    assert e.first_remaining_date == date(2026, 10, 13)
    assert e.last_remaining_date == LAST_DAY


def test_stop_2n_above_entry_with_atr20_at_last_entry():
    dates, _h, _l, _c, atr20, _a22, _ch = _compose()
    e = _entry()
    idx = dates.index(e.last_remaining_date)
    stop = two_n_stop(e.entry_price, atr20[idx], True)
    assert atr20[idx] is not None and atr20[idx] > 0
    assert stop == e.entry_price + D(2) * atr20[idx]
    assert stop > e.entry_price


def test_chandelier_short_is_min_low_22_plus_3_atr22():
    _d, _h, lows, _c, _a20, atr22, chand = _compose()
    mins = rolling_min(lows, 22)
    last = len(lows) - 1
    assert chand[last] == mins[last] + D(3) * atr22[last]
    assert chand[last] == min(lows[-22:]) + D(3) * atr22[last]
    # przed rozgrzewka ATR22 / okna: None
    assert chand[0] is None and chand[ATR22_PERIOD - 2] is None
    assert chand[ATR22_PERIOD - 1] is not None


def test_ratchet_short_is_min_and_never_rises():
    _d, _h, _l, _c, _a20, _a22, chand = _compose()
    start, end = 0, len(chand) - 1
    vals = [v for v in chand[start : end + 1] if v is not None]
    ratcheted = ratchet_extreme(chand, start, end, True)
    assert ratcheted == min(vals)
    # konstrukcja serii: chandelier ROSNIE po spadku (ostatnia wartosc > minimum)
    imin = chand.index(min(vals))
    assert imin < end and chand[end] > min(vals)
    assert ratcheted < chand[end]
    # zapadka w czasie: zadne rozszerzenie okna nie podnosi wyniku
    prev = None
    for e in range(ATR22_PERIOD - 1, end + 1):
        cur = ratchet_extreme(chand, start, e, True)
        if prev is not None:
            assert cur <= prev
        prev = cur


def test_stop_effective_short_is_min_and_risk_native_positive():
    dates, _h, _l, closes, atr20, _a22, chand = _compose()
    e = _entry()
    stop_2n = two_n_stop(e.entry_price, atr20[dates.index(e.last_remaining_date)], True)
    d_idx = len(dates) - 1
    stop_ch = ratchet_extreme(chand, dates.index(e.first_remaining_date), d_idx, True)
    eff, src = stop_effective(stop_2n, stop_ch, True)
    assert eff == min(stop_2n, stop_ch)
    assert src == ("two_n" if stop_2n <= stop_ch else "chandelier")
    # B-52: zapadka 2N (short = min) tez wybierana minimum
    held = stop_2n_held(
        [dict(r) for r in SELLS], [], dates, atr20, date(2026, 10, 13), LAST_DAY, D(1), True, True, stop_2n
    )
    eff_h, _src_h = stop_effective(stop_2n, stop_ch, True, held)
    assert held is not None and held <= stop_2n
    assert eff_h == min(stop_2n, stop_ch, held)
    close_d = closes[d_idx]
    assert close_d < eff
    risk, below = compute_risk_native(close_d, eff, abs(QTY), MULT, True)
    assert below is False
    assert risk == (eff - close_d) * 5 * 10
    assert risk > 0


def test_risk_native_short_above_stop_is_below_stop_zero_risk():
    risk_v, below = compute_risk_native(D("160"), D("155"), D(5), MULT, True)
    assert below is True
    assert risk_v == 0


def test_level1_short_future_summed_with_base_equity_one_name():
    base_id, fut_id = 7, 99
    nk_eq = emitent_key(base_id, "equity", None)
    nk_fut = emitent_key(fut_id, "future", base_id)
    assert nk_eq == nk_fut == base_id
    capital = D(100000)
    items = [
        ("BASE", "equity", False, D(1200), None),
        ("FPKNZ26", "future", False, D(800), None),
    ]
    res = aggregate_risk_budgets(items, capital, [nk_eq, nk_fut])
    # jedna nazwa: suma 2000 PLN / 100000 -> 2.0%, ten sam pct dla obu wierszy
    assert res.name_risk_pcts == [D(2), D(2)]
    assert res.satellite_risk_total == D(2000)
    names = level1_by_name(
        [
            (nk_eq, True, "BASE", "PLN", D("1.2"), res.name_risk_pcts[0], res.level1_results[0][1]),
            (nk_fut, True, "FPKNZ26", "PLN", D("0.8"), res.name_risk_pcts[1], res.level1_results[1][1]),
        ]
    )
    assert len(names) == 1
    assert names[0].name_key == base_id
    assert names[0].pct == D(2)
    assert [m[0] for m in names[0].members] == ["BASE", "FPKNZ26"]
    assert names[0].incomplete is False
    # kontrola: bez wspolnego klucza (osobne nazwy) to dwie nazwy
    res2 = aggregate_risk_budgets(items, capital, [nk_eq, fut_id])
    assert res2.name_risk_pcts == [D("1.2"), D("0.8")]


def test_module_has_no_changes_marker():
    # pomiar: uzywamy wylacznie publicznych funkcji modulu
    assert hasattr(risk, "run_risk")

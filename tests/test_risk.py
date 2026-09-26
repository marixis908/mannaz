"""Testy czystej logiki ryzyka i sizingu (§19 dokumentu projektowego) — brief
CC-P, P4.1/P4.2. WYŁĄCZNIE dane syntetyczne (bez prawdziwych numerów
rachunków/ilości), bez sieci/bazy."""

import math
import statistics
from datetime import date
from decimal import Decimal

import pytest

from mannaz.risk import (
    aggregate_risk_budgets,
    chandelier_hold,
    chandelier_series,
    check_kontraktowy_coverage,
    compute_risk_native,
    compute_theme_budgets,
    compute_weighted_entry_price,
    gbp_pence_factor,
    is_breached,
    is_regime,
    is_risk_budget_eligible,
    is_warning,
    kontraktowy_account_value,
    level1_check,
    level3_check,
    log_returns,
    population_stdev,
    ratchet_extreme,
    resolve_default_date,
    resolve_fx_rate,
    rolling_max,
    rolling_min,
    simple_moving_average,
    stop_effective,
    two_n_stop,
    wilder_atr_series,
)


# ---------------------------------------------------------------------------
# Wilder ATR
# ---------------------------------------------------------------------------


def test_wilder_atr_series_seed_and_one_smoothing_step():
    highs = [Decimal(12), Decimal(14), Decimal(10)]
    lows = [Decimal(8), Decimal(10), Decimal(6)]
    closes = [Decimal(10), Decimal(12), Decimal(8)]
    atr = wilder_atr_series(highs, lows, closes, period=2)
    # TR0=4, TR1=max(4,|14-10|=4,|10-10|=0)=4 -> seed atr[1]=(4+4)/2=4
    # TR2=max(4,|10-12|=2,|6-12|=6)=6 -> atr[2]=(4*1+6)/2=5
    assert atr == [None, Decimal(4), Decimal(5)]


def test_wilder_atr_series_insufficient_history_is_all_none():
    highs = [Decimal(12), Decimal(14)]
    lows = [Decimal(8), Decimal(10)]
    closes = [Decimal(10), Decimal(12)]
    assert wilder_atr_series(highs, lows, closes, period=5) == [None, None]


# ---------------------------------------------------------------------------
# rolling max/min/SMA
# ---------------------------------------------------------------------------


def test_rolling_max_and_min_require_full_window():
    values = [Decimal(1), Decimal(3), Decimal(2), Decimal(5), Decimal(4)]
    assert rolling_max(values, 3) == [None, None, Decimal(3), Decimal(5), Decimal(5)]
    assert rolling_min(values, 3) == [None, None, Decimal(1), Decimal(2), Decimal(2)]


def test_simple_moving_average():
    values = [Decimal(1), Decimal(2), Decimal(3), Decimal(4), Decimal(5), Decimal(6)]
    out = simple_moving_average(values, 3)
    assert out == [None, None, Decimal(2), Decimal(3), Decimal(4), Decimal(5)]


# ---------------------------------------------------------------------------
# GBp -> GBP
# ---------------------------------------------------------------------------


def test_gbp_pence_factor_applies_only_for_gbp_and_l_suffix():
    assert gbp_pence_factor("GBP", "FOO.L") == Decimal("0.01")
    assert gbp_pence_factor("GBP", "FOO") == Decimal(1)
    assert gbp_pence_factor("USD", "FOO.L") == Decimal(1)
    assert gbp_pence_factor(None, None) == Decimal(1)


# ---------------------------------------------------------------------------
# Chandelier (główny stop, zapadka) — long i short
# ---------------------------------------------------------------------------


def test_chandelier_series_long_and_short_with_small_window():
    highs = [Decimal(10), Decimal(12), Decimal(9), Decimal(14), Decimal(11)]
    lows = [Decimal(8), Decimal(9), Decimal(7), Decimal(10), Decimal(9)]
    atr22 = [None, None, Decimal(1), Decimal(2), Decimal(1)]

    long_series = chandelier_series(highs, lows, atr22, is_short=False, window=3)
    assert long_series == [None, None, Decimal(9), Decimal(8), Decimal(11)]

    short_series = chandelier_series(highs, lows, atr22, is_short=True, window=3)
    assert short_series == [None, None, Decimal(10), Decimal(13), Decimal(10)]


def test_ratchet_extreme_long_takes_max_short_takes_min_ignoring_none():
    long_values = [None, None, Decimal(9), Decimal(8), Decimal(11)]
    assert ratchet_extreme(long_values, 2, 4, is_short=False) == Decimal(11)
    assert ratchet_extreme(long_values, 2, 3, is_short=False) == Decimal(9)

    short_values = [None, None, Decimal(10), Decimal(13), Decimal(10)]
    assert ratchet_extreme(short_values, 2, 4, is_short=True) == Decimal(10)


def test_ratchet_extreme_all_none_returns_none():
    assert ratchet_extreme([None, None], 0, 1, is_short=False) is None


def test_two_n_stop_long_and_short():
    assert two_n_stop(Decimal(100), Decimal(5), is_short=False) == Decimal(90)
    assert two_n_stop(Decimal(100), Decimal(5), is_short=True) == Decimal(110)
    assert two_n_stop(None, Decimal(5), is_short=False) is None


def test_stop_effective_long_picks_higher_tighter_stop():
    assert stop_effective(Decimal(90), Decimal(95), is_short=False) == (Decimal(95), "chandelier")
    assert stop_effective(Decimal(95), Decimal(90), is_short=False) == (Decimal(95), "two_n")
    # remis -> two_n wygrywa (listowany pierwszy)
    assert stop_effective(Decimal(90), Decimal(90), is_short=False) == (Decimal(90), "two_n")


def test_stop_effective_short_picks_lower_tighter_stop():
    assert stop_effective(Decimal(110), Decimal(105), is_short=True) == (Decimal(105), "chandelier")
    assert stop_effective(Decimal(105), Decimal(110), is_short=True) == (Decimal(105), "two_n")


def test_stop_effective_one_side_missing_falls_back():
    assert stop_effective(None, Decimal(95), is_short=False) == (Decimal(95), "chandelier")
    assert stop_effective(Decimal(90), None, is_short=False) == (Decimal(90), "two_n")
    assert stop_effective(None, None, is_short=False) == (None, None)


# ---------------------------------------------------------------------------
# Wariant kontrolny B — chandelier_hold (do P4.4)
# ---------------------------------------------------------------------------


def test_chandelier_hold_long_and_short_use_whole_holding_window():
    highs = [Decimal(10), Decimal(12), Decimal(9), Decimal(14), Decimal(11)]
    lows = [Decimal(8), Decimal(9), Decimal(7), Decimal(10), Decimal(9)]
    assert chandelier_hold(highs, lows, first_entry_idx=1, d_idx=4, atr22_at_d=Decimal(2), is_short=False) == Decimal(8)
    assert chandelier_hold(highs, lows, first_entry_idx=1, d_idx=4, atr22_at_d=Decimal(2), is_short=True) == Decimal(13)


def test_chandelier_hold_missing_atr_returns_none():
    assert chandelier_hold([Decimal(1)], [Decimal(1)], 0, 0, None, is_short=False) is None


# ---------------------------------------------------------------------------
# is_breached — wspólna logika "po zlej stronie poziomu" (below_stop /
# below_chandelier_hold / below_chandelier_from_entry, poprawka P4.1)
# ---------------------------------------------------------------------------


def test_is_breached_long_and_short():
    assert is_breached(Decimal(95), Decimal(100), is_short=False) is True  # ponizej stopu (long)
    assert is_breached(Decimal(105), Decimal(100), is_short=False) is False
    assert is_breached(Decimal(105), Decimal(100), is_short=True) is True  # powyzej stopu (short)
    assert is_breached(Decimal(95), Decimal(100), is_short=True) is False


def test_is_breached_missing_inputs_returns_none():
    assert is_breached(None, Decimal(100), is_short=False) is None
    assert is_breached(Decimal(100), None, is_short=False) is None


# ---------------------------------------------------------------------------
# REGIME / OSTRZEŻENIE (§19.1)
# ---------------------------------------------------------------------------


def test_is_regime_true_only_when_both_conditions_hold():
    assert is_regime(Decimal(90), Decimal(100), Decimal(105)) is True
    assert is_regime(Decimal(90), Decimal(100), Decimal(95)) is False  # SMA200 rosnie
    assert is_regime(Decimal(110), Decimal(100), Decimal(105)) is False  # close nad SMA200
    assert is_regime(None, Decimal(100), Decimal(105)) is False


def test_is_warning_volatility_regime():
    assert is_warning(Decimal("0.05"), Decimal("0.02"), Decimal(100), Decimal(100)) is True


def test_is_warning_channel_condition():
    assert is_warning(None, None, Decimal(70), Decimal(100)) is True
    assert is_warning(None, None, Decimal(80), Decimal(100)) is False


def test_log_returns_matches_math_log():
    values = [Decimal(100), Decimal(110), Decimal(99)]
    out = log_returns(values)
    assert out[0] == Decimal(str(math.log(110 / 100)))
    assert out[1] == Decimal(str(math.log(99 / 110)))


def test_log_returns_none_on_non_positive_or_missing():
    assert log_returns([None, Decimal(10)]) == [None]
    assert log_returns([Decimal(0), Decimal(10)]) == [None]


def test_population_stdev_matches_statistics_pstdev():
    values = [Decimal(1), Decimal(2), Decimal(3)]
    expected = Decimal(str(statistics.pstdev([1.0, 2.0, 3.0])))
    assert population_stdev(values) == expected


def test_population_stdev_needs_at_least_two_values():
    assert population_stdev([Decimal(1)]) is None
    assert population_stdev([]) is None


# ---------------------------------------------------------------------------
# FX — kurs NBP A, ostatni dostępny ≤ D
# ---------------------------------------------------------------------------


def test_resolve_fx_rate_uses_last_available_on_or_before_target():
    rates = [(date(2024, 1, 1), Decimal("4.0")), (date(2024, 1, 3), Decimal("4.2"))]
    assert resolve_fx_rate(rates, "USD", date(2024, 1, 2)) == (Decimal("4.0"), date(2024, 1, 1))
    assert resolve_fx_rate(rates, "USD", date(2024, 1, 5)) == (Decimal("4.2"), date(2024, 1, 3))
    assert resolve_fx_rate(rates, "USD", date(2023, 12, 1)) == (None, None)


def test_resolve_fx_rate_pln_is_always_one():
    assert resolve_fx_rate([], "PLN", date(2024, 1, 2)) == (Decimal(1), date(2024, 1, 2))


# ---------------------------------------------------------------------------
# Ryzyko native + budżety §19.2
# ---------------------------------------------------------------------------


def test_compute_risk_native_long_healthy_and_below_stop():
    risk, below = compute_risk_native(Decimal(100), Decimal(90), Decimal(10), Decimal(1), is_short=False)
    assert risk == Decimal(100)
    assert below is False

    risk, below = compute_risk_native(Decimal(85), Decimal(90), Decimal(10), Decimal(1), is_short=False)
    assert risk == Decimal(0)
    assert below is True


def test_compute_risk_native_short_healthy_and_below_stop():
    risk, below = compute_risk_native(Decimal(100), Decimal(110), Decimal(5), Decimal(2), is_short=True)
    assert risk == Decimal(100)
    assert below is False

    risk, below = compute_risk_native(Decimal(115), Decimal(110), Decimal(5), Decimal(2), is_short=True)
    assert risk == Decimal(0)
    assert below is True


def test_compute_risk_native_missing_inputs_returns_none():
    assert compute_risk_native(None, Decimal(90), Decimal(10), Decimal(1), False) == (None, None)


def test_level1_check_breach_above_one_percent():
    pct, breach = level1_check(Decimal(2000), Decimal(100000))
    assert pct == Decimal(2)
    assert breach is True
    pct, breach = level1_check(Decimal(500), Decimal(100000))
    assert pct == Decimal("0.5")
    assert breach is False


def test_level1_check_missing_capital_returns_none():
    assert level1_check(Decimal(100), None) == (None, None)
    assert level1_check(Decimal(100), Decimal(0)) == (None, None)


def test_level3_check_breach_above_fifteen_percent():
    pct, breach = level3_check(Decimal(20000), Decimal(100000))
    assert pct == Decimal(20)
    assert breach is True


def test_compute_theme_budgets_sums_per_theme_and_flags_breach():
    rows = [("AAA", Decimal(1000)), ("BBB", Decimal(2000)), ("CCC", Decimal(500))]
    ticker_to_theme = {"AAA": "tech", "BBB": "tech", "CCC": "energy"}
    out = compute_theme_budgets(rows, ticker_to_theme, Decimal(100000))
    assert out["tech"].risk_pct == Decimal(3)
    assert out["tech"].breach is False  # dokladnie na progu, nie > 3%
    assert out["energy"].risk_pct == Decimal("0.5")
    assert out["energy"].breach is False


def test_compute_theme_budgets_skips_tickers_without_theme():
    rows = [("AAA", Decimal(1000)), ("ZZZ", Decimal(9999))]
    ticker_to_theme = {"AAA": "tech"}
    out = compute_theme_budgets(rows, ticker_to_theme, Decimal(100000))
    assert set(out.keys()) == {"tech"}


# ---------------------------------------------------------------------------
# Cena wejścia ważona pozostałymi lotami FIFO
# ---------------------------------------------------------------------------


def test_weighted_entry_price_simple_partial_sell_keeps_original_lot_price():
    rows = [
        {"date": date(2024, 1, 1), "type": "kupno", "qty": Decimal(10), "price": Decimal(10)},
        {"date": date(2024, 2, 1), "type": "sprzedaz", "qty": Decimal(4), "price": Decimal(12)},
    ]
    result = compute_weighted_entry_price(rows, events=[], allow_short=False)
    assert result.qty == Decimal(6)
    assert result.entry_price == Decimal(10)
    assert result.first_remaining_date == date(2024, 1, 1)
    assert result.last_remaining_date == date(2024, 1, 1)


def test_weighted_entry_price_fifo_order_consumes_oldest_lot_first():
    rows = [
        {"date": date(2024, 1, 1), "type": "kupno", "qty": Decimal(5), "price": Decimal(10)},
        {"date": date(2024, 1, 10), "type": "kupno", "qty": Decimal(5), "price": Decimal(20)},
        {"date": date(2024, 2, 1), "type": "sprzedaz", "qty": Decimal(7), "price": Decimal(25)},
    ]
    result = compute_weighted_entry_price(rows, events=[], allow_short=False)
    assert result.qty == Decimal(3)
    assert result.entry_price == Decimal(20)  # tylko drugi lot pozostal
    assert result.first_remaining_date == date(2024, 1, 10)
    assert result.last_remaining_date == date(2024, 1, 10)


def test_weighted_entry_price_split_scales_remaining_lot_price_and_qty():
    rows = [
        {"date": date(2024, 1, 1), "type": "kupno", "qty": Decimal(10), "price": Decimal(100)},
    ]
    events = [{"date": date(2024, 6, 1), "ratio": Decimal(25)}]
    result = compute_weighted_entry_price(rows, events, allow_short=False)
    assert result.qty == Decimal(250)
    assert result.entry_price == Decimal(4)  # 100 / 25
    assert result.entry_price * result.qty == Decimal(1000)  # koszt calkowity niezmieniony


def test_weighted_entry_price_oversell_without_allow_short_has_zero_cost():
    rows = [{"date": date(2024, 1, 1), "type": "sprzedaz", "qty": Decimal(3), "price": Decimal(10)}]
    result = compute_weighted_entry_price(rows, events=[], allow_short=False)
    assert result.qty == Decimal(-3)
    assert result.entry_price == Decimal(0)


def test_weighted_entry_price_short_open_with_allow_short_gets_real_price():
    rows = [{"date": date(2024, 1, 1), "type": "sprzedaz", "qty": Decimal(2), "price": Decimal(100)}]
    result = compute_weighted_entry_price(rows, events=[], allow_short=True)
    assert result.qty == Decimal(-2)
    assert result.entry_price == Decimal(100)


def test_weighted_entry_price_short_partially_covered_nets_correctly():
    rows = [
        {"date": date(2024, 1, 1), "type": "sprzedaz", "qty": Decimal(5), "price": Decimal(100)},
        {"date": date(2024, 2, 1), "type": "kupno", "qty": Decimal(3), "price": Decimal(80)},
    ]
    result = compute_weighted_entry_price(rows, events=[], allow_short=True)
    assert result.qty == Decimal(-2)
    assert result.entry_price == Decimal(100)  # pozostaly lot ma cene pierwotnej sprzedazy
    assert result.first_remaining_date == date(2024, 1, 1)
    assert result.last_remaining_date == date(2024, 1, 1)


def test_weighted_entry_price_no_rows_returns_none_entry():
    result = compute_weighted_entry_price([], events=[], allow_short=False)
    assert result.entry_price is None
    assert result.qty == Decimal(0)
    assert result.first_remaining_date is None


# ---------------------------------------------------------------------------
# Domyślne D (brief P4.1 [Z])
# ---------------------------------------------------------------------------


def test_resolve_default_date_picks_latest_date_without_null_close():
    flags = [
        (date(2026, 9, 23), False),
        (date(2026, 9, 24), False),
        (date(2026, 9, 25), True),
    ]
    assert resolve_default_date(flags) == date(2026, 9, 24)


def test_resolve_default_date_all_null_returns_none():
    assert resolve_default_date([(date(2026, 9, 25), True)]) is None


# ---------------------------------------------------------------------------
# is_risk_budget_eligible (§19.4, brief CC-R) — equity/etf spoza core LUB future
# ---------------------------------------------------------------------------


def test_is_risk_budget_eligible_equity_etf_non_core_and_future_only():
    assert is_risk_budget_eligible("equity", is_core=False) is True
    assert is_risk_budget_eligible("etf", is_core=False) is True
    assert is_risk_budget_eligible("equity", is_core=True) is False  # core -> nie
    assert is_risk_budget_eligible("future", is_core=False) is True
    assert is_risk_budget_eligible("certificate", is_core=False) is False


# ---------------------------------------------------------------------------
# aggregate_risk_budgets (brief CC-R (e)) — kontrakt w heat/temacie/poziomie 1,
# pozycja core wykluczona z budżetów
# ---------------------------------------------------------------------------


def test_aggregate_risk_budgets_future_enters_heat_theme_and_level1_core_excluded():
    items = [
        ("AAA", "equity", False, Decimal(500), "tech"),
        ("FUT1", "future", False, Decimal(1500), "tech"),
        ("CORE1", "equity", True, Decimal(9999), "tech"),  # core -> wykluczony z budzetow
    ]
    result = aggregate_risk_budgets(items, Decimal(100000))

    # poziom 3 (heat) — kontrakt wchodzi ryzykiem, pozycja core pominieta
    assert result.satellite_risk_total == Decimal(2000)
    assert result.total_risk_pct == Decimal(2)
    assert result.level3_breach is False

    # poziom 2 (temat) — kontrakt wchodzi do sumy tematu
    assert result.theme_budgets["tech"].risk_pct == Decimal(2)
    assert result.theme_budgets["tech"].breach is False

    # poziom 1 — kontrakt liczony, core -> (None, None)
    assert result.level1_results[0] == (Decimal("0.5"), False)
    assert result.level1_results[1] == (Decimal("1.5"), True)
    assert result.level1_results[2] == (None, None)
    assert result.level1_breach_tickers == ["FUT1"]


def test_aggregate_risk_budgets_short_future_mirrored_risk_enters_heat():
    """Kontrakt krotki: stop nad cena (lustrzane ryzyko wzgledem long) —
    compute_risk_native -> risk_pln -> wchodzi do heat na tych samych zasadach
    co long."""
    risk_native, below_stop = compute_risk_native(
        close_d=Decimal(100), stop_eff=Decimal(110), qty_abs=Decimal(3), multiplier=Decimal(10), is_short=True
    )
    assert risk_native == Decimal(300)  # (110-100)*3*10
    assert below_stop is False

    items = [("FSHORT", "future", False, risk_native, "energy")]
    result = aggregate_risk_budgets(items, Decimal(100000))
    assert result.satellite_risk_total == Decimal(300)
    assert result.theme_budgets["energy"].risk_pct == Decimal("0.3")


def test_aggregate_risk_budgets_multiplier_missing_risk_none_is_skipped():
    items = [("FMISS", "future", False, None, "tech")]
    result = aggregate_risk_budgets(items, Decimal(100000))
    assert result.satellite_risk_total == Decimal(0)
    assert result.level1_results == [(None, None)]
    assert "tech" not in result.theme_budgets


# ---------------------------------------------------------------------------
# kontraktowy_account_value (brief CC-R (a)/(g), §19.4 + decyzja nadzorcy K3)
# — srodki ogolem lacznie z depozytem zablokowanym; nominal kupna/sprzedazy
# NIE jest przeplywem srodkow, liczy sie wylacznie prowizja jako koszt.
# ---------------------------------------------------------------------------


def _kontraktowy_row(
    transaction_date, row_type, amount, qty=None, price=None, multiplier=None, currency="PLN", broker_ticker="FSYN1"
):
    return {
        "transaction_date": transaction_date,
        "currency": currency,
        "row_type": row_type,
        "amount": Decimal(amount),
        "qty": Decimal(qty) if qty is not None else None,
        "price": Decimal(price) if price is not None else None,
        "multiplier": Decimal(multiplier) if multiplier is not None else None,
        "broker_ticker": broker_ticker,
    }


def test_kontraktowy_account_value_kupno_sprzedaz_only_commission_deposits_and_transfers_passthrough():
    rows = [
        # kupno: nominal 3*100*10=3000, prowizja 15 -> amount = -(3000+15)
        _kontraktowy_row(date(2026, 1, 10), "kupno", "-3015", qty=3, price=100, multiplier=10),
        # sprzedaz: nominal 2*50*10=1000, prowizja 20 -> amount = +(1000+20)
        _kontraktowy_row(date(2026, 1, 15), "sprzedaz", "1020", qty=2, price=50, multiplier=10),
        _kontraktowy_row(date(2026, 1, 20), "depozyt_doplata", "500"),
        _kontraktowy_row(date(2026, 1, 25), "przelew_wewnetrzny", "-200"),
    ]
    # nominal (3000, 1000) NIE wchodzi -> tylko -15 (prowizja kupno) - 20 (prowizja sprzedaz) + 500 - 200
    assert kontraktowy_account_value(rows, as_of=date(2026, 1, 25)) == Decimal(265)


def test_kontraktowy_account_value_filters_by_as_of():
    rows = [
        _kontraktowy_row(date(2026, 1, 10), "kupno", "-3015", qty=3, price=100, multiplier=10),
        _kontraktowy_row(date(2026, 1, 15), "sprzedaz", "1020", qty=2, price=50, multiplier=10),
        _kontraktowy_row(date(2026, 1, 20), "depozyt_doplata", "500"),
        _kontraktowy_row(date(2026, 1, 25), "przelew_wewnetrzny", "-200"),  # po as_of, pominiety
    ]
    assert kontraktowy_account_value(rows, as_of=date(2026, 1, 20)) == Decimal(465)


def test_kontraktowy_account_value_missing_multiplier_raises_value_error_with_series_name():
    # K4b: brak instruments.multiplier -> wyraźny błąd z nazwą serii, bez zgadywania
    rows = [_kontraktowy_row(date(2026, 2, 1), "sprzedaz", "10025", qty=1, price=100, multiplier=None, broker_ticker="FEXP1")]
    with pytest.raises(ValueError, match="FEXP1"):
        kontraktowy_account_value(rows, as_of=date(2026, 2, 1))


def test_kontraktowy_account_value_ignores_manual_non_cash_rows():
    # K4c: wiersze ręczne niosą koszt dla FIFO, nie przepływ gotówki -> saldo bez zmian
    base = [
        _kontraktowy_row(date(2026, 1, 10), "kupno", "-3015", qty=3, price=100, multiplier=10),
        _kontraktowy_row(date(2026, 1, 20), "depozyt_zwrot", "500"),
    ]
    manual = [
        _kontraktowy_row(date(2026, 1, 5), "bilans_otwarcia", "-1000", qty=1, price=1000),
        _kontraktowy_row(date(2026, 1, 6), "zamiana_wydanie", "700", qty=1, price=700),
        _kontraktowy_row(date(2026, 1, 6), "zamiana_przyjecie", "-700", qty=1, price=700),
    ]
    assert kontraktowy_account_value(base + manual, as_of=date(2026, 1, 20)) == kontraktowy_account_value(
        base, as_of=date(2026, 1, 20)
    ) == Decimal(485)


def test_kontraktowy_account_value_other_currency_raises_value_error():
    rows = [_kontraktowy_row(date(2026, 1, 10), "kupno", "-3015", qty=3, price=100, multiplier=10, currency="USD")]
    with pytest.raises(ValueError, match="USD"):
        kontraktowy_account_value(rows, as_of=date(2026, 1, 10))


# ---------------------------------------------------------------------------
# check_kontraktowy_coverage (brief CC-R (b)) — nigdy cichego zera
# ---------------------------------------------------------------------------


def test_check_kontraktowy_coverage_no_rows_raises_runtime_error():
    with pytest.raises(RuntimeError):
        check_kontraktowy_coverage(n_rows=0, max_date=None, has_open_futures=True, as_of=date(2026, 9, 25))
    with pytest.raises(RuntimeError):
        check_kontraktowy_coverage(n_rows=0, max_date=None, has_open_futures=False, as_of=date(2026, 9, 25))


def test_check_kontraktowy_coverage_stale_history_with_open_futures_raises_runtime_error():
    with pytest.raises(RuntimeError):
        check_kontraktowy_coverage(n_rows=5, max_date=date(2026, 9, 20), has_open_futures=True, as_of=date(2026, 9, 25))


def test_check_kontraktowy_coverage_ok_cases_do_not_raise():
    # historia siega D
    check_kontraktowy_coverage(n_rows=5, max_date=date(2026, 9, 25), has_open_futures=True, as_of=date(2026, 9, 25))
    # historia sprzed D, ale brak otwartych kontraktow -> nie ma czego rozliczac codziennie
    check_kontraktowy_coverage(n_rows=5, max_date=date(2026, 9, 20), has_open_futures=False, as_of=date(2026, 9, 25))

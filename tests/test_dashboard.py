"""Testy czystych funkcji dashboardu satelity (brief CC-D, krok D4) —
`scripts/dashboard/model.py`. WYŁĄCZNIE dane syntetyczne (tickery oczywiście
zmyślone: "TST1"/"TST2"/..., liczby okrągłe), bez sieci/bazy. Kontrakt:
tmp/dashboard/kontrakt-danych.md.

`scripts/dashboard/` nie jest pakietem pod `src/` (jak `mannaz`), więc
importujemy `model` po dopisaniu jego katalogu do `sys.path` — WYŁĄCZNIE w
tym pliku (nie modyfikujemy `tests/conftest.py`, poza zakresem tego zadania:
tworzymy tylko pliki pod `scripts/dashboard/` i ten jeden plik testowy)."""

import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

_DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "scripts" / "dashboard"
if str(_DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(_DASHBOARD_DIR))

import model  # noqa: E402

from mannaz.risk import is_risk_budget_eligible  # noqa: E402 — ta sama definicja co generate.py


# ---------------------------------------------------------------------------
# Formatowanie PL
# ---------------------------------------------------------------------------


def test_format_pl_number_groups_four_digit_numbers():
    # Kontrakt: Intl pl-PL POMIJA grupowanie liczb 4-cyfrowych — to jest
    # dokładnie przypadek, który ręczna implementacja MUSI obsłużyć.
    assert model.format_pl_number(Decimal(1234)) == "1 234"


def test_format_pl_number_groups_large_numbers():
    assert model.format_pl_number(Decimal(1234567)) == "1 234 567"


def test_format_pl_number_with_decimals_uses_comma():
    assert model.format_pl_number(Decimal("1234567.891"), 2) == "1 234 567,89"


def test_format_pl_number_negative_sign_before_grouping():
    assert model.format_pl_number(Decimal("-2500"), 0) == "-2 500"


def test_format_pl_number_small_number_no_grouping():
    assert model.format_pl_number(Decimal(42), 2) == "42,00"


# ---------------------------------------------------------------------------
# Rynek z sufiksu yahoo_symbol
# ---------------------------------------------------------------------------


def test_market_from_yahoo_symbol_gpw():
    assert model.market_from_yahoo_symbol("TST1.WA") == "GPW"


def test_market_from_yahoo_symbol_canada():
    assert model.market_from_yahoo_symbol("TST1.TO") == "Kanada"
    assert model.market_from_yahoo_symbol("TST1.V") == "Kanada"
    assert model.market_from_yahoo_symbol("TST1.NE") == "Kanada"


def test_market_from_yahoo_symbol_europe():
    assert model.market_from_yahoo_symbol("TST1.DE") == "Europa"
    assert model.market_from_yahoo_symbol("TST1.L") == "Europa"


def test_market_from_yahoo_symbol_usa_no_suffix():
    assert model.market_from_yahoo_symbol("TST1") == "USA"


def test_market_from_yahoo_symbol_none_is_nieustalony():
    assert model.market_from_yahoo_symbol(None) == "nieustalony"
    assert model.market_from_yahoo_symbol("") == "nieustalony"


def test_market_from_yahoo_symbol_unknown_suffix_is_nieustalony():
    assert model.market_from_yahoo_symbol("TST1.XX") == "nieustalony"


# ---------------------------------------------------------------------------
# Klasyfikacja SAT/CORE/FUT/INNE + etykieta rachunku
# ---------------------------------------------------------------------------


def test_classify_position_sat():
    assert model.classify_position("ZAGRANICZNY", "equity", False) == "SAT"
    assert model.classify_position("AKCYJNY", "etf", False) == "SAT"


def test_classify_position_core():
    assert model.classify_position("ZAGRANICZNY", "etf", True) == "CORE"


def test_classify_position_fut():
    assert model.classify_position("KONTRAKTOWY", "future", False) == "FUT"


def test_classify_position_inne():
    assert model.classify_position("AKCYJNY", "certificate", False) == "INNE"
    assert model.classify_position("KONTRAKTOWY", "equity", False) == "INNE"


def test_account_label_strips_account_number():
    assert model.account_label("AKCYJNY 000123") == "AKCYJNY"
    assert model.account_label("ZAGRANICZNY 999") == "ZAGRANICZNY"


# ---------------------------------------------------------------------------
# Waluta nieobsłużona / FX (STOP kontrakt §3)
# ---------------------------------------------------------------------------


def test_resolve_fx_factor_pln_is_one():
    assert model.resolve_fx_factor("PLN", None, True) == Decimal(1)


def test_resolve_fx_factor_supported_currency_with_rate():
    assert model.resolve_fx_factor("USD", Decimal("4.0"), True) == Decimal("4.0")


def test_resolve_fx_factor_supported_currency_missing_rate_on_d_returns_none():
    # Waluta obsługiwana (ma pary w fx_nbp), ale brak konkretnego kursu <= D
    # -> None, BEZ STOP (kontrakt §3, wiersz "FX", "gdy brak").
    assert model.resolve_fx_factor("USD", None, True) is None


def test_resolve_fx_factor_pence_currency_raises():
    for pence in ("GBp", "GBX"):
        try:
            model.resolve_fx_factor(pence, Decimal(1), False)
        except model.UnhandledCurrencyError:
            pass
        else:
            raise AssertionError(f"oczekiwano UnhandledCurrencyError dla {pence}")


def test_resolve_fx_factor_unsupported_currency_raises():
    try:
        model.resolve_fx_factor("XYZ", None, False)
    except model.UnhandledCurrencyError:
        pass
    else:
        raise AssertionError("oczekiwano UnhandledCurrencyError dla waluty bez pary w fx_nbp")


# ---------------------------------------------------------------------------
# Konsolidacja
# ---------------------------------------------------------------------------


def _syn_row(**kwargs):
    base = {
        "isin": "TSTISIN1",
        "broker_ticker": "TST1",
        "market": "USA",
        "settlement_currency": "USD",
        "value_pln": Decimal("1000"),
        "residual_cost": Decimal("800"),
        "rachunek": "ZAGRANICZNY",
        "instrument_id": 1,
    }
    base.update(kwargs)
    return base


def test_consolidate_positions_merges_same_isin_same_currency():
    rows = [_syn_row(value_pln=Decimal("1000"), residual_cost=Decimal("800")),
            _syn_row(value_pln=Decimal("500"), residual_cost=Decimal("300"))]
    result = model.consolidate_positions(rows)
    assert result.count_before == 2
    assert result.count_after == 1
    assert result.merge_count == 1
    assert result.ambiguous_count == 0
    assert result.mixed_currency_merge_count == 0
    merged = result.positions[0]
    assert merged["consolidated"] is True
    assert merged["value_pln"] == Decimal("1500")
    assert merged["residual_cost"] == Decimal("1100")


def test_consolidate_positions_mixed_currency_no_result_sum():
    rows = [
        _syn_row(value_pln=Decimal("1000"), residual_cost=Decimal("800"), settlement_currency="USD"),
        _syn_row(value_pln=Decimal("500"), residual_cost=Decimal("300"), settlement_currency="EUR"),
    ]
    result = model.consolidate_positions(rows)
    assert result.count_after == 1
    assert result.mixed_currency_merge_count == 1
    merged = result.positions[0]
    assert merged["mixed_currency"] is True
    assert merged["value_pln"] == Decimal("1500")  # wartość PLN scalana ZAWSZE
    assert merged["residual_cost"] is None  # wynik: bez sumy przy różnych walutach


def test_consolidate_positions_ambiguous_same_isin_different_market_not_merged():
    rows = [
        _syn_row(market="USA"),
        _syn_row(market="GPW"),
    ]
    result = model.consolidate_positions(rows)
    assert result.count_before == 2
    assert result.count_after == 2  # NIE scalone
    assert result.merge_count == 0
    assert result.ambiguous_count == 1
    assert all(p["ambiguous"] for p in result.positions)


def test_consolidate_positions_without_isin_keys_by_ticker_and_market():
    rows = [
        _syn_row(isin=None, broker_ticker="TST9", market="USA", value_pln=Decimal("100"), residual_cost=Decimal("90")),
        _syn_row(isin=None, broker_ticker="TST9", market="USA", value_pln=Decimal("200"), residual_cost=Decimal("110")),
    ]
    result = model.consolidate_positions(rows)
    assert result.count_after == 1
    assert result.positions[0]["value_pln"] == Decimal("300")


def test_consolidate_positions_no_merge_when_single_row_per_key():
    rows = [_syn_row(isin="A"), _syn_row(isin="B", broker_ticker="TST2")]
    result = model.consolidate_positions(rows)
    assert result.count_before == result.count_after == 2
    assert result.merge_count == 0


# ---------------------------------------------------------------------------
# Wagi
# ---------------------------------------------------------------------------


def test_compute_weights_sum_to_one():
    rows = [
        {"value_pln": Decimal("100")},
        {"value_pln": Decimal("300")},
        {"value_pln": Decimal("600")},
    ]
    bw = sum((r["value_pln"] for r in rows), Decimal(0))
    weights = model.compute_weights(rows, bw)
    assert sum(weights, Decimal(0)) == Decimal(1)
    assert weights[0] == Decimal("100") / bw


def test_compute_weights_none_value_gives_none_weight():
    rows = [{"value_pln": Decimal("100")}, {"value_pln": None}]
    weights = model.compute_weights(rows, Decimal("100"))
    assert weights[0] == Decimal(1)
    assert weights[1] is None


def test_compute_weights_zero_bw_gives_all_none():
    rows = [{"value_pln": Decimal("100")}]
    assert model.compute_weights(rows, Decimal(0)) == [None]
    assert model.compute_weights(rows, None) == [None]


# ---------------------------------------------------------------------------
# Wynik (a)
# ---------------------------------------------------------------------------


def test_compute_result_a_same_currency():
    res = model.compute_result_a(
        qty=Decimal(10), close_raw=Decimal("120"), residual_cost=Decimal("1000"),
        quote_currency="USD", settlement_currency="USD", fx_quote=Decimal("4"), fx_settlement=Decimal("4"),
    )
    # 10*120 - 1000 = 200; %=200/1000*100=20
    assert res.amount == Decimal("200")
    assert res.pct == Decimal("20")
    assert res.cross_rate_flag is False
    assert res.brak_opis is None


def test_compute_result_a_cross_currency_sets_flag():
    # notowanie USD, rozliczenie EUR — kurs krzyżowy FX(USD)/FX(EUR) przez PLN
    res = model.compute_result_a(
        qty=Decimal(10), close_raw=Decimal("100"), residual_cost=Decimal("500"),
        quote_currency="USD", settlement_currency="EUR", fx_quote=Decimal("4"), fx_settlement=Decimal("5"),
    )
    # wartosc_w_rozliczeniu = 10*100*(4/5) = 800; wynik = 800-500=300; %=60
    assert res.cross_rate_flag is True
    assert res.amount == Decimal("300")
    assert res.pct == Decimal("60")


def test_compute_result_a_missing_price_is_brak():
    res = model.compute_result_a(
        qty=Decimal(10), close_raw=None, residual_cost=Decimal("500"),
        quote_currency="USD", settlement_currency="USD", fx_quote=Decimal(4), fx_settlement=Decimal(4),
    )
    assert res.amount is None
    assert res.brak_opis is not None


def test_compute_result_a_missing_residual_cost_is_brak():
    res = model.compute_result_a(
        qty=Decimal(10), close_raw=Decimal(100), residual_cost=None,
        quote_currency="USD", settlement_currency="USD", fx_quote=Decimal(4), fx_settlement=Decimal(4),
    )
    assert res.amount is None
    assert res.brak_opis is not None


def test_compute_result_a_nonpositive_residual_cost_is_brak():
    res = model.compute_result_a(
        qty=Decimal(10), close_raw=Decimal(100), residual_cost=Decimal(0),
        quote_currency="USD", settlement_currency="USD", fx_quote=Decimal(4), fx_settlement=Decimal(4),
    )
    assert res.amount is None


def test_compute_result_a_missing_cross_rate_is_brak():
    res = model.compute_result_a(
        qty=Decimal(10), close_raw=Decimal(100), residual_cost=Decimal(500),
        quote_currency="USD", settlement_currency="EUR", fx_quote=None, fx_settlement=Decimal(5),
    )
    assert res.amount is None
    assert res.brak_opis is not None


# ---------------------------------------------------------------------------
# Testy pochodzenia ryzyka (kontrakt §5)
# ---------------------------------------------------------------------------


def test_evaluate_provenance_all_pass():
    result = model.evaluate_provenance(True, True, True)
    assert result.passed is True
    assert result.failed_tests == []


def test_evaluate_provenance_test1_fail():
    result = model.evaluate_provenance(False, True, True)
    assert result.passed is False
    assert len(result.failed_tests) == 1
    assert "test 1" in result.failed_tests[0]


def test_evaluate_provenance_multiple_fail():
    result = model.evaluate_provenance(False, False, True)
    assert result.passed is False
    assert len(result.failed_tests) == 2


def test_evaluate_test1_single_run():
    assert model.evaluate_test1_single_run(1) is True
    assert model.evaluate_test1_single_run(2) is False
    assert model.evaluate_test1_single_run(0) is False


def test_evaluate_test2_position_set_match():
    keys = {("AKCYJNY 1", 1, "PLN")}
    assert model.evaluate_test2_position_set_match(keys, keys) is True
    assert model.evaluate_test2_position_set_match(keys, set()) is False


def test_evaluate_test3_empty_list_passes():
    assert model.evaluate_test3_closed_positions_provenance([]) is True


def test_evaluate_test3_missing_risk_daily_row_fails():
    items = [{"has_risk_daily_row": False, "computed_at": None, "max_sell_created_at": None}]
    assert model.evaluate_test3_closed_positions_provenance(items) is False


def test_evaluate_test3_computed_at_before_sell_fails():
    items = [{
        "has_risk_daily_row": True,
        "computed_at": datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
        "max_sell_created_at": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
    }]
    assert model.evaluate_test3_closed_positions_provenance(items) is False


def test_evaluate_test3_computed_at_after_sell_passes():
    items = [{
        "has_risk_daily_row": True,
        "computed_at": datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc),
        "max_sell_created_at": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
    }]
    assert model.evaluate_test3_closed_positions_provenance(items) is True


# ---------------------------------------------------------------------------
# Agregacja ryzyka (kontrakt §5) — `is_risk_budget_eligible` z mannaz.risk,
# ta sama definicja co w generate.py.
# ---------------------------------------------------------------------------


def _risk_row(**kwargs):
    base = {
        "instrument_type": "equity",
        "is_core": False,
        "multiplier_missing": False,
        "risk_pct_satellite_capital": Decimal("1.0"),
        "level1_breach": False,
        "close_d": Decimal("100"),
        "theme": "tech",
    }
    base.update(kwargs)
    return base


def test_sum_open_risk_pct_excludes_core_and_multiplier_missing():
    rows = [
        _risk_row(risk_pct_satellite_capital=Decimal("1.0")),
        _risk_row(is_core=True, risk_pct_satellite_capital=Decimal("5.0")),  # core -> wykluczone
        _risk_row(instrument_type="future", multiplier_missing=True, risk_pct_satellite_capital=Decimal("2.0")),  # wykluczone
        _risk_row(instrument_type="future", risk_pct_satellite_capital=Decimal("0.5")),
    ]
    total = model.sum_open_risk_pct(rows, is_risk_budget_eligible)
    assert total == Decimal("1.5")


def test_count_level1_breaches():
    rows = [_risk_row(level1_breach=True), _risk_row(level1_breach=False), _risk_row(level1_breach=True)]
    assert model.count_level1_breaches(rows) == 2


def test_price_coverage_counts_eligible_rows_with_price():
    rows = [
        _risk_row(close_d=Decimal("1")),
        _risk_row(close_d=None),
        _risk_row(is_core=True, close_d=Decimal("1")),  # nie liczone do N (nieeligible)
    ]
    n, big_n = model.price_coverage(rows, is_risk_budget_eligible)
    assert (n, big_n) == (1, 2)


def test_group_risk_pct_by_theme_groups_none_as_bez_tematu():
    rows = [
        _risk_row(theme="tech", risk_pct_satellite_capital=Decimal("1.0")),
        _risk_row(theme="tech", risk_pct_satellite_capital=Decimal("0.5")),
        _risk_row(theme=None, risk_pct_satellite_capital=Decimal("2.0")),
    ]
    grouped = model.group_risk_pct_by_theme(rows, is_risk_budget_eligible)
    assert grouped["tech"] == Decimal("1.5")
    assert grouped["bez tematu"] == Decimal("2.0")


# ---------------------------------------------------------------------------
# Kontrakty terminowe — nominał
# ---------------------------------------------------------------------------


def test_compute_futures_nominal_pln_basic():
    nominal = model.compute_futures_nominal_pln(Decimal(2), Decimal(100), Decimal("50"), Decimal("1"))
    assert nominal == Decimal("10000")


def test_compute_futures_nominal_pln_negative_qty_keeps_sign():
    nominal = model.compute_futures_nominal_pln(Decimal(-2), Decimal(100), Decimal("50"), Decimal("1"))
    assert nominal == Decimal("-10000")


def test_compute_futures_nominal_pln_missing_multiplier_is_none():
    assert model.compute_futures_nominal_pln(Decimal(2), None, Decimal("50"), Decimal("1")) is None


def test_compute_futures_nominal_pln_missing_base_price_is_none():
    assert model.compute_futures_nominal_pln(Decimal(2), Decimal(100), None, Decimal("1")) is None


# ---------------------------------------------------------------------------
# Sesje / świeżość
# ---------------------------------------------------------------------------


def test_count_weekday_sessions_between_typical_week():
    # piątek -> poniedziałek: sobota+niedziela pomijane, poniedziałek liczony
    assert model.count_weekday_sessions_between(date(2026, 9, 25), date(2026, 9, 28)) == 1


def test_count_weekday_sessions_between_weekend_only_is_zero():
    assert model.count_weekday_sessions_between(date(2026, 9, 25), date(2026, 9, 27)) == 0


def test_count_weekday_sessions_between_same_or_earlier_is_zero():
    d = date(2026, 9, 25)
    assert model.count_weekday_sessions_between(d, d) == 0
    assert model.count_weekday_sessions_between(d, date(2026, 9, 20)) == 0


def test_count_weekday_sessions_between_full_business_week():
    # poniedziałek -> poniedziałek kolejnego tygodnia = 5 sesji (wt-pt + pon)
    assert model.count_weekday_sessions_between(date(2026, 9, 21), date(2026, 9, 28)) == 5


# ---------------------------------------------------------------------------
# Sanity: dataclassy serializują się do dict (jak w generate.py::asdict)
# ---------------------------------------------------------------------------


def test_result_a_and_provenance_are_asdict_friendly():
    res = model.compute_result_a(
        qty=Decimal(1), close_raw=Decimal(1), residual_cost=Decimal(1),
        quote_currency="PLN", settlement_currency="PLN", fx_quote=Decimal(1), fx_settlement=Decimal(1),
    )
    d = asdict(res)
    assert set(d.keys()) == {"amount", "pct", "cross_rate_flag", "brak_opis"}

    prov = model.evaluate_provenance(True, True, True)
    d2 = asdict(prov)
    assert set(d2.keys()) == {"passed", "failed_tests"}

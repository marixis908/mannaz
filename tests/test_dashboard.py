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
        "price_status": "ok",  # T27 (kontrakt §3/§4) — domyślnie kompletna
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
# Status ceny wg T27 (kontrakt §3/§4): pozycja scalona = najgorszy ze
# składowych ("incomplete" > "stale" > "ok").
# ---------------------------------------------------------------------------


def test_worst_price_status_incomplete_wins_over_everything():
    assert model.worst_price_status(["ok", "stale", "incomplete"]) == "incomplete"
    assert model.worst_price_status(["ok", "incomplete"]) == "incomplete"


def test_worst_price_status_stale_wins_over_ok():
    assert model.worst_price_status(["ok", "stale"]) == "stale"


def test_worst_price_status_all_ok_is_ok():
    assert model.worst_price_status(["ok", "ok"]) == "ok"


def test_consolidate_positions_merged_status_is_worst_of_components_incomplete():
    rows = [
        _syn_row(price_status="ok", value_pln=Decimal("1000")),
        _syn_row(price_status="incomplete", value_pln=None),
    ]
    merged = model.consolidate_positions(rows).positions[0]
    assert merged["consolidated"] is True
    assert merged["price_status"] == "incomplete"
    # T27 (kontrakt §3): pozycja incomplete NIE ma cicho zsumowanej wartości —
    # suma jest None, gdy choć jeden komponent nie ma value_pln.
    assert merged["value_pln"] is None


def test_consolidate_positions_merged_status_is_worst_of_components_stale():
    rows = [
        _syn_row(price_status="ok", value_pln=Decimal("1000")),
        _syn_row(price_status="stale", value_pln=Decimal("500")),
    ]
    merged = model.consolidate_positions(rows).positions[0]
    assert merged["price_status"] == "stale"
    assert merged["value_pln"] == Decimal("1500")


# ---------------------------------------------------------------------------
# Liczniki n/N i sumy "wszystko-albo-nic" (kontrakt §2/§3/§7): BW/CORE/
# nominał FUT — brak wartości jednej pozycji zbioru -> suma = brak pomiaru
# (None), NIGDY suma częściowa; pozycja NIGDY nie wypada z mianownika N.
# ---------------------------------------------------------------------------


def test_count_complete_counts_priced_rows():
    rows = [{"value_pln": Decimal("100")}, {"value_pln": None}, {"value_pln": Decimal("50")}]
    assert model.count_complete(rows, "value_pln") == (2, 3)


def test_compute_sum_if_complete_returns_none_when_any_incomplete():
    rows = [{"value_pln": Decimal("100")}, {"value_pln": None}]
    assert model.compute_sum_if_complete(rows, "value_pln") is None


def test_compute_sum_if_complete_sums_when_all_complete():
    rows = [{"value_pln": Decimal("100")}, {"value_pln": Decimal("50")}]
    assert model.compute_sum_if_complete(rows, "value_pln") == Decimal("150")


def test_compute_sum_if_complete_empty_set_is_zero():
    assert model.compute_sum_if_complete([], "value_pln") == Decimal(0)


def test_compute_sum_if_complete_generic_field_name_for_fut_nominal():
    rows = [{"nominal_pln": Decimal("1000")}, {"nominal_pln": None}]
    assert model.compute_sum_if_complete(rows, "nominal_pln") is None
    assert model.count_complete(rows, "nominal_pln") == (1, 2)


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


def test_evaluate_provenance_all_pass_test3_pass():
    result = model.evaluate_provenance(True, True, "pass")
    assert result.overall_status == "PASS"
    assert result.passed is True
    assert result.failed_tests == []
    assert result.note is None


def test_evaluate_provenance_all_pass_test3_nie_dotyczy():
    # Decyzja ownera: "nie dotyczy" (brak transakcji po D ryzyka w ogóle) NIE
    # blokuje PASS zbiorczego, o ile testy 1/2 PASS.
    result = model.evaluate_provenance(True, True, "nie_dotyczy")
    assert result.overall_status == "PASS"
    assert result.passed is True
    assert result.note == "kontrola dodatnia nie dotyczy: brak danych po D"


def test_evaluate_provenance_test3_niedostepna_is_nierozstrzygniety_not_fail():
    # Decyzja ownera: transakcje po D ryzyka ISTNIEJĄ, ale kontrola dodatnia
    # nie znalazła czego sprawdzić -> "NIEROZSTRZYGNIETY", ODDZIELNY status od
    # "FAIL" (choć renderowanie traktuje go jak FAIL — `passed` = False).
    result = model.evaluate_provenance(True, True, "niedostepna")
    assert result.overall_status == "NIEROZSTRZYGNIETY"
    assert result.passed is False
    assert result.failed_tests == []  # "niedostepna" to NIE "FAIL"
    assert result.note == "kontrola dodatnia niedostepna mimo danych po D"


def test_evaluate_provenance_test1_fail_overrides_niedostepna_to_fail():
    result = model.evaluate_provenance(False, True, "niedostepna")
    assert result.overall_status == "FAIL"
    assert result.passed is False
    assert len(result.failed_tests) == 1
    assert "test 1" in result.failed_tests[0]


def test_evaluate_provenance_test3_fail():
    result = model.evaluate_provenance(True, True, "fail")
    assert result.overall_status == "FAIL"
    assert result.passed is False
    assert len(result.failed_tests) == 1
    assert "test 3" in result.failed_tests[0]


def test_evaluate_provenance_multiple_fail():
    result = model.evaluate_provenance(False, False, "pass")
    assert result.overall_status == "FAIL"
    assert result.passed is False
    assert len(result.failed_tests) == 2


def test_evaluate_test1_single_run_identical_timestamps_pass():
    ts = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    assert model.evaluate_test1_single_run([ts, ts, ts]) is True


def test_evaluate_test1_single_run_within_5s_tolerance_pass():
    lo = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    hi = datetime(2026, 9, 25, 10, 0, 5, tzinfo=timezone.utc)
    assert model.evaluate_test1_single_run([lo, hi]) is True


def test_evaluate_test1_single_run_exceeds_5s_tolerance_fails():
    lo = datetime(2026, 9, 25, 10, 0, 0, tzinfo=timezone.utc)
    hi = datetime(2026, 9, 25, 10, 0, 6, tzinfo=timezone.utc)
    assert model.evaluate_test1_single_run([lo, hi]) is False


def test_evaluate_test1_single_run_empty_is_false():
    assert model.evaluate_test1_single_run([]) is False


def test_evaluate_test2_position_set_match():
    keys = {("AKCYJNY 1", 1, "PLN")}
    assert model.evaluate_test2_position_set_match(keys, keys) is True
    assert model.evaluate_test2_position_set_match(keys, set()) is False


def test_evaluate_test3_nie_dotyczy_when_no_transactions_after_risk_date():
    # Decyzja ownera: "nie dotyczy" rozstrzyga PIERWSZE (brak transakcji po D
    # ryzyka w ogóle) — niezależnie od tego, co by dała kontrola dodatnia.
    assert model.evaluate_test3_closed_positions_provenance(False, []) == "nie_dotyczy"
    items = [{
        "has_risk_daily_row": True,
        "computed_at": datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc),
        "max_sell_created_at": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
    }]
    assert model.evaluate_test3_closed_positions_provenance(False, items) == "nie_dotyczy"


def test_evaluate_test3_niedostepna_when_transactions_after_but_no_closed_position():
    # Transakcje po D ryzyka ISTNIEJĄ, ale kontrola dodatnia nie znalazła
    # żadnej pozycji zamkniętej do sprawdzenia -> "niedostepna" (decyzja
    # ownera), ODDZIELNE od "nie_dotyczy" i od "fail".
    assert model.evaluate_test3_closed_positions_provenance(True, []) == "niedostepna"


def test_evaluate_test3_missing_risk_daily_row_fails():
    items = [{"has_risk_daily_row": False, "computed_at": None, "max_sell_created_at": None}]
    assert model.evaluate_test3_closed_positions_provenance(True, items) == "fail"


def test_evaluate_test3_computed_at_before_sell_fails():
    items = [{
        "has_risk_daily_row": True,
        "computed_at": datetime(2026, 9, 24, 10, 0, tzinfo=timezone.utc),
        "max_sell_created_at": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
    }]
    assert model.evaluate_test3_closed_positions_provenance(True, items) == "fail"


def test_evaluate_test3_computed_at_after_sell_passes():
    items = [{
        "has_risk_daily_row": True,
        "computed_at": datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc),
        "max_sell_created_at": datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc),
    }]
    assert model.evaluate_test3_closed_positions_provenance(True, items) == "pass"


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
        "price_is_stale": False,  # kontrakt §5: cena użyta w ryzyku (T27)
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


def test_price_coverage_counts_eligible_rows_with_price():
    rows = [
        _risk_row(close_d=Decimal("1")),
        _risk_row(close_d=None),
        _risk_row(is_core=True, close_d=Decimal("1")),  # nie liczone do N (nieeligible)
    ]
    n, big_n = model.price_coverage(rows, is_risk_budget_eligible)
    assert (n, big_n) == (1, 2)


def test_count_stale_risk_rows_counts_eligible_stale_only():
    # kontrakt §5: licznik cen nieświeżych w risk_daily zastępuje etykietę
    # B-19 — n = wiersze SAT+FUT z price_is_stale=True, N = wszystkie SAT+FUT.
    rows = [
        _risk_row(price_is_stale=True),
        _risk_row(price_is_stale=False),
        _risk_row(is_core=True, price_is_stale=True),  # core -> wykluczone z N
        _risk_row(instrument_type="future", price_is_stale=True),
    ]
    n, big_n = model.count_stale_risk_rows(rows, is_risk_budget_eligible)
    assert (n, big_n) == (2, 3)


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

    prov = model.evaluate_provenance(True, True, "pass")
    d2 = asdict(prov)
    assert set(d2.keys()) == {"passed", "overall_status", "tests", "failed_tests", "note"}
    assert all(set(asdict_t.keys()) == {"name", "status"} for asdict_t in d2["tests"])


# ---------------------------------------------------------------------------
# Import generate.py i sygnatury funkcji importowanych z mannaz (B-26)
# ---------------------------------------------------------------------------
# generate.py wywołuje je POZYCYJNIE (patrz wywołania w generate.py), więc
# oczekiwane są dokładnie te nazwy wymaganych parametrów w tej kolejności;
# parametry nadmiarowe muszą mieć wartość domyślną. Test wykrywa zmianę nazwy
# lub parametrów; zmiany semantyki nie wykrywa (ujawni ją uruchomienie generatora).

import inspect  # noqa: E402

EXPECTED_REQUIRED_PARAMS = {
    ("mannaz.risk", "open_positions_as_of"): ["cur", "as_of"],
    ("mannaz.risk", "resolve_position_price_coverage"): ["cur", "pos", "as_of"],
    ("mannaz.risk", "read_kontraktowy_rows"): ["cur", "as_of"],
    ("mannaz.risk", "check_kontraktowy_coverage"): ["n_rows", "max_date", "has_open_futures", "as_of"],
    ("mannaz.risk", "kontraktowy_account_value"): ["rows", "as_of"],
    ("mannaz.risk", "is_risk_budget_eligible"): ["instrument_type", "is_core"],
    ("mannaz.fifo", "positions_as_of"): ["conn_or_cur", "as_of"],
}


def test_generate_importuje_sie_bez_bazy_i_uzywa_tych_samych_funkcji():
    import importlib

    import generate  # noqa: F401 — import modułu, bez uruchamiania main()

    for (module_name, func_name) in EXPECTED_REQUIRED_PARAMS:
        source = getattr(importlib.import_module(module_name), func_name)
        assert getattr(generate, func_name) is source, f"generate.{func_name} nie wskazuje {module_name}.{func_name}"


def test_sygnatury_funkcji_mannaz_zgodne_z_wywolaniami_generate():
    import importlib

    for (module_name, func_name), expected in EXPECTED_REQUIRED_PARAMS.items():
        params = list(inspect.signature(getattr(importlib.import_module(module_name), func_name)).parameters.values())
        positional = [p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        required = [p.name for p in positional if p.default is inspect.Parameter.empty]
        assert required == expected, f"{module_name}.{func_name}: wymagane {required} != oczekiwane {expected}"
        extra_required = [
            p.name for p in params
            if p.kind in (p.KEYWORD_ONLY,) and p.default is inspect.Parameter.empty
        ]
        assert not extra_required, f"{module_name}.{func_name}: nowe wymagane parametry keyword-only {extra_required}"


# B-32: poziom 1 per NAZWĘ (nie per wiersz)


def _name_row(key, pct, name_pct, breach, label="AAA", cur="USD"):
    return _risk_row(
        name_key=key, name_label=label, settlement_currency=cur,
        risk_pct_satellite_capital=pct, name_risk_pct=name_pct, level1_breach=breach,
    )


def test_name_breach_positive_two_rows_one_name_counts_once():
    rows = [_name_row(1, Decimal("0.6"), Decimal("1.2"), True, cur="USD"),
            _name_row(1, Decimal("0.6"), Decimal("1.2"), True, cur="PLN")]
    assert model.count_level1_name_breaches(rows, is_risk_budget_eligible) == 1
    assert model.count_level1_incomplete_names(rows, is_risk_budget_eligible) == 0


def test_name_breach_negative_two_names_zero():
    rows = [_name_row(1, Decimal("0.6"), Decimal("0.6"), False, "AAA"),
            _name_row(2, Decimal("0.6"), Decimal("0.6"), False, "BBB")]
    assert model.count_level1_name_breaches(rows, is_risk_budget_eligible) == 0
    assert model.count_level1_incomplete_names(rows, is_risk_budget_eligible) == 0


def test_name_incomplete_counted_separately_not_clean():
    rows = [_name_row(1, Decimal("0.6"), None, None),
            _name_row(2, Decimal("0.2"), Decimal("0.2"), False, "BBB")]
    assert model.count_level1_incomplete_names(rows, is_risk_budget_eligible) == 1
    assert model.count_level1_name_breaches(rows, is_risk_budget_eligible) == 0

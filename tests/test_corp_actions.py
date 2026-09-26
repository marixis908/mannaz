"""Testy czystej logiki detektora splitów — wyłącznie dane syntetyczne, bez
sieci/bazy. Brief CC-P, poprawka P2.3/P2.5."""

from datetime import date
from decimal import Decimal

from mannaz.corp_actions import (
    RatioSample,
    classify_ratio,
    resolve_event_date,
    yahoo_symbol_for,
    _pence_adjustment_factor,
)


# ---------------------------------------------------------------------------
# yahoo_symbol_for — mapowanie minimalne
# ---------------------------------------------------------------------------


def test_gpw_pln_akcyjny_gets_wa_suffix():
    assert (
        yahoo_symbol_for(broker_ticker="CDR", currency="PLN", rachunek="AKCYJNY 000001", is_core=False)
        == "CDR.WA"
    )


def test_us_usd_zagraniczny_no_suffix():
    assert (
        yahoo_symbol_for(broker_ticker="APH", currency="USD", rachunek="ZAGRANICZNY 000003", is_core=False)
        == "APH"
    )


def test_core_eur_etf_gets_de_suffix():
    assert (
        yahoo_symbol_for(broker_ticker="SPYI", currency="EUR", rachunek="ZAGRANICZNY 000003", is_core=True)
        == "SPYI.DE"
    )


def test_non_core_eur_is_ambiguous_and_skipped():
    # Może być ADR na Xetra ALBO spółka natywnie europejska — brak pełnego
    # mapowania (P3.1) -> pomijamy zamiast zgadywać.
    assert (
        yahoo_symbol_for(broker_ticker="ASML", currency="EUR", rachunek="ZAGRANICZNY 000003", is_core=False)
        is None
    )


def test_future_in_kontraktowy_pln_is_not_akcyjny_and_skipped():
    assert (
        yahoo_symbol_for(broker_ticker="FDNPM26", currency="PLN", rachunek="KONTRAKTOWY 000002", is_core=False)
        is None
    )


def test_malformed_ticker_skipped():
    assert (
        yahoo_symbol_for(broker_ticker="ABC/DEF", currency="USD", rachunek="ZAGRANICZNY 000003", is_core=False)
        is None
    )


# ---------------------------------------------------------------------------
# classify_ratio
# ---------------------------------------------------------------------------


def test_ratio_close_to_one_is_not_a_split():
    assert classify_ratio(Decimal("1.02")) is None
    assert classify_ratio(Decimal("0.95")) is None


def test_ratio_close_to_two_is_split():
    event_type, ratio = classify_ratio(Decimal("1.9868"))
    assert event_type == "split"
    assert ratio == Decimal(2)


def test_ratio_close_to_twenty_five_is_split():
    # SPYI-like: broker ~170 EUR, close_yahoo ~6.8 EUR -> ratio ~25
    event_type, ratio = classify_ratio(Decimal("25.11"))
    assert event_type == "split"
    assert ratio == Decimal(25)


def test_ratio_close_to_half_is_reverse_split():
    event_type, ratio = classify_ratio(Decimal("0.51"))
    assert event_type == "reverse_split"
    assert ratio == Decimal("0.5")


def test_ratio_deviates_but_not_integer_form_is_rejected():
    # odbiega o >30% od 1, ale nie jest bliskie żadnej liczbie całkowitej ani 1/N
    assert classify_ratio(Decimal("1.42")) is None


def test_ratio_zero_or_negative_rejected():
    assert classify_ratio(Decimal(0)) is None
    assert classify_ratio(Decimal(-5)) is None


# ---------------------------------------------------------------------------
# resolve_event_date
# ---------------------------------------------------------------------------


def test_resolve_event_date_matches_yfinance_split_when_available():
    # APH-like: mediana ~2, yfinance ma zarejestrowany split 2:1 na konkretną datę
    samples = [RatioSample(transaction_date=date(2026, 5, 27), ratio=Decimal("1.9868"))]
    yf_splits = [(date(2026, 9, 3), Decimal("2.0"))]
    event_date, date_source = resolve_event_date(samples, Decimal(2), yf_splits)
    assert event_date == date(2026, 9, 3)
    assert date_source == "yfinance_splits"


def test_resolve_event_date_boundary_when_ratio_transitions_to_one():
    samples = [
        RatioSample(transaction_date=date(2024, 1, 1), ratio=Decimal("2.05")),
        RatioSample(transaction_date=date(2024, 1, 5), ratio=Decimal("2.0")),
        RatioSample(transaction_date=date(2024, 6, 1), ratio=Decimal("1.01")),  # po realnym splicie
        RatioSample(transaction_date=date(2024, 7, 1), ratio=Decimal("0.99")),
    ]
    event_date, date_source = resolve_event_date(samples, Decimal(2), yf_splits=[])
    assert event_date == date(2024, 6, 1)
    assert date_source == "inferred_boundary"


def test_resolve_event_date_inferred_all_when_ratio_never_returns_to_one():
    # SPYI-like: yfinance splits puste, WSZYSTKIE próbki mają ratio != 1
    samples = [
        RatioSample(transaction_date=date(2023, 10, 25), ratio=Decimal("25.11")),
        RatioSample(transaction_date=date(2023, 11, 10), ratio=Decimal("25.18")),
    ]
    event_date, date_source = resolve_event_date(samples, Decimal(25), yf_splits=[])
    assert event_date == date(2023, 11, 11)  # dzień po ostatniej transakcji
    assert date_source == "inferred_all"


# ---------------------------------------------------------------------------
# Konwersja GBp/GBP (guard ogólny, brief wymaga jawnej obsługi)
# ---------------------------------------------------------------------------


def test_pence_adjustment_applies_only_for_lse_symbol_and_gbp_currency():
    assert _pence_adjustment_factor("VOD.L", "GBP") == Decimal("0.01")
    assert _pence_adjustment_factor("VOD.L", "GBp") == Decimal(1)  # już w groszach, bez podwójnej korekty
    assert _pence_adjustment_factor("APH", "USD") == Decimal(1)

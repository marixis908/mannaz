"""Testy czystej logiki prices_daily — wyłącznie dane syntetyczne, bez
sieci/bazy. Brief CC-P, P3.2."""

from datetime import date
from decimal import Decimal

from mannaz.prices import (
    check_currency,
    cumulative_ratio_after,
    detect_log_return_outliers,
    reconstruct_raw,
)


# ---------------------------------------------------------------------------
# cumulative_ratio_after / reconstruct_raw — odtwarzanie *_raw
# ---------------------------------------------------------------------------


def test_cumulative_ratio_after_no_events_is_one():
    assert cumulative_ratio_after(date(2024, 1, 1), []) == Decimal(1)


def test_cumulative_ratio_after_single_future_split_applies():
    events = [(date(2024, 6, 1), Decimal(25))]
    # sesja PRZED zdarzeniem -> mnożnik = ratio zdarzenia (odtwarzamy raw)
    assert cumulative_ratio_after(date(2024, 1, 1), events) == Decimal(25)
    # sesja PO zdarzeniu -> brak korekty
    assert cumulative_ratio_after(date(2024, 12, 1), events) == Decimal(1)


def test_cumulative_ratio_after_multiple_splits_compound():
    # SPYI-like (25:1) potem APH-like (2:1) później w czasie — sesja sprzed
    # OBU zdarzeń dostaje iloczyn obu ratio.
    events = [(date(2024, 3, 1), Decimal(25)), (date(2024, 9, 1), Decimal(2))]
    assert cumulative_ratio_after(date(2024, 1, 1), events) == Decimal(50)
    assert cumulative_ratio_after(date(2024, 6, 1), events) == Decimal(2)
    assert cumulative_ratio_after(date(2024, 12, 1), events) == Decimal(1)


def test_reconstruct_raw_none_value_stays_none():
    assert reconstruct_raw(None, date(2024, 1, 1), [(date(2024, 6, 1), Decimal(2))]) is None


def test_reconstruct_raw_spyi_like_example():
    # broker ~170 EUR w 2023-10, close_split_adj yahoo ~6.8 EUR, ratio 25
    # (empiria P2.3) -> raw odtworzony ~170.
    events = [(date(2023, 11, 11), Decimal(25))]
    raw = reconstruct_raw(Decimal("6.8"), date(2023, 10, 25), events)
    assert raw == Decimal("170.0")


# ---------------------------------------------------------------------------
# check_currency (T7)
# ---------------------------------------------------------------------------


def test_check_currency_match():
    assert check_currency("USD", "USD") == "match"


def test_check_currency_mismatch():
    assert check_currency("EUR", "USD") == "mismatch"


def test_check_currency_gbp_pence_is_own_state_not_silently_equal():
    assert check_currency("GBp", "GBP") == "gbp_pence_conversion_needed"
    assert check_currency("GBX", "GBP") == "gbp_pence_conversion_needed"


def test_check_currency_no_data():
    assert check_currency(None, "USD") == "no_data"


# ---------------------------------------------------------------------------
# detect_log_return_outliers (T8) — kontrolka dodatnia/ujemna z briefu P3.2
# ---------------------------------------------------------------------------


def test_t8_negative_control_normal_series_no_hits():
    closes = [
        (date(2024, 1, 1), Decimal("100")),
        (date(2024, 1, 2), Decimal("101")),
        (date(2024, 1, 3), Decimal("99")),
        (date(2024, 1, 4), Decimal("100.5")),
    ]
    assert detect_log_return_outliers(closes) == []


def test_t8_positive_control_last_session_times_100_exactly_one_hit():
    # Brief P3.2: "kontrolka syntetyczna (kopia jednego szeregu, jedna sesja
    # ×100 -> dokładnie 1 trafienie)". Zmieniamy OSTATNIĄ sesję, żeby istniała
    # tylko JEDNA para zwrotu, którą to dotyka (brak sesji PO niej).
    base = [
        (date(2024, 1, 1), Decimal("100")),
        (date(2024, 1, 2), Decimal("101")),
        (date(2024, 1, 3), Decimal("99")),
        (date(2024, 1, 4), Decimal("100.5")),
    ]
    control = base[:-1] + [(base[-1][0], base[-1][1] * 100)]
    hits = detect_log_return_outliers(control)
    assert len(hits) == 1
    assert hits[0][0] == base[-1][0]


def test_t8_zero_or_negative_close_ignored_not_crashed():
    closes = [
        (date(2024, 1, 1), Decimal("100")),
        (date(2024, 1, 2), Decimal("0")),
        (date(2024, 1, 3), Decimal("101")),
    ]
    # brak wyjątku (dzielenie/log przez zero pominięte), brak fałszywych trafień
    assert detect_log_return_outliers(closes) == []

"""Testy czystej logiki prices_daily — wyłącznie dane syntetyczne, bez
sieci/bazy. Brief CC-P, P3.2."""

from datetime import date
from decimal import Decimal

from mannaz.prices import (
    OhlcRow,
    _to_decimal,
    check_currency,
    cumulative_ratio_after,
    detect_log_return_outliers,
    reconstruct_raw,
    split_valid_and_rejected,
    validate_price_row,
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


# ---------------------------------------------------------------------------
# I2 (brief CC-I, B-21) — _to_decimal (±Infinity) / validate_price_row /
# split_valid_and_rejected — logika czysta, bez sieci/bazy.
# ---------------------------------------------------------------------------


def test_to_decimal_infinity_becomes_none():
    # T12: ±Infinity nigdy nie ma dotrzeć do zapisu — filtrowane już w _to_decimal.
    assert _to_decimal(float("inf")) is None
    assert _to_decimal(float("-inf")) is None


def test_to_decimal_nan_still_becomes_none():
    assert _to_decimal(float("nan")) is None


def test_to_decimal_normal_value_unaffected():
    assert _to_decimal(10.5) == Decimal("10.5")


def _make_row(
    price_date: date = date(2024, 1, 1),
    high: str | None = "11",
    low: str | None = "9",
    close: str | None = "10.5",
) -> OhlcRow:
    return OhlcRow(
        price_date=price_date,
        open_split_adj=Decimal("10"),
        high_split_adj=Decimal(high) if high is not None else None,
        low_split_adj=Decimal(low) if low is not None else None,
        close_split_adj=Decimal(close) if close is not None else None,
        volume_split_adj=Decimal("1000"),
        adj_close_total_return=Decimal(close) if close is not None else None,
    )


def test_validate_price_row_all_present_no_bad_columns():
    assert validate_price_row(_make_row()) == []


def test_validate_price_row_none_close_flagged():
    assert validate_price_row(_make_row(close=None)) == ["close_split_adj"]


def test_validate_price_row_infinity_high_flagged():
    row = _make_row()
    row.high_split_adj = Decimal("Infinity")
    assert validate_price_row(row) == ["high_split_adj"]


def test_validate_price_row_negative_infinity_low_flagged():
    row = _make_row()
    row.low_split_adj = Decimal("-Infinity")
    assert validate_price_row(row) == ["low_split_adj"]


def test_validate_price_row_multiple_bad_columns_order_preserved():
    row = _make_row(low=None, close=None)
    assert validate_price_row(row) == ["low_split_adj", "close_split_adj"]


def test_validate_price_row_open_and_volume_not_checked():
    # open_split_adj/volume_split_adj NIE są w liście wymaganych (I2) — puste
    # tam nie dyskwalifikuje wiersza (close_raw/high_raw/low_raw zależą tylko
    # od high/low/close_split_adj, patrz docstring modułu).
    row = _make_row()
    row.open_split_adj = None
    row.volume_split_adj = None
    assert validate_price_row(row) == []


def test_split_valid_and_rejected_empty_list():
    assert split_valid_and_rejected([]) == ([], [])


def test_split_valid_and_rejected_partitions_one_bad_row():
    good = _make_row(price_date=date(2024, 1, 1))
    bad = _make_row(price_date=date(2024, 1, 2), close=None)
    valid, rejected = split_valid_and_rejected([good, bad])
    assert valid == [good]
    assert len(rejected) == 1
    assert rejected[0][0] is bad
    assert rejected[0][1] == ["close_split_adj"]


def test_split_valid_and_rejected_all_rows_bad():
    bad1 = _make_row(close=None)
    bad2 = _make_row(high=None)
    valid, rejected = split_valid_and_rejected([bad1, bad2])
    assert valid == []
    assert [r[0] for r in rejected] == [bad1, bad2]

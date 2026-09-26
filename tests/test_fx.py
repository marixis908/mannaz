"""Testy czystej logiki FX — wyłącznie dane syntetyczne, bez sieci/bazy.
Brief CC-P, P3.3."""

from datetime import date
from decimal import Decimal

from mannaz.fx import _chunk_date_ranges, compute_fx_control


# ---------------------------------------------------------------------------
# _chunk_date_ranges — limit NBP <=93 dni na zapytanie
# ---------------------------------------------------------------------------


def test_chunk_date_ranges_single_chunk_when_within_limit():
    ranges = _chunk_date_ranges(date(2024, 1, 1), date(2024, 1, 10))
    assert ranges == [(date(2024, 1, 1), date(2024, 1, 10))]


def test_chunk_date_ranges_splits_at_93_days():
    start = date(2023, 10, 1)
    end = date(2024, 3, 1)  # > 93 dni od startu
    ranges = _chunk_date_ranges(start, end)
    assert ranges[0] == (start, date(2023, 10, 1) + __import__("datetime").timedelta(days=92))
    # brak dziur/nakładek: koniec chunku i+1 zaczyna się dzień po końcu chunku i
    for (s1, e1), (s2, _e2) in zip(ranges, ranges[1:]):
        assert s2 == e1 + __import__("datetime").timedelta(days=1)
    assert ranges[-1][1] == end
    assert all((e - s).days <= 92 for s, e in ranges)


def test_chunk_date_ranges_exact_93_day_boundary_is_single_chunk():
    start = date(2024, 1, 1)
    end = start + __import__("datetime").timedelta(days=92)  # dokładnie 93 dni łącznie
    ranges = _chunk_date_ranges(start, end)
    assert ranges == [(start, end)]


# ---------------------------------------------------------------------------
# compute_fx_control — mediana |frankfurter/nbp - 1| w %
# ---------------------------------------------------------------------------


def test_compute_fx_control_identical_rates_zero_diff():
    dates = [date(2024, 1, d) for d in range(1, 6)]
    nbp = [(d, Decimal("4.00")) for d in dates]
    ff = [(d, Decimal("4.00")) for d in dates]
    result = compute_fx_control(nbp, ff, "USD")
    assert result.n_sessions_compared == 5
    assert result.median_abs_pct_diff == Decimal("0")


def test_compute_fx_control_one_percent_deviation():
    dates = [date(2024, 1, d) for d in range(1, 4)]
    nbp = [(d, Decimal("4.00")) for d in dates]
    ff = [(d, Decimal("4.04")) for d in dates]  # +1% dokładnie
    result = compute_fx_control(nbp, ff, "USD")
    assert result.median_abs_pct_diff == Decimal("1.00")


def test_compute_fx_control_uses_only_common_dates():
    nbp = [(date(2024, 1, 1), Decimal("4.00")), (date(2024, 1, 2), Decimal("4.00"))]
    ff = [(date(2024, 1, 2), Decimal("4.00"))]  # tylko jedna wspólna data
    result = compute_fx_control(nbp, ff, "USD")
    assert result.n_sessions_compared == 1


def test_compute_fx_control_no_common_dates():
    nbp = [(date(2024, 1, 1), Decimal("4.00"))]
    ff = [(date(2024, 2, 1), Decimal("4.00"))]
    result = compute_fx_control(nbp, ff, "USD")
    assert result.n_sessions_compared == 0
    assert result.median_abs_pct_diff is None
    assert result.note != ""


def test_compute_fx_control_limits_to_n_most_recent_sessions():
    dates = [date(2024, 1, d) for d in range(1, 11)]
    nbp = [(d, Decimal("4.00")) for d in dates]
    ff = [(d, Decimal("4.00")) for d in dates[:5]] + [(d, Decimal("4.40")) for d in dates[5:]]
    result = compute_fx_control(nbp, ff, "USD", n_sessions=5)
    # tylko 5 najnowszych wspolnych dat -> wszystkie z odchyleniem 10%
    assert result.n_sessions_compared == 5
    assert result.median_abs_pct_diff == Decimal("10.00")

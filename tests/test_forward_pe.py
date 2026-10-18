"""Testy czystej logiki forward P/E - dane syntetyczne, bez sieci/bazy.
Brief CC forward-pe rew. 1, par. 5 (tabela S1..S9 ustalona przez planiste)."""

from datetime import date

import pytest

from mannaz.forward_pe import (
    MEASURE_NAME,
    PeriodEstimate,
    Position,
    Snapshot,
    aggregate,
    eps_ntm,
    freshness,
    gaap_gap,
    growth_status,
    divergence,
    pe_ratio,
    revision_fixed,
)

ABS = 1e-6
PCT = 1e-4
E = date(2026, 12, 31)


def test_s1_ntm_weight_and_eps():
    r = eps_ntm(date(2026, 10, 1), E, 10.0, 12.0)
    assert r.w == pytest.approx(0.249315, abs=ABS)
    assert r.w == pytest.approx(91 / 365, abs=1e-12)
    assert r.eps == pytest.approx(11.501370, abs=ABS)
    assert r.measure == MEASURE_NAME


def test_s2_ntm_drift_and_fixed_revision_zero():
    a = eps_ntm(date(2026, 7, 1), E, 10.0, 20.0).eps
    b = eps_ntm(date(2026, 10, 1), E, 10.0, 20.0).eps
    assert a == pytest.approx(14.986301, abs=ABS)
    assert b == pytest.approx(17.506849, abs=ABS)
    assert (b / a - 1) * 100 == pytest.approx(16.8190, abs=PCT)
    periods = (
        PeriodEstimate("0y", date(2026, 12, 31), 10.0),
        PeriodEstimate("+1y", date(2027, 12, 31), 20.0),
    )
    sa = Snapshot(date(2026, 7, 1), periods)
    sb = Snapshot(date(2026, 10, 1), periods)
    r1 = revision_fixed(sa, sb, date(2026, 12, 31))
    r2 = revision_fixed(sa, sb, date(2027, 12, 31))
    assert r1.rel == 0.0 and r2.rel == 0.0
    assert r1.days_between == 92


def test_s3_label_shift_after_fiscal_year_rollover():
    sa = Snapshot(
        date(2027, 1, 1),
        (
            PeriodEstimate("0y", date(2026, 8, 31), 75.0),
            PeriodEstimate("+1y", date(2027, 8, 31), 175.0),
        ),
    )
    sb = Snapshot(
        date(2027, 10, 1),
        (
            PeriodEstimate("0y", date(2027, 8, 31), 175.0),
            PeriodEstimate("+1y", date(2028, 8, 31), 200.0),
        ),
    )
    fixed = revision_fixed(sa, sb, date(2027, 8, 31))
    assert fixed.rel == 0.0
    by_label = 175.0 / 75.0 - 1
    assert by_label == pytest.approx(1.333333, abs=ABS)
    assert by_label != fixed.rel
    missing = revision_fixed(sa, sb, date(2026, 8, 31))
    assert missing.status == "brak porównywalnej rewizji"
    assert missing.rel is None and missing.abs_change is None


def test_s4_growth_status():
    g = growth_status(5, 6)
    assert g.status == "wzrost procentowy"
    assert g.pct == pytest.approx(20.0, abs=PCT)
    g = growth_status(-2, 1)
    assert g.status == "oczekiwane przejście ze straty do zysku"
    assert g.pct is None and g.delta == 3
    g = growth_status(2, -1)
    assert g.status == "oczekiwane przejście z zysku do straty"
    assert g.pct is None and g.delta == -3
    g = growth_status(-3, -1)
    assert g.status == "oczekiwane zmniejszenie straty"
    assert g.pct is None and g.delta == 2
    g = growth_status(-1, -3)
    assert g.status == "oczekiwane pogłębienie straty"
    assert g.pct is None and g.delta == -2
    for ttm, ntm in ((0, 4), (4, 0)):
        g = growth_status(ttm, ntm)
        assert g.status == "zerowa baza — wzrost procentowy nieokreślony"
        assert g.pct is None


def test_s5_gaap_gap():
    g = gaap_gap(2.00, 0.01)
    assert g.ratio is None and g.ratio_status == "iloraz nieokreślony"
    assert g.diff == pytest.approx(1.99, abs=ABS)
    g = gaap_gap(2.00, -0.50)
    assert g.ratio is None and g.ratio_status == "iloraz nieokreślony"
    assert g.diff == pytest.approx(2.50, abs=ABS)
    g = gaap_gap(2.00, 1.00)
    assert g.ratio == pytest.approx(2.0, abs=ABS) and g.ratio_status is None
    assert g.diff == pytest.approx(1.00, abs=ABS)


def test_s6_pe_ratio():
    assert pe_ratio(100, 5) == 20.0
    assert pe_ratio(50, 2.5) == 20.0


def test_s7a_aggregate_losses_offset():
    a = aggregate([Position(0.5, "dane", 0.05), Position(0.5, "dane", -0.05)])
    assert a.ep_covered == pytest.approx(0.0, abs=ABS)
    assert a.pe_covered_positive == pytest.approx(20.0, abs=ABS)
    assert a.coverage_positive == pytest.approx(0.5, abs=ABS)
    assert a.coverage_data == pytest.approx(1.0, abs=ABS)
    assert a.pe_equity is None
    assert a.pe_equity_status == "nieinterpretowalne (E/P ≤ 0)"


def test_s7b_aggregate_missing_data():
    a = aggregate(
        [
            Position(0.4, "dane", 0.05),
            Position(0.4, "dane", 0.04),
            Position(0.2, "brak danych", None),
        ]
    )
    assert a.ep_covered * 100 == pytest.approx(4.5, abs=PCT)
    # decyzja planisty FPE r1 pkt 3: 200/9 (22.222222), tolerancja 1e-6
    assert a.pe_covered_positive == pytest.approx(22.222222, abs=ABS)
    assert a.pe_covered_positive == pytest.approx(200 / 9, abs=ABS)
    assert a.coverage_data == pytest.approx(0.8, abs=ABS)
    assert a.pe_equity is None
    assert a.pe_equity_status == "nieinterpretowalne (braki danych)"


def test_s7c_aggregate_cash_outside_valuation():
    # decyzja planisty FPE r1 pkt 4
    a = aggregate([Position(0.5, "dane", 0.05), Position(0.5, "gotówka", None)])
    assert a.pe_equity == pytest.approx(20.0, abs=ABS)
    assert a.pe_equity_status is None
    assert a.pe_equity_label == "P/E części akcyjnej"
    assert a.coverage_data == pytest.approx(1.0, abs=ABS)
    assert a.cash_share == pytest.approx(0.5, abs=ABS)
    assert a.weights["gotówka"] == pytest.approx(0.5, abs=ABS)


def test_f5_tiny_missing_weight_blocks_pe():
    # decyzja planisty FPE r1b pkt 2a (kontrprzyklad Codex r1b)
    a = aggregate([Position(0.9999999995, "dane", 0.05), Position(0.0000000005, "brak danych", None)])
    assert a.pe_equity is None
    assert a.pe_equity_status == "nieinterpretowalne (braki danych)"


def test_f5_float_noise_weights_without_missing():
    # decyzja planisty FPE r1b pkt 2b: 0.1+0.2+0.7 != 1.0 w float, status z kategorii
    a = aggregate([Position(0.1, "dane", 0.05), Position(0.2, "dane", 0.05), Position(0.7, "dane", 0.05)])
    assert a.pe_equity == pytest.approx(20.0, abs=ABS)
    assert a.pe_equity_status is None


def test_s7b_missing_not_replaced_by_zero():
    with pytest.raises(ValueError):
        Position(0.2, "brak danych", 0.0)


def test_s8_freshness_unknown():
    f = freshness(None, today=date.today())
    assert f.status == "wiek konsensusu nieznany"
    assert f.age_days is None


def test_s10_ntm_after_fy1_end_clamps_to_zero():
    # decyzja planisty FPE r1 pkt 5: FY1 zamkniety, niezaraportowany (przypadek IFX)
    r = eps_ntm(date(2026, 10, 18), date(2026, 9, 30), 10.0, 12.0)
    assert r.w == pytest.approx(0.0, abs=ABS)
    assert r.eps == pytest.approx(12.0, abs=ABS)


def test_s9_divergence():
    d = divergence(100, 111)
    assert d.rel == pytest.approx(0.11, abs=ABS) and d.flag is True
    d = divergence(100, 109)
    assert d.rel == pytest.approx(0.09, abs=ABS) and d.flag is False
    for p, c in ((0.04, 0.06), (1, -1)):
        d = divergence(p, c)
        assert d.status == "baza bliska zeru / zmiana znaku"
        assert d.rel is None and d.flag is None


@pytest.mark.parametrize("tied_snapshot", ["a", "b", "both"])
def test_e13_period_match_tie_returns_status(tied_snapshot):
    from datetime import timedelta
    from mannaz.forward_pe import STATUS_PERIOD_TIE

    target = date(2026, 12, 31)
    tied = (PeriodEstimate("0y", target - timedelta(days=3), 10),
            PeriodEstimate("+1y", target + timedelta(days=3), 12))
    single = (PeriodEstimate("0y", target, 11),)
    a = Snapshot(date(2026, 9, 1), tied if tied_snapshot in ("a", "both") else single)
    b = Snapshot(date(2026, 10, 1), tied if tied_snapshot in ("b", "both") else single)
    result = revision_fixed(a, b, target)
    assert result.status == STATUS_PERIOD_TIE
    assert result.rel is None and result.abs_change is None and result.days_between == 30

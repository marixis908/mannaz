"""B-76 (K2, testy bez bazy): definicja wypełniacza dostawcy, detektor okna
sesji i render podsekcji DATA FAILURE. Dane syntetyczne."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from mannaz.cycle import STAGE_RISK, ReportState, has_data_failures, render_report
from mannaz.prices import find_window_fillers, is_provider_filler

D = Decimal


def test_is_provider_filler_flat_zero_volume_true():
    assert is_provider_filler(D("5"), D("5"), D("5"), D("5"), D("0")) is True
    assert is_provider_filler(5.0, 5.0, 5.0, 5.0, 0) is True


def test_is_provider_filler_flat_positive_volume_false():
    assert is_provider_filler(D("5"), D("5"), D("5"), D("5"), D("100")) is False


def test_is_provider_filler_volume_none_false():
    assert is_provider_filler(D("5"), D("5"), D("5"), D("5"), None) is False


def test_is_provider_filler_open_not_close_false():
    assert is_provider_filler(D("5"), D("5"), D("5"), D("5.1"), D("0")) is False
    assert is_provider_filler(None, D("5"), D("5"), D("5"), D("0")) is False


def _sessions(n: int, end: date) -> list[date]:
    """n kolejnych dni (kalendarzowo) kończących się na `end` — każdy to "sesja"."""
    return [end - timedelta(days=i) for i in range(n - 1, -1, -1)]


def _normal(symbol, exchange, day):
    return (symbol, exchange, day, D("10"), D("11"), D("9"), D("10.5"), D("1000"))


def _filler(symbol, exchange, day, price="10"):
    return (symbol, exchange, day, D(price), D(price), D(price), D(price), D("0"))


D0 = date(2026, 10, 15)


def test_find_window_fillers_positive_control():
    days = _sessions(30, D0)
    rows = [_normal("AAA", "XETR", x) for x in days[:-3]] + [_filler("AAA", "XETR", days[-2])]
    assert find_window_fillers(rows, D0) == [("AAA", "XETR", days[-2])]


def test_find_window_fillers_flat_with_volume_not_reported():
    days = _sessions(30, D0)
    rows = [_normal("AAA", "XETR", x) for x in days[:-1]]
    rows.append(("AAA", "XETR", days[-1], D("10"), D("10"), D("10"), D("10"), D("500")))
    assert find_window_fillers(rows, D0) == []


def test_find_window_fillers_window_edge_22nd_in_23rd_out():
    days = _sessions(40, D0)  # days[-1] == D0 (1. sesja wstecz), days[-22] = 22. wstecz

    def with_filler_on(target):
        rows = [_normal("AAA", "XETR", x) for x in days if x != target]
        return rows + [_filler("AAA", "XETR", target)]

    assert find_window_fillers(with_filler_on(days[-22]), D0) == [("AAA", "XETR", days[-22])]
    assert find_window_fillers(with_filler_on(days[-23]), D0) == []
    # wlacznie z d: filler na dniu d to 1. sesja wstecz
    assert find_window_fillers(with_filler_on(D0), D0) == [("AAA", "XETR", D0)]


def test_find_window_fillers_ignores_rows_after_d():
    days = _sessions(30, D0)
    rows = [_normal("AAA", "XETR", x) for x in days] + [_filler("AAA", "XETR", D0 + timedelta(days=1))]
    assert find_window_fillers(rows, D0) == []


def test_find_window_fillers_window_is_per_exchange():
    # gielda X handluje codziennie (22 sesje = 22 dni), gielda Y tylko co 2. dzien
    # (22 sesje = 44 dni). Filler 30 dni wstecz: poza oknem X, w oknie Y.
    far = D0 - timedelta(days=30)
    x_days = [D0 - timedelta(days=i) for i in range(0, 60)]
    y_days = [D0 - timedelta(days=i) for i in range(0, 60, 2)]
    y_days = [x for x in y_days if x != far] + []
    rows = [_normal("XXX", "XETR", x) for x in x_days if x != far] + [_filler("XXX", "XETR", far)]
    rows += [_normal("YYY", "XWAR", x) for x in y_days] + [_filler("YYY", "XWAR", far)]
    assert find_window_fillers(rows, D0) == [("YYY", "XWAR", far)]


def test_find_window_fillers_none_exchange_is_own_group_and_sorted():
    days = _sessions(5, D0)
    rows = [_normal("BBB", None, x) for x in days[:-2]]
    rows += [_filler("BBB", None, days[-2]), _filler("AAA", "XETR", days[-1]), _filler("AAA", "XETR", days[-3])]
    rows += [_normal("AAA", "XETR", days[-2])]
    assert find_window_fillers(rows, D0) == [
        ("AAA", "XETR", days[-3]),
        ("AAA", "XETR", days[-1]),
        ("BBB", None, days[-2]),
    ]


def _state(**kw) -> ReportState:
    return ReportState(d=D0, run_started_at=datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc), **kw)


def test_render_nonempty_fillers_has_subsection_and_data_failure():
    state = _state(window_fillers=[("AAA.DE", "XETR", date(2026, 10, 8)), ("AAA.DE", "XETR", date(2026, 10, 9))])
    assert has_data_failures(state) is True
    text = render_report(state)
    assert "### Świece-wypełniacze dostawcy w oknie 22 sesji (B-76)" in text
    assert "- AAA.DE (XETR): 2 (daty: 2026-10-08, 2026-10-09)" in text


def test_render_empty_fillers_brak_and_no_data_failure():
    state = _state()
    assert has_data_failures(state) is False
    text = render_report(state)
    head = "### Świece-wypełniacze dostawcy w oknie 22 sesji (B-76)"
    assert head in text
    after = text.split(head, 1)[1].lstrip("\n").splitlines()[0]
    assert after == "brak"


def test_render_fillers_not_executed_note_when_d_unresolved():
    state = ReportState(d=None, run_started_at=datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc))
    state.stage_not_executed.add(STAGE_RISK)
    text = render_report(state)
    after = text.split("### Świece-wypełniacze dostawcy w oknie 22 sesji (B-76)", 1)[1].lstrip("\n").splitlines()[0]
    assert after.startswith("nie wykonano")


def test_render_fillers_listed_even_when_risk_stage_failed_after_detection():
    # Detektor działa zaraz po ustaleniu D; etap ryzyka może zawieść później
    # (np. IncompleteRiskDateError) — lista FAILURE nie może zniknąć.
    state = _state(window_fillers=[("PKN.WA", "GPW", date(2026, 10, 13))])
    state.stage_not_executed.add(STAGE_RISK)
    assert has_data_failures(state) is True
    assert "- PKN.WA (GPW): 1 (daty: 2026-10-13)" in render_report(state)


def test_render_provider_revisions_section_filler_kept_line_only_when_positive():
    head = "## Rewizje cen dostawcy"
    assert "B-76): " not in render_report(_state()).split(head, 1)[1].split("\n## ", 1)[0]
    state = _state(filler_kept_rows=3, filler_kept_instruments=2)
    assert "- wypełniacze dostawcy pominięte (zapisany wiersz inny, B-76): 3 wierszy, 2 instrumentów" in render_report(state)

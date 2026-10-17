"""DQ: K1 (B-83) przeklasyfikowanie `row_missing_price` po D do HEARTBEAT oraz
B-82 (ticker i data brakow ceny pozycji na dniach NAV satelity w "D niekompletne").
Testy bez bazy: logika klasyfikacji (`reclassify_rows_after_d`) i render na
ReportState; zrodlo wpisow w C5 sprawdza test db (test_dq_db.py)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from mannaz.cycle import (
    ReportState,
    has_data_failures,
    reclassify_rows_after_d,
    render_report,
)
from _satstub import minimal_stage_result

D0 = date(2026, 10, 15)
AFTER = date(2026, 10, 16)


def _state(**kw) -> ReportState:
    return ReportState(d=None, run_started_at=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc), **kw)


def _with_entries(entries) -> ReportState:
    from mannaz.cycle import _format_ingest_error

    st = _state()
    st.price_ingest_entries = list(entries)
    st.price_ingest_errors = [_format_ingest_error(*e) for e in entries]
    return st


def _entry(symbol, day, error_type="row_missing_price", source="yahoo", detail="puste/niepoprawne kolumny: close"):
    return (source, symbol, day, error_type, detail)


def _section(text, head):
    return text.split(head, 1)[1].split("\n## ", 1)[0]


def test_k1_positive_row_missing_price_after_d_goes_to_heartbeat_no_failure():
    st = _with_entries([_entry("AAA.AS", AFTER), _entry("BBB.AS", AFTER)])
    assert has_data_failures(st) is True  # domyslnie wszystko jest FAILURE
    st.d = D0
    reclassify_rows_after_d(st, D0)
    assert st.price_ingest_errors == []
    assert has_data_failures(st) is False
    text = render_report(st)
    hb = _section(text, "## 🟢 HEARTBEAT")
    assert "- wiersze bez ceny po D (T12):" in hb
    assert "  - 2026-10-16: 2 (AAA.AS, BBB.AS)" in hb
    assert "brak" == _section(text, "### ingest_errors (ten przebieg)").split("\n###", 1)[0].strip()


def test_k1_negative_row_missing_price_before_d_stays_failure():
    st = _with_entries([_entry("AAA.AS", date(2026, 10, 14))])
    reclassify_rows_after_d(st, D0)
    assert len(st.price_ingest_errors) == 1 and st.rows_without_price_after_d == []
    st.d = D0
    assert has_data_failures(st) is True


def test_k1_edge_price_date_equal_d_stays_failure():
    st = _with_entries([_entry("AAA.AS", D0)])
    reclassify_rows_after_d(st, D0)
    assert len(st.price_ingest_errors) == 1 and st.rows_without_price_after_d == []


def test_k1_connection_error_after_d_still_failure():
    st = _with_entries(
        [_entry("AAA.AS", AFTER, error_type="connection_error"), _entry("BBB.AS", AFTER, error_type="empty_response"),
         _entry("CCC.AS", AFTER, error_type="provider_filler"), _entry("DDD.AS", AFTER)]
    )
    reclassify_rows_after_d(st, D0)
    assert len(st.price_ingest_errors) == 3
    assert st.rows_without_price_after_d == [("DDD.AS", AFTER)]
    st.d = D0
    assert has_data_failures(st) is True


def test_k1_row_missing_price_without_date_stays_failure():
    st = _with_entries([_entry("AAA.AS", None)])
    reclassify_rows_after_d(st, D0)
    assert len(st.price_ingest_errors) == 1


def test_k1_d_unresolved_all_nine_stay_in_data_failure():
    syms = [f"S{i}.AS" for i in range(9)]
    st = _with_entries([_entry(s, AFTER) for s in syms])
    st.d_resolution_failure = "brak daty kompletnej"  # reclassify NIE jest wolane (d is None)
    assert st.d is None
    text = render_report(st)
    ing = _section(text, "### ingest_errors (ten przebieg)").split("\n###", 1)[0]
    assert sum(1 for s in syms if f"yahoo {s} data=2026-10-16 row_missing_price" in ing) == 9
    assert has_data_failures(st) is True
    assert "  - brak" in _section(text, "## 🟢 HEARTBEAT")


def test_k1_heartbeat_brak_when_empty():
    assert "  - brak" in _section(render_report(_state()), "## 🟢 HEARTBEAT")


# --- B-82 ---------------------------------------------------------------------------


def _sat_state(missing_detail):
    st = ReportState(d=D0, run_started_at=datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc))
    sat = minimal_stage_result(D0)
    sat.report["missing_detail"] = missing_detail
    st.satellite = sat
    return st


def test_b82_ticker_and_date_in_d_niekompletne_as_information():
    st = _sat_state([("ZZZ", "2026-10-02", "brak ceny"), ("AAA", "2026-10-01", "brak ceny"), ("AAA", "2026-10-02", "brak ceny")])
    text = render_report(st)
    sec = _section(text, "### D niekompletne").split("\n###", 1)[0]
    lines = [ln for ln in sec.splitlines() if ln.startswith("- informacja")]
    assert lines == [
        "- informacja: NAV satelity 2026-10-01: AAA · brak ceny",
        "- informacja: NAV satelity 2026-10-02: AAA · brak ceny",
        "- informacja: NAV satelity 2026-10-02: ZZZ · brak ceny",
    ]
    assert "brak\n" not in sec.replace("brak ceny", "")  # informacja zastepuje "brak"
    assert has_data_failures(st) is False
    assert st.exit_code == 0


def test_b82_no_missing_detail_keeps_brak():
    sec = _section(render_report(_sat_state([])), "### D niekompletne").split("\n###", 1)[0]
    assert sec.strip().splitlines()[-1] == "brak"


def test_b82_missing_detail_key_absent_is_backward_compatible():
    st = _sat_state([])
    del st.satellite.report["missing_detail"]
    assert "NAV satelity" not in render_report(st)

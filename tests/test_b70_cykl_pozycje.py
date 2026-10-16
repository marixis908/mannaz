"""B-70 / B70.2 / B-73 / B-74 / B-80 / B-81 (brief CC-B70, C6): testy czyste (bez
bazy i bez sieci). Czesci wymagajace bazy (T1, `run_cycle` z `risk_daily`,
pozycje miedzy ocenami, stop_rule NULL) sa w `test_b70_cykl_pozycje_db.py`.

Dane wylacznie syntetyczne: rachunki "<TYP> SYNTH-B70" (bez numerow), daty 1990
(poza T3, gdzie zakres kotwic [S1, D] wymusza daty 2023+), tickery AAA...
Kwot ani ilosci wlascicielskich nie ma."""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from _satstub import minimal_stage_result
from mannaz import cycle as cycle_mod
from mannaz import satellite as sat_mod
from mannaz.cycle import (
    STOP_ORDER_HIGH_MESSAGE,
    PROVIDER_MESSAGE_MAX_CHARS,
    PositionChange,
    PositionSnapshotRow,
    ReportState,
    _call_capturing_provider_messages,
    _provider_messages_line,
    _snapshot_from_open_positions,
    build_stop_order_rows,
    diff_positions,
    has_data_failures,
    render_report,
    stop_order_decision,
)
from mannaz.prices import ProviderMessage, capture_provider_messages
from mannaz.risk import STOP_RULE_VERSION, IncompleteRiskItem
from mannaz.satellite import (
    S1,
    AccountValue,
    AccountValuesError,
    load_account_values_dir,
    run_satellite_stage,
)

ACC_AKCYJNY = "AKCYJNY SYNTH-B70"
ACC_ZAGR = "ZAGRANICZNY SYNTH-B70"


def _state(d: date = date(1990, 3, 9)) -> ReportState:
    state = ReportState(d=d, run_started_at=datetime(1990, 3, 10, 8, 0, tzinfo=timezone.utc))
    state.heartbeat_total = 1
    state.heartbeat_covered = 1
    return state


def _section(text: str, header: str) -> str:
    """Tresc sekcji `header` (np. "## Zmiany pozycji") do nastepnego naglowka `## `."""
    assert header in text, f"brak sekcji {header!r} w raporcie"
    tail = text.split(header, 1)[1]
    return tail.split("\n## ", 1)[0]


# ===========================================================================
# T2 (B70.2) — brak ceny pozycji otwartej na D -> ticker w "D niekompletne"
# (zachowanie dzis: risk.IncompleteRiskDateError -> state.date_incomplete_items
#  -> _render_data_failure_section). Odpowiednik db na run_risk z brakiem ceny:
#  tests/test_risk_pit_db.py::test_u5_open_market_gap_on_real_data_raises_and_writes_nothing
#  oraz (run_cycle) test_b70_cykl_pozycje_db.py::test_t2_*.
# ===========================================================================


def test_t2_missing_price_item_listed_in_d_niekompletne_section():
    state = _state()
    state.date_incomplete_items = [
        IncompleteRiskItem("AAA", "AAA.WA", "GPW", "market_open_no_price"),
        IncompleteRiskItem("BBB", None, None, "base_instrument_not_found"),
    ]
    assert has_data_failures(state)
    text = render_report(state)
    sec = _section(text, "### D niekompletne").split("\n### ", 1)[0]
    assert "- AAA · AAA.WA · GPW · market_open_no_price" in sec
    assert "- BBB · brak · brak · base_instrument_not_found" in sec
    assert not sec.strip().startswith("brak")  # lista, nie "brak"
    assert "DATA FAILURE" in text


def test_t2_state_shape_produced_by_run_cycle_lists_ticker_somewhere_in_report():
    """Ksztalt stanu, jaki robi `run_cycle` po `IncompleteRiskDateError`: `date_incomplete_items` wypelnione ORAZ
    `STAGE_RISK` w `stage_not_executed`. Ticker brakujacej ceny ma byc w raporcie (brief B70.2).
    UWAGA: test jest CZERWONY na kodzie z C1-C5 (blad w kodzie, nie w tescie) — patrz raport C6."""
    state = _state()
    state.date_incomplete_items = [IncompleteRiskItem("B70AAA", "B70AAA.WA", "GPW", "market_open_no_price")]
    state.stage_not_executed.add(cycle_mod.STAGE_RISK)
    text = render_report(state)
    assert "B70AAA" in text
    assert "market_open_no_price" in _section(text, "### D niekompletne").split("\n### ", 1)[0]


def test_t2_control_without_items_section_says_brak():
    state = _state()
    sec = _section(render_report(state), "### D niekompletne").split("\n### ", 1)[0]
    assert sec.strip().startswith("brak")
    assert not has_data_failures(state)


# ===========================================================================
# T3 (B-73) — katalog kotwic M78
# ===========================================================================

_HDR = "date,account,currency,positions_value,cash_total\n"


def _w(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_text(_HDR + body, encoding="utf-8")
    return p


def test_t3_two_files_merged_into_one_sorted_list(tmp_path):
    # plik 2024-02-02 zapisany PIERWSZY, rachunki w pliku w odwrotnej kolejnosci:
    # wynik ma byc posortowany po (data, rachunek, waluta), nie po kolejnosci zapisu.
    _w(tmp_path, "account_values_2024-02-02.csv", f"2024-02-02,{ACC_ZAGR},EUR,10,5\n2024-02-02,{ACC_AKCYJNY},PLN,20,6\n")
    _w(tmp_path, "account_values_2024-01-05.csv", f"2024-01-05,{ACC_AKCYJNY},PLN,30,7\n")
    out = load_account_values_dir(tmp_path)
    assert [(a.date.isoformat(), a.account, a.currency) for a in out] == [
        ("2024-01-05", "AKCYJNY", "PLN"),
        ("2024-02-02", "AKCYJNY", "PLN"),
        ("2024-02-02", "ZAGRANICZNY", "EUR"),
    ]
    assert all(isinstance(a, AccountValue) for a in out)


def test_t3_files_not_matching_exact_pattern_are_ignored(tmp_path):
    _w(tmp_path, "account_values_2024-01-05.csv", f"2024-01-05,{ACC_AKCYJNY},PLN,1,1\n")
    # kontrolka: te pliki MAJA bledne dane (gdyby byly czytane -> blad), ale nie pasuja do wzorca nazwy
    _w(tmp_path, "account_values_2024-1-5.csv", f"1999-01-01,{ACC_AKCYJNY},PLN,1,1\n")
    _w(tmp_path, "notes.csv", "zle,dane\n")
    _w(tmp_path, "account_values_2024-01-06.csv.bak", f"1999-01-01,{ACC_AKCYJNY},PLN,1,1\n")
    (tmp_path / "account_values_2024-01-07.csv").mkdir()  # katalog o pasujacej nazwie nie jest plikiem
    out = load_account_values_dir(tmp_path)
    assert [a.date.isoformat() for a in out] == ["2024-01-05"]


def test_t3_row_date_differs_from_file_name_raises_with_file_name(tmp_path):
    _w(tmp_path, "account_values_2024-01-05.csv", f"2024-01-05,{ACC_AKCYJNY},PLN,1,1\n")
    _w(tmp_path, "account_values_2024-01-12.csv", f"2024-01-11,{ACC_AKCYJNY},PLN,1,1\n")
    with pytest.raises(AccountValuesError) as ei:
        load_account_values_dir(tmp_path)
    msg = str(ei.value)
    assert msg.startswith("account_values_2024-01-12.csv")
    assert "2024-01-11" in msg and "2024-01-12" in msg


def test_t3_duplicate_key_in_one_file_raises_with_file_name(tmp_path):
    _w(
        tmp_path,
        "account_values_2024-01-05.csv",
        f"2024-01-05,{ACC_AKCYJNY},PLN,1,1\n2024-01-05,{ACC_AKCYJNY} INNY,PLN,2,2\n",  # ta sama grupa AKCYJNY+PLN
    )
    with pytest.raises(AccountValuesError) as ei:
        load_account_values_dir(tmp_path)
    msg = str(ei.value)
    assert msg.startswith("account_values_2024-01-05.csv")
    assert "duplikat" in msg and "w tym samym pliku" in msg


def test_t3_duplicate_key_between_files_is_blocked_by_date_equals_name_rule(tmp_path):
    """Duplikat klucza (data, rachunek, waluta) MIEDZY plikami: klucz zawiera date, a data wiersza
    musi rownac sie dacie z nazwy pliku, wiec dwa rozne pliki nie moga niezaleznie dzielic klucza.
    Proba (wiersz z data pliku A w pliku B) konczy sie bledem DATY (nazwa pliku B), nie duplikatu."""
    _w(tmp_path, "account_values_2024-01-05.csv", f"2024-01-05,{ACC_AKCYJNY},PLN,1,1\n")
    _w(tmp_path, "account_values_2024-01-12.csv", f"2024-01-05,{ACC_AKCYJNY},PLN,1,1\n")
    with pytest.raises(AccountValuesError) as ei:
        load_account_values_dir(tmp_path)
    assert str(ei.value).startswith("account_values_2024-01-12.csv")
    assert "duplikat" not in str(ei.value)


def test_t3_unparsable_file_raises_with_file_name(tmp_path):
    _w(tmp_path, "account_values_2024-01-05.csv", f"nie-data,{ACC_AKCYJNY},PLN,1,1\n")
    with pytest.raises(AccountValuesError) as ei:
        load_account_values_dir(tmp_path)
    assert str(ei.value).startswith("account_values_2024-01-05.csv")


def test_t3_empty_and_missing_directory_give_empty_list(tmp_path):
    empty = tmp_path / "pusty"
    empty.mkdir()
    assert load_account_values_dir(empty) == []
    assert load_account_values_dir(tmp_path / "nie_ma_takiego") == []
    assert load_account_values_dir(str(tmp_path / "nie_ma_takiego")) == []  # str tez
    # kontrolka dodatnia tego samego typu wejscia: katalog z plikiem NIE jest pusty
    _w(empty, "account_values_2024-01-05.csv", f"2024-01-05,{ACC_AKCYJNY},PLN,1,1\n")
    assert len(load_account_values_dir(empty)) == 1


class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def cursor(self):
        return _FakeCur()


def _stage_with_fake_compute(monkeypatch, snapshots, d=date(2024, 6, 30)):
    seen = {}

    def fake_compute(cur, d_from, d_to, snaps):
        seen["args"] = (d_from, d_to, list(snaps))
        report = {
            "data_verified": False,
            "data_unverified_reasons": [],
            "alerts": {},
            "violation_registry": [],
            "registry2": {"status": "przed T42"},
        }
        return report, {}

    monkeypatch.setattr(sat_mod, "compute_satellite", fake_compute)
    monkeypatch.setattr(
        sat_mod, "persist_mandate_registry", lambda *a, **k: {"registry": a[1], "write": False}
    )
    res = run_satellite_stage(_FakeConn(), d, out_dir=None, write=False, code_sha_value="x", snapshots=snapshots)
    return res, seen["args"]


def _av(day: date, acc="AKCYJNY", ccy="PLN") -> AccountValue:
    return AccountValue(date=day, account=acc, currency=ccy, positions_value=1.0, cash_total=1.0)


def test_t3_run_satellite_stage_splits_anchors_in_and_out_of_range(monkeypatch):
    d = date(2024, 6, 30)
    snaps = [
        _av(date(2023, 10, 11)),  # dzien przed S1 -> poza
        _av(S1),  # dokladnie S1 -> w zakresie
        _av(date(2024, 3, 1)),
        _av(date(2024, 6, 30), "ZAGRANICZNY", "EUR"),  # dokladnie D -> w zakresie
        _av(date(2024, 7, 1)),  # dzien po D -> poza
        _av(date(2024, 7, 1), "ZAGRANICZNY", "EUR"),  # ta sama data poza zakresem, drugi wiersz -> jedna data
    ]
    res, (d_from, d_to, passed) = _stage_with_fake_compute(monkeypatch, snaps, d)
    assert (d_from, d_to) == (S1, d)
    assert [a.date for a in passed] == [S1, date(2024, 3, 1), d]  # tylko w zakresie, kolejnosc zachowana
    assert res.report["anchors_out_of_range"] == ["2023-10-11", "2024-07-01"]  # posortowane, bez powtorzen


def test_t3_run_satellite_stage_without_anchors_has_no_out_of_range_key_and_passes_empty(monkeypatch):
    res, (_, _, passed) = _stage_with_fake_compute(monkeypatch, ())
    assert passed == []  # puste -> compute_satellite doda "brak bramki M78 (nie podano account_values)"
    assert "anchors_out_of_range" not in res.report
    res2, _ = _stage_with_fake_compute(monkeypatch, [_av(date(2024, 3, 1))])
    assert "anchors_out_of_range" not in res2.report  # kontrolka: kotwica w zakresie nie daje klucza


def test_t3_compute_satellite_message_for_no_anchors_is_unchanged():
    """Dzisiejszy tekst przyczyny, gdy brak plikow: stala w kodzie compute_satellite (bez bazy nie da sie
    wywolac calej funkcji, wiec sprawdzamy zrodlo)."""
    src = Path(sat_mod.__file__).read_text(encoding="utf-8")
    assert 'reasons.append("brak bramki M78 (nie podano account_values)")' in src
    assert "gate = m78_gate(list(snapshots), daily.navs) if snapshots else None" in src


def _satellite_state(report_patch: dict) -> ReportState:
    state = _state(date(2024, 6, 30))
    stage = minimal_stage_result(date(2024, 6, 30))
    stage.report.update(report_patch)
    state.satellite = stage
    return state


def test_t3_render_out_of_range_anchor_line():
    text = render_report(_satellite_state({"anchors_out_of_range": ["2023-10-11", "2024-07-01"]}))
    sec = _section(text, "## Satelita (§21.6)")
    assert "- kotwica poza zakresem: 2023-10-11" in sec
    assert "- kotwica poza zakresem: 2024-07-01" in sec


def test_t3_render_without_anchors_has_no_anchor_lines_and_keeps_reason():
    text = render_report(_satellite_state({"data_unverified_reasons": ["brak bramki M78 (nie podano account_values)"]}))
    sec = _section(text, "## Satelita (§21.6)")
    assert "  - brak bramki M78 (nie podano account_values)" in sec
    assert "kotwica" not in sec


def _gate_row(day, comp, status, rel=None, acc="AKCYJNY", ccy="PLN"):
    return {
        "date": day, "account": acc, "currency": ccy, "component": comp,
        "broker": 123456.78, "ours": 123450.11, "diff": -6.67, "diff_pln": -6.67,  # kwoty NIE moga trafic do raportu
        "rel_diff": rel, "status": status,
    }


def test_t3_render_anchor_statuses_without_amounts():
    gate = {
        "ok": False, "result": "rozjazd", "n": 6, "n_zgodny": 1, "tolerance": 0.0001, "missing_components": [],
        "rows": [
            _gate_row("2024-03-01", "papiery", "zgodny", 0.00001),
            _gate_row("2024-03-01", "srodki", "niezgodny", 0.0012),
            _gate_row("2024-03-01", "papiery", "brak danych", None, "ZAGRANICZNY", "EUR"),
            _gate_row("2024-03-01", "srodki", "niepelny", None, "ZAGRANICZNY", "EUR"),
            _gate_row("2024-04-05", "papiery", "niezgodny", None),  # rel_diff None (broker == 0)
        ],
    }
    text = render_report(_satellite_state({"gate": gate}))
    sec = _section(text, "## Satelita (§21.6)")
    assert "- kotwica 2024-03-01:" in sec
    assert "- kotwica 2024-04-05:" in sec
    assert "  - AKCYJNY/PLN papiery: zgodny" in sec
    assert "  - AKCYJNY/PLN srodki: niezgodny (0.1200%)" in sec
    assert "  - ZAGRANICZNY/EUR papiery: brak" in sec
    assert "  - ZAGRANICZNY/EUR srodki: niepelny" in sec
    assert "  - AKCYJNY/PLN papiery: niezgodny\n" in sec  # bez rel_diff -> sam status, bez "(None%)"
    assert "None%" not in sec and "(None" not in sec.split("Rejestr 1")[0]
    # zero kwot: ani broker, ani ours, ani diff
    for amount in ("123456", "123450", "6.67", "123,456"):
        assert amount not in sec


def test_t3_render_anchor_dates_in_order_and_out_of_range_after_gate_rows():
    gate = {"rows": [_gate_row("2024-04-05", "papiery", "zgodny", 0.0), _gate_row("2024-03-01", "papiery", "zgodny", 0.0)]}
    text = render_report(_satellite_state({"gate": gate, "anchors_out_of_range": ["2023-01-01"]}))
    sec = _section(text, "## Satelita (§21.6)")
    assert sec.index("kotwica 2024-03-01") < sec.index("kotwica 2024-04-05") < sec.index("kotwica poza zakresem: 2023-01-01")


# ===========================================================================
# T4 (B-74) — sekcja "Zmiany pozycji" wzgledem poprzedniej oceny
# ===========================================================================


def _pc(ticker, rodzaj, rachunek=ACC_AKCYJNY):
    return PositionChange(broker_ticker=ticker, rachunek=rachunek, rodzaj=rodzaj)


def test_t4_render_changes_new_closed_up_down_with_first_line_and_between():
    state = _state()
    state.position_changes_computed = True
    state.position_changes_prev_date = date(1990, 3, 5)
    state.position_changes = [
        _pc("AAA", "nowa"),
        _pc("BBB", "zamknięta"),
        _pc("CCC", "zmiana ilości ↑"),
        _pc("DDD", "zmiana ilości ↓", ACC_ZAGR),
    ]
    state.position_changes_between = ["EEE", "FFF"]
    sec = _section(render_report(state), "## Zmiany pozycji")
    lines = [ln for ln in sec.split("\n") if ln.strip()]
    assert lines[0] == "względem poprzedniej oceny (1990-03-05)"
    assert "- AAA (AKCYJNY): nowa" in lines
    assert "- BBB (AKCYJNY): zamknięta" in lines
    assert "- CCC (AKCYJNY): zmiana ilości ↑" in lines
    assert "- DDD (ZAGRANICZNY): zmiana ilości ↓" in lines
    assert "otwarte i zamknięte między ocenami: EEE, FFF" in lines
    assert "SYNTH" not in sec and "B70" not in sec  # rachunek tylko jako typ


def test_t4_render_no_changes_and_no_between_says_brak_twice():
    state = _state()
    state.position_changes_computed = True
    state.position_changes_prev_date = date(1990, 3, 5)
    sec = _section(render_report(state), "## Zmiany pozycji")
    lines = [ln for ln in sec.split("\n") if ln.strip()]
    assert lines[0] == "względem poprzedniej oceny (1990-03-05)"
    assert lines[1] == "brak"
    assert lines[2] == "otwarte i zamknięte między ocenami: brak"


def test_t4_render_no_previous_evaluation():
    state = _state()
    state.position_changes_computed = True
    state.position_changes_prev_date = None
    sec = _section(render_report(state), "## Zmiany pozycji")
    assert "brak poprzedniej oceny" in sec
    assert "względem poprzedniej oceny" not in sec
    assert "między ocenami" not in sec


def test_t4_render_section_not_computed():
    state = _state()  # position_changes_computed domyslnie False
    sec = _section(render_report(state), "## Zmiany pozycji")
    assert "nie wykonano (cykl zatrzymany przed ustaleniem D)" in sec
    # nawet gdy lista zmian jest niepusta, niepoliczona sekcja jej nie pokazuje
    state.position_changes = [_pc("AAA", "nowa")]
    sec2 = _section(render_report(state), "## Zmiany pozycji")
    assert "AAA" not in sec2


def _op(ticker, instrument_id, qty, rachunek=ACC_AKCYJNY, ccy="PLN"):
    """Wiersz w ksztalcie `risk.open_positions_as_of` (tylko pola uzywane przez cykl)."""
    return {
        "rachunek": rachunek, "broker_ticker": ticker, "instrument_id": instrument_id,
        "settlement_currency": ccy, "qty": Decimal(qty),
    }


def test_t4_diff_new_closed_up_down_unchanged_and_short_by_magnitude():
    before = _snapshot_from_open_positions(
        [
            _op("BBB", 2, "5"),  # zamknieta
            _op("CCC", 3, "7"),  # ↑
            _op("AAA0", 9, "10"),  # bez zmiany
            _op("SSU", 5, "-8", ACC_ZAGR),  # short: |8| -> |5| = ↓
            _op("SSD", 6, "-3", ACC_ZAGR),  # short: |3| -> |9| = ↑
        ]
    )
    after = _snapshot_from_open_positions(
        [
            _op("AAA", 1, "4"),  # nowa
            _op("CCC", 3, "9"),
            _op("AAA0", 9, "10"),
            _op("SSU", 5, "-5", ACC_ZAGR),
            _op("SSD", 6, "-9", ACC_ZAGR),
        ]
    )
    assert all(isinstance(r, PositionSnapshotRow) for r in before + after)
    got = {(c.broker_ticker, c.rodzaj) for c in diff_positions(before, after)}
    assert got == {
        ("AAA", "nowa"),
        ("BBB", "zamknięta"),
        ("CCC", "zmiana ilości ↑"),
        ("SSU", "zmiana ilości ↓"),
        ("SSD", "zmiana ilości ↑"),
    }
    # kontrolka: short -8 -> -8 nie jest zmiana
    same = _snapshot_from_open_positions([_op("SSX", 7, "-8", ACC_ZAGR)])
    assert diff_positions(same, same) == []


def test_t4_snapshot_key_is_account_instrument_currency():
    r = _snapshot_from_open_positions([_op("AAA", 1, "4", ACC_ZAGR, "EUR")])[0]
    assert cycle_mod._snapshot_key(r) == (ACC_ZAGR, 1, "EUR")


# ===========================================================================
# T5 (B-80) — stop_order_decision / build_stop_order_rows
# ===========================================================================

_ATR = Decimal("20")


def test_t5_rule_version_is_4_19():
    assert STOP_RULE_VERSION == "4.19"


def test_t5_loosening_with_changed_rule_long_is_tak_with_rule_version():
    got = stop_order_decision(
        "long", Decimal("90"), Decimal("100"), _ATR, False, rule_changed=True, rule_change="poluzowanie"
    )
    assert got == f"tak (zmiana reguły rew. {STOP_RULE_VERSION})"
    assert got == "tak (zmiana reguły rew. 4.19)"


def test_t5_loosening_with_changed_rule_short_is_tak_mirror():
    got = stop_order_decision(
        "short", Decimal("110"), Decimal("100"), _ATR, False, rule_changed=True, rule_change="poluzowanie"
    )
    assert got == "tak (zmiana reguły rew. 4.19)"


def test_t5_same_rule_wrong_direction_keeps_ratchet():
    for kind, stop_d in (("long", Decimal("90")), ("short", Decimal("110"))):
        got = stop_order_decision(
            kind, stop_d, Decimal("100"), _ATR, False, rule_changed=False, rule_change="poluzowanie"
        )
        assert got == "nie (zapadka: zostaw poprzedni poziom)"


def test_t5_changed_rule_but_tightening_keeps_todays_ratchet():
    for kind, stop_d in (("long", Decimal("90")), ("short", Decimal("110"))):
        got = stop_order_decision(
            kind, stop_d, Decimal("100"), _ATR, False, rule_changed=True, rule_change="zaostrzenie"
        )
        assert got == "nie (zapadka: zostaw poprzedni poziom)"


@pytest.mark.parametrize("direction", ["0", None])
def test_t5_changed_rule_zero_or_none_direction_keeps_ratchet(direction):
    got = stop_order_decision(
        "long", Decimal("90"), Decimal("100"), _ATR, False, rule_changed=True, rule_change=direction
    )
    assert got == "nie (zapadka: zostaw poprzedni poziom)"


def test_t5_high_state_wins_over_rule_change_loosening():
    for kind, stop_d in (("long", Decimal("90")), ("short", Decimal("110"))):
        got = stop_order_decision(
            kind, stop_d, Decimal("100"), _ATR, True, rule_changed=True, rule_change="poluzowanie"
        )
        assert got == STOP_ORDER_HIGH_MESSAGE
        assert "reguły" not in got


def test_t5_changed_rule_does_not_alter_other_branches():
    # bez zmiany poziomu -> "nie" nawet przy rule_changed + poluzowanie
    assert stop_order_decision("long", Decimal("100"), Decimal("100"), _ATR, False, True, "poluzowanie") == "nie"
    # nowa pozycja
    assert stop_order_decision("long", Decimal("100"), None, _ATR, False, True, "poluzowanie") == "tak (nowa pozycja)"
    # dobra strona: dalej prog ATR22
    assert stop_order_decision("long", Decimal("110"), Decimal("100"), _ATR, False, True, "poluzowanie") == "tak"
    assert (
        stop_order_decision("long", Decimal("101"), Decimal("100"), _ATR, False, True, "poluzowanie")
        == "nie (zmiana < 0,25×ATR22)"
    )


def _item(ticker, kind, stop_d, stop_prev, below=False, *tail, rachunek=ACC_AKCYJNY):
    return (ticker, rachunek, kind, stop_d, "PLN", "PLN", stop_prev, None, _ATR, below, *tail)


def test_t5_build_rows_13th_element_true_with_loosening_gives_new_decision_and_sorts_in_tak_group():
    items = [
        _item("ZZZ", "long", Decimal("90"), Decimal("100"), False, Decimal("95"), False, False),  # nie (zapadka), 13-ty False
        _item("BBB", "long", Decimal("90"), Decimal("100"), False, Decimal("95"), False, True),  # tak (zmiana reguly)
        _item("AAA", "long", Decimal("110"), Decimal("100"), False, None, False, False),  # tak
        _item("HHH", "long", Decimal("90"), Decimal("100"), True, Decimal("95"), False, True),  # HIGH mimo rule_changed
    ]
    rows = build_stop_order_rows(items)
    by = {r.broker_ticker: r for r in rows}
    assert by["BBB"].decision == "tak (zmiana reguły rew. 4.19)"
    assert by["BBB"].rule_change == "poluzowanie"
    assert by["ZZZ"].decision == "nie (zapadka: zostaw poprzedni poziom)"
    assert by["HHH"].decision == STOP_ORDER_HIGH_MESSAGE
    # sortowanie: HIGH, potem grupa "tak" (rachunek, ticker): AAA, BBB; na koncu "nie"
    assert [r.broker_ticker for r in rows] == ["HHH", "AAA", "BBB", "ZZZ"]


def test_t5_build_rows_short_loosening_mirror():
    rows = build_stop_order_rows([_item("SSS", "short", Decimal("110"), Decimal("100"), False, Decimal("105"), False, True)])
    assert rows[0].rule_change == "poluzowanie"  # short: nowa 110 > stara 105 = poluzowanie
    assert rows[0].decision == "tak (zmiana reguły rew. 4.19)"


def test_t5_build_rows_12_element_tuple_is_todays_behaviour():
    item12 = _item("AAA", "long", Decimal("90"), Decimal("100"), False, Decimal("95"), False)
    assert len(item12) == 12
    row = build_stop_order_rows([item12])[0]
    assert row.decision == "nie (zapadka: zostaw poprzedni poziom)"
    item10 = _item("AAA", "long", Decimal("90"), Decimal("100"), False)
    assert len(item10) == 10
    assert build_stop_order_rows([item10])[0].decision == "nie (zapadka: zostaw poprzedni poziom)"


def test_t5_build_rows_13th_element_true_but_tightening_stays_ratchet():
    row = build_stop_order_rows(
        [_item("AAA", "long", Decimal("90"), Decimal("100"), False, Decimal("80"), False, True)]  # nowa 90 > stara 80
    )[0]
    assert row.rule_change == "zaostrzenie"
    assert row.decision == "nie (zapadka: zostaw poprzedni poziom)"


# ===========================================================================
# T6 (B-81) — capture_provider_messages + HEARTBEAT
# ===========================================================================

_MSG = "$SHO.WA: Data doesn't exist for startDate = 1, endDate = 2"
_MSG_TEXT = "Data doesn't exist for startDate = 1, endDate = 2"


@pytest.fixture
def yf_logger():
    lg = logging.getLogger("yfinance")
    saved = (lg.propagate, lg.level, list(lg.handlers))
    try:
        yield lg
    finally:
        lg.propagate, lg.level = saved[0], saved[1]
        lg.handlers[:] = saved[2]


def _logger_state(lg):
    return (lg.propagate, lg.level, list(lg.handlers))


def test_t6_logger_error_is_captured_without_prefix(yf_logger):
    with capture_provider_messages() as cap:
        yf_logger.error(_MSG)
    assert cap.messages == [ProviderMessage("SHO.WA", _MSG_TEXT)]


def test_t6_stderr_line_is_captured_too_and_not_leaked(yf_logger, capsys):
    with capture_provider_messages() as cap:
        sys.stderr.write("$ABC.WA: possibly delisted; no price data found\n")
        sys.stderr.write("linia bez symbolu\n")
        sys.stderr.write("$NOEOL.WA: ostatnia linia bez konca")  # niepelna ostatnia linia
    assert cap.messages == [
        ProviderMessage("ABC.WA", "possibly delisted; no price data found"),
        ProviderMessage("?", "linia bez symbolu"),
        ProviderMessage("NOEOL.WA", "ostatnia linia bez konca"),
    ]
    assert capsys.readouterr().err == ""  # przechwycone, nie wypisane


def test_t6_order_is_occurrence_order_and_multiline_logger_message_is_split(yf_logger):
    with capture_provider_messages() as cap:
        yf_logger.error("$AAA.WA: pierwszy")
        sys.stderr.write("$BBB.WA: drugi\n")
        yf_logger.warning("$CCC.WA: trzeci\n\n$DDD.WA: czwarty")
    assert [(m.symbol, m.text) for m in cap.messages] == [
        ("AAA.WA", "pierwszy"), ("BBB.WA", "drugi"), ("CCC.WA", "trzeci"), ("DDD.WA", "czwarty"),
    ]


def test_t6_below_warning_is_ignored_control(yf_logger):
    with capture_provider_messages() as cap:
        yf_logger.info("$INFO.WA: nie powinno trafic")
        yf_logger.debug("$DBG.WA: nie powinno trafic")
        yf_logger.error("$ERR.WA: powinno trafic")  # kontrolka dodatnia w tym samym bloku
    assert [m.symbol for m in cap.messages] == ["ERR.WA"]


@pytest.mark.parametrize("propagate", [True, False])
@pytest.mark.parametrize("level", [logging.NOTSET, logging.INFO, logging.CRITICAL])
def test_t6_logger_state_restored_after_block(yf_logger, propagate, level):
    yf_logger.propagate = propagate
    yf_logger.setLevel(level)
    sentinel = logging.NullHandler()
    yf_logger.addHandler(sentinel)
    before = _logger_state(yf_logger)
    with capture_provider_messages() as cap:
        # w srodku blok zmienia stan (handler dodany, propagate wylaczone) — kontrolka, ze test widzi zmiane
        assert yf_logger.propagate is False
        assert len(yf_logger.handlers) == len(before[2]) + 1
        yf_logger.error(_MSG)
    assert len(cap.messages) == 1
    assert _logger_state(yf_logger) == before


def test_t6_logger_state_restored_when_block_raises(yf_logger):
    yf_logger.propagate = True
    yf_logger.setLevel(logging.ERROR)
    before = _logger_state(yf_logger)
    stderr_before = sys.stderr
    cap = capture_provider_messages()
    with pytest.raises(RuntimeError, match="boom"):
        with cap:
            yf_logger.error(_MSG)
            sys.stderr.write("$X.WA: przed wyjatkiem\n")
            raise RuntimeError("boom")
    assert _logger_state(yf_logger) == before
    assert sys.stderr is stderr_before
    assert [m.symbol for m in cap.messages] == ["SHO.WA", "X.WA"]  # komunikaty sprzed wyjatku zachowane


def test_t6_call_capturing_keeps_messages_when_stage_raises(yf_logger):
    state = _state()

    def stage(arg):
        yf_logger.error(_MSG)
        raise ValueError(f"etap padl {arg}")

    with pytest.raises(ValueError, match="etap padl 7"):
        _call_capturing_provider_messages(state, stage, 7)
    assert state.provider_messages == [ProviderMessage("SHO.WA", _MSG_TEXT)]


def test_t6_call_capturing_returns_value_and_extends_in_stage_order(yf_logger):
    state = _state()

    def stage_a():
        yf_logger.error("$AAA.WA: a")
        return "A"

    def stage_b(x, y):
        yf_logger.error("$BBB.WA: b")
        return x + y

    assert _call_capturing_provider_messages(state, stage_a) == "A"
    assert _call_capturing_provider_messages(state, stage_b, 1, 2) == 3
    assert [m.symbol for m in state.provider_messages] == ["AAA.WA", "BBB.WA"]


def test_t6_heartbeat_line_one_message():
    state = _state()
    state.provider_messages = [ProviderMessage("SHO.WA", _MSG_TEXT)]
    sec = _section(render_report(state), "## 🟢 HEARTBEAT")
    assert f"- komunikaty dostawcy: 1 (SHO.WA: {_MSG_TEXT})" in sec


def test_t6_heartbeat_line_zero_messages():
    sec = _section(render_report(_state()), "## 🟢 HEARTBEAT")
    assert "- komunikaty dostawcy: 0\n" in sec + "\n"
    assert "komunikaty dostawcy: 0 (" not in sec


def test_t6_heartbeat_truncates_message_text_to_120_chars():
    assert PROVIDER_MESSAGE_MAX_CHARS == 120
    exactly = "x" * 120
    over = "y" * 121
    way_over = "z" * 300
    line = _provider_messages_line([ProviderMessage("A.WA", exactly)])
    assert line == f"- komunikaty dostawcy: 1 (A.WA: {exactly})"  # 120 -> bez zmian
    line2 = _provider_messages_line([ProviderMessage("B.WA", over)])
    assert line2 == f"- komunikaty dostawcy: 1 (B.WA: {'y' * 117}...)"
    line3 = _provider_messages_line([ProviderMessage("C.WA", way_over), ProviderMessage("D.WA", "krotki")])
    assert line3 == f"- komunikaty dostawcy: 2 (C.WA: {'z' * 117}...; D.WA: krotki)"
    item_c = line3.split("(", 1)[1].split("; ")[0].split(": ", 1)[1]
    assert len(item_c) == 120


def test_t6_provider_messages_are_not_a_data_failure():
    state = _state()
    state.provider_messages = [ProviderMessage("SHO.WA", _MSG_TEXT)] * 3
    assert not has_data_failures(state)
    text = render_report(state)
    sec = _section(text, "## 🔴 DATA FAILURE")
    assert "SHO.WA" not in sec
    # kontrolka dodatnia: prawdziwy DATA FAILURE na tym samym typie stanu jest wykrywany
    state.import_failures.append("blad testowy")
    assert has_data_failures(state)

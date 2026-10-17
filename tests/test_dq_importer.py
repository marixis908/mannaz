"""DQ (K2 / B-84, B-77, B-78 detektor): importer cen — tolerancja rewizji, ochrona
korekty recznej, wypelniacz vs zapisany wiersz, konflikty w raporcie cyklu.
Testy bez bazy: `run_prices_fetch` na falszywym kursorze (zapytania SELECT/INSERT
rozpoznawane po tresci SQL), helpery bazodanowe podmienione, zero sieci.
Dane syntetyczne (daty 1990)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from mannaz import prices
from mannaz.cycle import ReportState, has_data_failures, render_report
from mannaz.prices import (
    ManualConflict,
    OhlcRow,
    find_window_fillers,
    run_prices_fetch,
)

D = Decimal
D1 = date(1990, 1, 2)
NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
YAHOO = "yahoo_split_adjusted"
MANUAL = "tradingview_manual_b76"
NOTRADE = "tradingview_manual_notrade"


class _Cur:
    def __init__(self, stored):
        self.stored = stored  # lista krotek (date, o, h, l, c, convention, volume)
        self.writes: list[tuple] = []
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        if "INSERT INTO prices_daily" in sql:
            self.writes.append(params)

    def fetchall(self):
        return list(self.stored) if "FROM prices_daily" in self._last else []


class _Conn:
    def __init__(self, stored):
        self.cur = _Cur(stored)

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _row(o, h, l, c, v="1000", day=D1):
    return OhlcRow(
        price_date=day,
        open_split_adj=D(o) if o is not None else None,
        high_split_adj=D(h) if h is not None else None,
        low_split_adj=D(l) if l is not None else None,
        close_split_adj=D(c) if c is not None else None,
        volume_split_adj=D(v),
        adj_close_total_return=D(c) if c is not None else None,
    )


def _stored(o, h, l, c, conv=YAHOO, v="1000", day=D1, raw=None):
    # raw: (o, h, l, c) zapisane *_raw; domyslnie = split_adj (instrument bez zdarzen)
    ohlc = tuple(D(x) if x is not None else None for x in (o, h, l, c))
    raw_vals = ohlc if raw is None else tuple(D(x) if x is not None else None for x in raw)
    return (day, *ohlc, conv, D(v) if v is not None else None, *raw_vals)


def _run(monkeypatch, stored, fetched, split_events=()):
    errors: list[dict] = []
    monkeypatch.setattr(prices, "_ensure_ingest_errors_table", lambda cur: None)
    monkeypatch.setattr(
        prices,
        "_mapped_instruments",
        lambda cur, ids, today=None: [{"id": 1, "yahoo_symbol": "SYN.WA", "currency": "PLN", "exchange": None}],
    )
    monkeypatch.setattr(prices, "_instrument_split_events", lambda cur, iid: list(split_events))
    monkeypatch.setattr(prices, "_log_ingest_error", lambda cur, **kw: errors.append(kw))
    monkeypatch.setattr(prices, "fetch_ohlc", lambda symbol, start, end: list(fetched))
    monkeypatch.setattr(prices, "fetch_currency", lambda symbol: "PLN")
    monkeypatch.setattr(prices, "current_trigger", lambda: "test")
    monkeypatch.setattr(prices, "code_sha", lambda: "sha")
    conn = _Conn(stored)
    summary = run_prices_fetch(conn, instrument_ids=[1], start=D1, end=D1, commit=False, now=NOW)
    return summary, conn.cur, errors


def _revisions(errors):
    return [e for e in errors if e["error_type"] == "provider_revision"]


# --- K2(a): tolerancja rewizji ---------------------------------------------------


def test_k2a_delta_1e7_no_update_no_revision(monkeypatch):
    summary, cur, errors = _run(
        monkeypatch, [_stored("10", "11", "9", "10.5")], [_row("10.000001", "11", "9", "10.5")]
    )
    assert cur.writes == []
    assert _revisions(errors) == []
    assert summary.revisions_total == 0
    assert summary.results[0].rows_unchanged == 1
    assert summary.results[0].rows_inserted == 0


def test_k2a_delta_1e5_revision_and_update(monkeypatch):
    summary, cur, errors = _run(
        monkeypatch, [_stored("10", "11", "9", "10.5")], [_row("10.0001", "11", "9", "10.5")]
    )
    assert len(cur.writes) == 1
    revs = _revisions(errors)
    assert len(revs) == 1 and "col=open_split_adj" in revs[0]["detail"]
    assert "high_split_adj" not in revs[0]["detail"]  # kolumny w tolerancji nie wchodza do detail
    assert summary.revisions_total == 1


def test_k2a_identical_values_same_convention_no_update(monkeypatch):
    # regresja KTY.WA: wczesniej UPSERT przy identycznych wartosciach nadpisywal konwencje/fetched_at
    summary, cur, errors = _run(monkeypatch, [_stored("10", "11", "9", "10.5")], [_row("10", "11", "9", "10.5")])
    assert cur.writes == []
    assert errors == []
    assert summary.results[0].rows_unchanged == 1


def test_k2a_other_convention_within_tolerance_update_without_revision(monkeypatch):
    # zapisana konwencja inna niz wyliczona (instrument bez zdarzen -> yahoo_split_adjusted)
    summary, cur, errors = _run(
        monkeypatch,
        [_stored("10", "11", "9", "10.5", conv="raw_reconstructed_from_corporate_events")],
        [_row("10", "11", "9", "10.5")],
    )
    assert len(cur.writes) == 1
    assert cur.writes[0][-1] == YAHOO
    assert _revisions(errors) == []


def test_k2a_same_convention_new_split_event_updates_raw(monkeypatch):
    # Codex R1 M1: kolejny split dopisany do corporate_events przy tej samej konwencji
    # raw_reconstructed... -> zapisane raw (ze starym zestawem zdarzen) nieaktualne -> UPDATE raw
    conv = "raw_reconstructed_from_corporate_events"
    old_events = [(date(1989, 1, 1), D("2"))]  # przed sesja D1 -> raw = split_adj
    new_events = old_events + [(D1 + timedelta(days=1), D("4"))]  # po sesji D1 -> raw x4
    stored = [_stored("10", "11", "9", "10.5", conv=conv)]  # raw = split_adj (stary zestaw)
    summary, cur, errors = _run(monkeypatch, stored, [_row("10", "11", "9", "10.5")], new_events)
    assert len(cur.writes) == 1
    assert _revisions(errors) == []
    assert summary.results[0].rows_unchanged == 0
    # kontrolka: ten sam zestaw zdarzen co przy zapisie -> bez UPDATE
    summary, cur, errors = _run(monkeypatch, stored, [_row("10", "11", "9", "10.5")], old_events)
    assert cur.writes == []
    assert summary.results[0].rows_unchanged == 1


def test_k2a_stored_null_raw_updates(monkeypatch):
    stored = [_stored("10", "11", "9", "10.5", raw=("10", "11", "9", None))]
    summary, cur, errors = _run(monkeypatch, stored, [_row("10", "11", "9", "10.5")])
    assert len(cur.writes) == 1
    assert _revisions(errors) == []


def test_k2a_stored_null_column_update_without_revision(monkeypatch):
    stored = [(D1, D("10"), D("11"), D("9"), None, YAHOO, D("1000"), D("10"), D("11"), D("9"), None)]
    summary, cur, errors = _run(monkeypatch, stored, [_row("10", "11", "9", "10.5")])
    assert len(cur.writes) == 1
    assert _revisions(errors) == []


def test_k2a_volume_ignored_in_comparison(monkeypatch):
    summary, cur, errors = _run(
        monkeypatch, [_stored("10", "11", "9", "10.5", v="1000")], [_row("10", "11", "9", "10.5", v="2500")]
    )
    assert cur.writes == [] and errors == []


# --- K2(b): ochrona korekty recznej ----------------------------------------------


def test_k2b_manual_row_close_delta_1pct_conflict_no_write(monkeypatch):
    summary, cur, errors = _run(
        monkeypatch, [_stored("10", "11", "9", "10", conv=MANUAL)], [_row("10.1", "11.1", "9.1", "10.1")]
    )
    assert cur.writes == []
    assert _revisions(errors) == []
    assert len(summary.manual_conflicts) == 1
    c = summary.manual_conflicts[0]
    assert (c.yahoo_symbol, c.price_date, c.stored_close, c.provider_close) == ("SYN.WA", D1, D("10"), D("10.1"))
    assert c.delta_pct == D("1")
    assert summary.results[0].rows_manual_protected == 1
    assert has_data_failures(ReportState(d=D1, run_started_at=NOW, manual_conflicts=[c])) is False


def test_k2b_manual_row_zero_delta_other_convention_no_update_no_revision_no_conflict(monkeypatch):
    # przypadek KTY.WA: konwencja manualna != wyliczona; K2(b) przed K2(a) -> wiersz nietkniety
    summary, cur, errors = _run(
        monkeypatch, [_stored("10", "11", "9", "10", conv=MANUAL)], [_row("10", "11", "9", "10")]
    )
    assert cur.writes == []
    assert errors == []
    assert summary.manual_conflicts == []
    assert summary.results[0].rows_manual_protected == 1


def test_k2b_manual_row_close_delta_01pct_no_conflict_no_write(monkeypatch):
    summary, cur, errors = _run(
        monkeypatch, [_stored("10", "11", "9", "10", conv=MANUAL)], [_row("10.01", "11", "9", "10.01")]
    )
    assert cur.writes == [] and errors == []
    assert summary.manual_conflicts == []


def test_k2b_conflict_threshold_is_strictly_above_half_percent(monkeypatch):
    summary, cur, _ = _run(
        monkeypatch, [_stored("10", "11", "9", "10", conv=MANUAL)], [_row("10", "11", "9", "10.05")]
    )
    assert summary.manual_conflicts == []  # dokladnie 0,5% -> bez konfliktu
    summary, cur, _ = _run(
        monkeypatch, [_stored("10", "11", "9", "10", conv=MANUAL)], [_row("10", "11", "9", "9.94")]
    )
    assert len(summary.manual_conflicts) == 1 and summary.manual_conflicts[0].delta_pct == D("-0.6")


def test_k2b_notrade_row_with_provider_filler_not_written(monkeypatch):
    stored = [_stored("7", "7", "7", "7", conv=NOTRADE, v="0")]
    summary, cur, errors = _run(monkeypatch, stored, [_row("7", "7", "7", "7", v="0")])
    assert cur.writes == []
    assert errors == []
    assert summary.manual_conflicts == []


def test_k2b_notrade_row_with_provider_filler_different_close_no_conflict(monkeypatch):
    # wypelniacz nie jest notowaniem (B-76) -> bez konfliktu, bez zapisu
    stored = [_stored("7", "7", "7", "7", conv=NOTRADE, v="0")]
    summary, cur, errors = _run(monkeypatch, stored, [_row("7.5", "7.5", "7.5", "7.5", v="0")])
    assert cur.writes == []
    assert errors == []
    assert summary.manual_conflicts == []


def test_k2b_b76_manual_row_with_provider_filler_no_conflict(monkeypatch):
    # przypadek K4(ii): korekta TV (sesja z obrotem), Yahoo nadal zwraca wypelniacz z close poprzedniej sesji
    stored = [_stored("149", "150", "145", "149.58", conv="tradingview_manual_b76", v="1189780")]
    summary, cur, errors = _run(monkeypatch, stored, [_row("144", "144", "144", "144", v="0")])
    assert cur.writes == []
    assert errors == []
    assert summary.manual_conflicts == []
    assert summary.results[0].filler_kept == 1


# --- B-77: wypelniacz vs zapisany wiersz ------------------------------------------


def test_b77_stored_flat_with_volume_vs_provider_filler_is_kept(monkeypatch):
    stored = [_stored("7", "7", "7", "7", v="500")]  # plaska legalna sesja (volume > 0)
    summary, cur, errors = _run(monkeypatch, stored, [_row("7", "7", "7", "7", v="0")])
    assert cur.writes == [] and errors == []
    assert summary.results[0].filler_kept == 1 and summary.results[0].filler_identical == 0
    assert summary.filler_kept_total == 1


def test_b77_stored_filler_volume0_vs_provider_filler_is_identical(monkeypatch):
    stored = [_stored("7", "7", "7", "7", v="0")]
    summary, cur, errors = _run(monkeypatch, stored, [_row("7", "7", "7", "7", v="0")])
    assert cur.writes == [] and errors == []
    assert summary.results[0].filler_identical == 1 and summary.results[0].filler_kept == 0
    assert summary.filler_kept_total == 0


def test_b77_stored_null_volume_vs_provider_filler_is_kept(monkeypatch):
    stored = [(D1, D("7"), D("7"), D("7"), D("7"), YAHOO, None, D("7"), D("7"), D("7"), D("7"))]
    summary, cur, _ = _run(monkeypatch, stored, [_row("7", "7", "7", "7", v="0")])
    assert summary.results[0].filler_kept == 1


# --- B-78: detektor pomija notrade, ale data liczy sie jako sesja -----------------


def _sessions(n, end):
    return [end - timedelta(days=i) for i in range(n - 1, -1, -1)]


def _normal(day, symbol="AAA", exchange="XETR"):
    return (symbol, exchange, day, D("10"), D("11"), D("9"), D("10.5"), D("1000"))


def _flat(day, conv=None, symbol="AAA", exchange="XETR"):
    row = (symbol, exchange, day, D("10"), D("10"), D("10"), D("10"), D("0"))
    return row + (conv,) if conv is not None else row


D0 = date(2026, 10, 15)


def test_b78_notrade_row_in_window_not_reported():
    days = _sessions(30, D0)
    rows = [_normal(x) for x in days if x != days[-3]] + [_flat(days[-3], conv=NOTRADE)]
    assert find_window_fillers(rows, D0) == []


def test_b78_unmarked_filler_still_reported_regression_b76():
    days = _sessions(30, D0)
    rows = [_normal(x) for x in days if x != days[-3]] + [_flat(days[-3])]
    assert find_window_fillers(rows, D0) == [("AAA", "XETR", days[-3])]
    rows = [_normal(x) for x in days if x != days[-3]] + [_flat(days[-3], conv=YAHOO)]
    assert find_window_fillers(rows, D0) == [("AAA", "XETR", days[-3])]


def test_b78_notrade_date_counts_as_session_for_window():
    days = _sessions(23, D0)  # days[0] = 23. sesja wstecz
    # sesje: days[1] tylko jako notrade, days[2:] normalne; filler na days[0].
    rows = [_normal(x) for x in days[2:]] + [_flat(days[1], conv=NOTRADE)] + [_flat(days[0])]
    assert find_window_fillers(rows, D0) == []  # days[0] to 23. sesja -> poza oknem
    # kontrolka: bez wiersza notrade (data nie jest sesja) filler wchodzi do okna
    rows_without = [_normal(x) for x in days[2:]] + [_flat(days[0])]
    assert find_window_fillers(rows_without, D0) == [("AAA", "XETR", days[0])]


# --- render: podsekcja konfliktow z korekta reczna ----------------------------------


def _state(**kw):
    return ReportState(d=D0, run_started_at=datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc), **kw)


def test_render_manual_conflicts_subsection_with_row():
    conflict = ManualConflict("AAA.DE", date(2026, 10, 9), D("10.00"), D("10.10"), D("1.00"))
    state = _state(manual_conflicts=[conflict])
    text = render_report(state)
    section = text.split("## Rewizje cen dostawcy", 1)[1].split("\n## ", 1)[0]
    assert "### Konflikty z korektą ręczną" in section
    assert "| ticker | data | zapisane | dostawca | delta |" in section
    assert "| AAA.DE | 2026-10-09 | 10.00 | 10.10 | " in section
    assert has_data_failures(state) is False


def test_render_manual_conflicts_brak_when_revisions_present_but_no_conflicts():
    from mannaz.cycle import ProviderRevision

    rev = ProviderRevision(
        ticker="AAA", instrument_id=1, price_date=date(2026, 10, 9),
        detail="col=close_split_adj old=10 new=11", close_old=D("10"), close_new=D("11"),
    )
    section = render_report(_state(provider_revisions=[rev])).split("## Rewizje cen dostawcy", 1)[1].split("\n## ", 1)[0]
    after = section.split("### Konflikty z korektą ręczną", 1)[1].lstrip("\n").splitlines()[0]
    assert after == "brak"


def test_render_manual_conflicts_only_conflicts_section_listed_and_exit_unchanged():
    conflict = ManualConflict("BBB.WA", date(2026, 10, 8), D("5"), D("5.5"), D("10"))
    assert has_data_failures(_state()) == has_data_failures(_state(manual_conflicts=[conflict]))

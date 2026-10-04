"""B-29 (brief CC-W2, V4+V5): detektor zdarzen korporacyjnych jako etap cyklu
(bramka DATA REVIEW przed FIFO/ryzykiem). Testy db: JEDNA transakcja,
`conn.rollback()` w `finally`, zero trwalych zmian, zero sieci (detektor
wstrzykiwany/stubowany)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from mannaz import corp_actions
from mannaz.corp_actions import (
    CorpActionsSummary,
    CorpEventsClassification,
    DetectedEvent,
    DriftEvent,
    detect_new_corporate_events,
)
from mannaz.cycle import (
    STAGE_CORP_EVENTS,
    STAGE_FIFO,
    STAGE_RISK,
    ReportState,
    has_data_failures,
    run_cycle,
)
from mannaz.db import get_connection
from mannaz.fx import FxSummary
from mannaz.prices import PricesSummary
from mannaz.risk import RiskSummary


def _event(instrument_id: int = 1, ticker: str = "TESTSYM", d: date = date(1990, 3, 5),
           event_type: str = "split", ratio: str = "4") -> DetectedEvent:
    return DetectedEvent(
        broker_ticker=ticker,
        instrument_id=instrument_id,
        event_type=event_type,
        ratio=Decimal(ratio),
        event_date=d,
        date_source="yfinance_splits",
        source="yfinance_splits",
        n_samples=0,
        median_ratio=Decimal(ratio),
    )


class _Spy:
    def __init__(self, rv):
        self.calls = 0
        self.rv = rv

    def __call__(self, *a, **k):
        self.calls += 1
        return self.rv


@pytest.fixture
def db_conn():
    try:
        conn = get_connection()
    except Exception as exc:
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    conn.rollback()
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _cycle_kwargs(tmp_path: Path, conn, detector, risk_spy=None, **over):
    (tmp_path / "incoming").mkdir(exist_ok=True)
    kw = dict(
        conn=conn,
        incoming_dir=tmp_path / "incoming",
        raw_archive_dir=tmp_path / "archive",
        reports_dir=tmp_path / "reports",
        today=date(1990, 3, 10),
        commit=False,
        fetch_prices=_Spy(PricesSummary()),
        fetch_fx=_Spy(FxSummary()),
        risk_fn=risk_spy or _Spy(RiskSummary(risk_date=date(1990, 1, 1))),
        check_ignored=lambda p: True,
        detect_corp_events_fn=detector,
    )
    kw.update(over)
    return kw


def _count_events(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM corporate_events")
        return cur.fetchone()[0]


# --- render / has_data_failures (bez bazy) ---------------------------------


def test_has_data_failures_counts_corp_events_review():
    state = ReportState(d=None, run_started_at=__import__("datetime").datetime.now())
    assert not has_data_failures(state)
    state.corp_events_review = [_event()]
    assert has_data_failures(state)


# --- V5: bramka w cyklu ------------------------------------------------------


@pytest.mark.db
def test_synthetic_split_stops_cycle_with_data_review_before_risk(db_conn, tmp_path):
    conn = db_conn
    try:
        risk_spy = _Spy(RiskSummary(risk_date=date(1990, 1, 1)))
        det = _Spy(CorpEventsClassification(saved=[_event(ticker="TESTSYM", d=date(1990, 3, 5), ratio="4")]))
        result = run_cycle(**_cycle_kwargs(tmp_path, conn, det, risk_spy=risk_spy))

        assert det.calls == 1
        assert len(result.state.corp_events_review) == 1
        assert {STAGE_FIFO, STAGE_RISK} <= result.state.stage_not_executed
        assert risk_spy.calls == 0
        assert result.exit_code == 2
        text = result.report_path.read_text(encoding="utf-8")
        assert "DATA REVIEW — zdarzenia korporacyjne do potwierdzenia" in text
        assert "| TESTSYM | 1990-03-05 | 4 | yfinance_splits/yfinance_splits |" in text
        assert "python -m mannaz.run_p3 corp-actions" in text
    finally:
        conn.rollback()


@pytest.mark.db
def test_no_new_event_does_not_block_and_corporate_events_unchanged(db_conn, tmp_path):
    conn = db_conn
    try:
        before = _count_events(conn)
        det = _Spy(CorpEventsClassification())
        result = run_cycle(**_cycle_kwargs(tmp_path, conn, det))
        assert det.calls == 1
        assert result.state.corp_events_review == []
        assert STAGE_FIFO not in result.state.stage_not_executed
        assert _count_events(conn) == before
    finally:
        conn.rollback()


@pytest.mark.db
def test_detector_exception_is_data_failure_and_fifo_not_executed(db_conn, tmp_path):
    conn = db_conn
    try:
        def boom(c):
            raise RuntimeError("siec niedostepna")

        result = run_cycle(**_cycle_kwargs(tmp_path, conn, boom))
        assert result.exit_code == 2
        assert any("błąd etapu detektora zdarzeń: RuntimeError" in f for f in result.state.import_failures)
        assert STAGE_FIFO in result.state.stage_not_executed
        assert STAGE_RISK in result.state.stage_not_executed
        assert result.state.corp_events_review == []
    finally:
        conn.rollback()


# --- B-45: date_drift w stanie raportu i w cyklu --------------------------------


def _drift(ticker="TESTSYM", stored=date(1990, 2, 1), detected=date(1990, 3, 1)) -> DriftEvent:
    ev = _event(ticker=ticker, d=detected, ratio="2")
    ev.source = "price_ratio_detector"
    return DriftEvent(event=ev, stored_date=stored)


def test_date_drift_is_rendered_and_is_not_a_data_failure():
    from mannaz.cycle import _render_data_failure_section

    state = ReportState(d=None, run_started_at=__import__("datetime").datetime.now())
    state.corp_events_date_drift = [_drift()]
    assert not has_data_failures(state)
    text = "\n".join(_render_data_failure_section(state))
    assert (
        "- informacja (date_drift): TESTSYM split ratio=2: zapisane 1990-02-01, wykryte 1990-03-01, bez zapisu"
        in text
    )


@pytest.mark.db
def test_date_drift_does_not_stop_cycle_and_fills_state(db_conn, tmp_path):
    conn = db_conn
    try:
        det = _Spy(CorpEventsClassification(date_drift=[_drift()]))
        result = run_cycle(**_cycle_kwargs(tmp_path, conn, det))
        assert len(result.state.corp_events_date_drift) == 1
        assert result.state.corp_events_review == []
        assert STAGE_FIFO not in result.state.stage_not_executed
        text = result.report_path.read_text(encoding="utf-8")
        assert "informacja (date_drift): TESTSYM split ratio=2" in text
    finally:
        conn.rollback()


# --- detect_new_corporate_events ----------------------------------------------

_INDEX_SQL_PATH = Path(__file__).resolve().parents[1] / "sql" / "013_corporate_events_klucz_splitu.sql"


def _apply_013_in_tx(conn) -> None:
    """Tresc sql/013 bez BEGIN/COMMIT (test dziala w jednej transakcji z rollbackiem)."""
    body = "\n".join(
        ln for ln in _INDEX_SQL_PATH.read_text(encoding="utf-8").splitlines() if ln.strip() not in ("BEGIN;", "COMMIT;")
    )
    with conn.cursor() as cur:
        cur.execute(body)


def _free_instrument(conn) -> tuple[int, str]:
    """Instrument bez zadnych zdarzen korporacyjnych (regula 2 patrzy na ratio niezaleznie od daty)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, broker_ticker FROM instruments i "
            "WHERE NOT EXISTS (SELECT 1 FROM corporate_events c WHERE c.instrument_id = i.id) ORDER BY id LIMIT 1"
        )
        return cur.fetchone()


def _stored(conn, inst_id, ticker, d, ratio="4", source="price_ratio_detector", title="tytul A (median_ratio=0.2500 n=5)"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO corporate_events (instrument_id, broker_ticker, event_date, event_type,
                                          ratio, title_raw, source, date_source)
            VALUES (%s, %s, %s, 'split', %s, %s, %s, 'inferred_boundary')
            """,
            (inst_id, ticker, d, Decimal(ratio), title, source),
        )


def _stub_collect(monkeypatch, candidates):
    s = CorpActionsSummary()
    s.candidates = list(candidates)
    monkeypatch.setattr(corp_actions, "collect_corp_action_candidates", lambda c: s)


@pytest.mark.db
def test_detect_new_events_filters_existing_key_and_does_not_write(db_conn, monkeypatch):
    """B-45: intencja bez zmian (znany klucz nie jest zgłaszany, baza bez zmian);
    kandydat price_ratio z ta sama data i innym title_raw wychodzi 'known'."""
    conn = db_conn
    try:
        _apply_013_in_tx(conn)
        inst_id, ticker = _free_instrument(conn)
        d_old, d_new = date(1990, 2, 1), date(1990, 2, 2)
        _stored(conn, inst_id, ticker, d_old)
        before = _count_events(conn)

        known_cand = _event(inst_id, ticker, d_old)
        known_cand.source = "price_ratio_detector"
        known_cand.title_raw = "tytul B (median_ratio=0.2490 n=6)"
        _stub_collect(monkeypatch, [known_cand, _event(inst_id, ticker, d_new)])
        result = detect_new_corporate_events(conn)

        assert [(e.instrument_id, e.event_date, e.event_type) for e in result.saved] == [(inst_id, d_new, "split")]
        assert [e.event_date for e in result.known] == [d_old]
        assert _count_events(conn) == before  # zapis w savepoincie wycofany
    finally:
        conn.rollback()


@pytest.mark.db
def test_detect_new_events_empty_when_only_existing(db_conn, monkeypatch):
    conn = db_conn
    try:
        _apply_013_in_tx(conn)
        inst_id, ticker = _free_instrument(conn)
        _stored(conn, inst_id, ticker, date(1990, 2, 1), source="yfinance_splits", title="x")
        _stub_collect(monkeypatch, [_event(inst_id, ticker, date(1990, 2, 1))])
        result = detect_new_corporate_events(conn)
        assert result.saved == [] and len(result.known) == 1
    finally:
        conn.rollback()


@pytest.mark.db
def test_detect_new_events_date_drift_not_in_saved(db_conn, monkeypatch):
    """B-45 test 2: price_ratio inferred_boundary z inna data -> date_drift, nie saved, zapis 0."""
    conn = db_conn
    try:
        _apply_013_in_tx(conn)
        inst_id, ticker = _free_instrument(conn)
        _stored(conn, inst_id, ticker, date(1990, 2, 1), ratio="25", source="yfinance_splits", title="x")
        before = _count_events(conn)
        cand = _event(inst_id, ticker, date(1990, 3, 1), ratio="25")
        cand.source, cand.date_source = "price_ratio_detector", "inferred_boundary"
        _stub_collect(monkeypatch, [cand])
        result = detect_new_corporate_events(conn)
        assert result.saved == []
        assert [(d.stored_date, d.event.event_date) for d in result.date_drift] == [(date(1990, 2, 1), date(1990, 3, 1))]
        assert _count_events(conn) == before
    finally:
        conn.rollback()

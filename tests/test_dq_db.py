"""DQ (testy bazodanowe, marker db; w worktree bez .env pomijane): K2/B-84 na
prawdziwym `prices_daily` (fixture jak test_b76_filler_db.py) oraz K1/B-83 w
`run_cycle`. Jedna transakcja z rollbackiem, daty syntetyczne 1990."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from mannaz.cycle import run_cycle
from mannaz.fx import FxSummary
from mannaz.prices import PricesSummary, _log_ingest_error
from mannaz.risk import RiskSummary
from _satstub import satellite_stub
from test_b76_filler_db import (  # noqa: F401  (fixtury i helpery)
    D1,
    _errors,
    _run,
    _stored,
    _yahoo_row,
    b76_instrument,
)
from test_prices_ingest_safety_db import db_conn  # noqa: F401


def _set_convention(conn, inst, price_date, convention):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE prices_daily SET adjustment_convention = %s WHERE instrument_id = %s AND price_date = %s AND source = 'yahoo'",
            (convention, inst["id"], price_date),
        )


@pytest.mark.db
def test_k2a_delta_below_tolerance_row_untouched_in_db(db_conn, b76_instrument, monkeypatch):  # noqa: F811
    from test_b76_filler_db import _insert_stored

    conn, inst = db_conn, b76_instrument
    try:
        _insert_stored(conn, inst, D1, "10", "11", "9", "10.5", "1000")
        before = _stored(conn, inst, D1)
        summary = _run(conn, inst, monkeypatch, [_yahoo_row(D1, "10.000001", "11", "9", "10.5", "2000")])
        assert _stored(conn, inst, D1) == before  # takze volume, konwencja
        assert _errors(conn, inst, "provider_revision") == []
        assert summary.revisions_total == 0
    finally:
        conn.rollback()


@pytest.mark.db
def test_k2b_manual_row_not_overwritten_conflict_reported(db_conn, b76_instrument, monkeypatch):  # noqa: F811
    from test_b76_filler_db import _insert_stored

    conn, inst = db_conn, b76_instrument
    try:
        _insert_stored(conn, inst, D1, "10", "11", "9", "10", "500")
        _set_convention(conn, inst, D1, "tradingview_manual_b76")
        before = _stored(conn, inst, D1)
        summary = _run(conn, inst, monkeypatch, [_yahoo_row(D1, "10.1", "11.1", "9.1", "10.1", "700")])
        assert _stored(conn, inst, D1) == before
        assert _errors(conn, inst, "provider_revision") == []
        assert len(summary.manual_conflicts) == 1
    finally:
        conn.rollback()


@pytest.mark.db
def test_k1_row_missing_price_after_d_heartbeat_and_before_d_failure(db_conn, tmp_path):  # noqa: F811
    conn = db_conn
    d = date(1990, 3, 6)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM instruments ORDER BY id LIMIT 1")
            iid = cur.fetchone()[0]

        def fetch_prices(c):
            with c.cursor() as cur:
                for day in (date(1990, 3, 7), date(1990, 3, 5)):
                    _log_ingest_error(
                        cur, source="yahoo", instrument_id=iid, price_date=day, error_type="row_missing_price",
                        detail="puste/niepoprawne kolumny: close", run_started_at=datetime.now(timezone.utc),
                    )
            return PricesSummary()

        (tmp_path / "incoming").mkdir(exist_ok=True)
        result = run_cycle(
            conn=conn, incoming_dir=tmp_path / "incoming", raw_archive_dir=tmp_path / "archive",
            reports_dir=tmp_path / "reports", today=date(1990, 3, 10), commit=False,
            fetch_prices=fetch_prices, fetch_fx=lambda *a, **k: FxSummary(),
            risk_fn=lambda *a, **k: RiskSummary(risk_date=d), check_ignored=lambda p: True,
            resolve_date_fn=lambda c, calendar_facts_fn=None: d, satellite_fn=satellite_stub,
        )
        st = result.state
        assert [x for x in st.rows_without_price_after_d] == [(st.rows_without_price_after_d[0][0], date(1990, 3, 7))]
        assert len(st.price_ingest_errors) == 1 and "data=1990-03-05" in st.price_ingest_errors[0]
    finally:
        conn.rollback()

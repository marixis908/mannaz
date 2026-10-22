"""B-91 (testy bazodanowe): `apply_manual_corrections(insert_missing=True)`.
Każdy test w jednej transakcji z rollbackiem (fixture `db_conn`; wzorzec
`tests/test_b76_filler_db.py`). Daty jawne: SESSION = poniedziałek 2026-10-05
(sesja XETR), NON_SESSION = sobota 2026-10-03. Instrument z fixture
`b76_instrument` dostaje w transakcji `exchange = 'XETRA'` (rollback)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mannaz.prices import (
    ManualCorrectionRejectedError,
    apply_manual_corrections,
    run_prices_fetch,
)
from test_b76_filler_db import (  # noqa: F401  (fixtures + helpery)
    NEW,
    _corr,
    _errors,
    _stored,
    _TxnProbe,
    _yahoo_row,
    b76_instrument as _b76_instrument,
)
from test_prices_ingest_safety_db import db_conn  # noqa: F401  (fixture)

SESSION = date(2026, 10, 5)
NON_SESSION = date(2026, 10, 3)


@pytest.fixture
def inst(db_conn, _b76_instrument):  # noqa: F811
    with db_conn.cursor() as cur:
        cur.execute("UPDATE instruments SET exchange = 'XETRA' WHERE id = %s", (_b76_instrument["id"],))
        cur.execute(
            "DELETE FROM prices_daily WHERE instrument_id = %s AND price_date IN (%s, %s)",
            (_b76_instrument["id"], SESSION, NON_SESSION),
        )
        cur.execute("DELETE FROM ingest_errors WHERE instrument_id = %s", (_b76_instrument["id"],))
    return _b76_instrument


def _add_filler_evidence(conn, inst, price_date, source="yahoo"):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingest_errors (source, instrument_id, price_date, error_type, detail,
                                       degraded_state, run_started_at)
            VALUES (%s, %s, %s, 'provider_filler', 'test', FALSE, now())
            """,
            (source, inst["id"], price_date),
        )


def _details(conn, inst):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT detail FROM ingest_errors WHERE instrument_id = %s AND error_type = 'manual_correction'",
            (inst["id"],),
        )
        return [r[0] for r in cur.fetchall()]


def _count_fillers(conn, inst):
    return len(_errors(conn, inst, "provider_filler"))


@pytest.mark.db
def test_1_insert_missing_with_evidence_and_session_inserts_row_and_logs(db_conn, inst):  # noqa: F811
    conn = db_conn
    try:
        _add_filler_evidence(conn, inst, SESSION)
        probe = _TxnProbe(conn)
        s = apply_manual_corrections(probe, [_corr(inst, SESSION)], dry_run=False, insert_missing=True)
        assert s.rejected == [] and s.rows_to_write == 1 and s.rows_written == 1 and s.rows_inserted == 1
        assert probe.commits == 1
        row = _stored(conn, inst, SESSION)
        assert row[:5] == NEW and row[5] == "tradingview_manual_b76" and row[6] == NEW[3]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT currency, volume_raw FROM prices_daily WHERE instrument_id = %s AND price_date = %s"
                " AND source = 'yahoo'",
                (inst["id"], SESSION),
            )
            assert cur.fetchone() == (inst["currency"], None)
        details = _details(conn, inst)
        assert len(details) == 1 and details[0].startswith("B-76 insert:") and "old=brak" in details[0]
    finally:
        conn.rollback()


@pytest.mark.db
def test_2_without_flag_missing_row_rejected_zero_changes(db_conn, inst):  # noqa: F811
    conn = db_conn
    try:
        _add_filler_evidence(conn, inst, SESSION)
        probe = _TxnProbe(conn)
        with pytest.raises(ManualCorrectionRejectedError) as ei:
            apply_manual_corrections(probe, [_corr(inst, SESSION)], dry_run=False)
        assert ei.value.summary.rejected == [(inst["yahoo_symbol"], SESSION, "brak wiersza")]
        assert ei.value.summary.rows_written == 0 and ei.value.summary.rows_inserted == 0
        assert probe.commits == 0
        assert _stored(conn, inst, SESSION) is None
        assert _errors(conn, inst, "manual_correction") == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_3_no_filler_evidence_rejected_whole_transaction(db_conn, inst):  # noqa: F811
    conn = db_conn
    try:
        _add_filler_evidence(conn, inst, SESSION)  # D1 ma dowod, druga data nie
        other = date(2026, 10, 6)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prices_daily WHERE instrument_id = %s AND price_date = %s", (inst["id"], other))
        # dowod od innego zrodla niz yahoo nie liczy sie
        _add_filler_evidence(conn, inst, other, source="tradingview")
        probe = _TxnProbe(conn)
        with pytest.raises(ManualCorrectionRejectedError) as ei:
            apply_manual_corrections(
                probe, [_corr(inst, SESSION), _corr(inst, other)], dry_run=False, insert_missing=True
            )
        assert ei.value.summary.rejected == [(inst["yahoo_symbol"], other, "brak dowodu wypelniacza")]
        assert probe.commits == 0
        assert _stored(conn, inst, SESSION) is None and _stored(conn, inst, other) is None
        assert _errors(conn, inst, "manual_correction") == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_4_date_outside_session_rejected(db_conn, inst):  # noqa: F811
    conn = db_conn
    try:
        _add_filler_evidence(conn, inst, NON_SESSION)
        probe = _TxnProbe(conn)
        with pytest.raises(ManualCorrectionRejectedError) as ei:
            apply_manual_corrections(probe, [_corr(inst, NON_SESSION)], dry_run=False, insert_missing=True)
        assert ei.value.summary.rejected == [(inst["yahoo_symbol"], NON_SESSION, "nie sesja")]
        assert _stored(conn, inst, NON_SESSION) is None
    finally:
        conn.rollback()


@pytest.mark.db
def test_4b_exchange_outside_map_rejected_as_not_session(db_conn, inst):  # noqa: F811
    conn = db_conn
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE instruments SET exchange = NULL WHERE id = %s", (inst["id"],))
        _add_filler_evidence(conn, inst, SESSION)
        s = apply_manual_corrections(_TxnProbe(conn), [_corr(inst, SESSION)], dry_run=True, insert_missing=True)
        assert s.rejected == [(inst["yahoo_symbol"], SESSION, "nie sesja")]
    finally:
        conn.rollback()


@pytest.mark.db
def test_5_dry_run_with_flag_changes_nothing(db_conn, inst):  # noqa: F811
    conn = db_conn
    try:
        _add_filler_evidence(conn, inst, SESSION)
        probe = _TxnProbe(conn)
        s = apply_manual_corrections(probe, [_corr(inst, SESSION)], dry_run=True, insert_missing=True)
        assert s.rejected == [] and s.rows_to_write == 1 and s.rows_written == 0 and s.rows_inserted == 0
        assert probe.commits == 0 and probe.rollbacks >= 1
        assert _stored(conn, inst, SESSION) is None
        assert _errors(conn, inst, "manual_correction") == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_6_importer_with_yahoo_filler_keeps_inserted_row_filler_kept(db_conn, inst, monkeypatch):  # noqa: F811
    conn = db_conn
    try:
        _add_filler_evidence(conn, inst, SESSION)
        apply_manual_corrections(_TxnProbe(conn), [_corr(inst, SESSION)], dry_run=False, insert_missing=True)
        before = _stored(conn, inst, SESSION)
        fillers_before = _count_fillers(conn, inst)
        # wypelniacz Yahoo z innym close niz wstawiony wiersz (8.5) -> filler_kept
        rows = [_yahoo_row(SESSION, "7", "7", "7", "7", "0")]
        monkeypatch.setattr("mannaz.prices.fetch_ohlc", lambda symbol, start, end: rows)
        monkeypatch.setattr("mannaz.prices.fetch_currency", lambda symbol: inst["currency"])
        summary = run_prices_fetch(conn, instrument_ids=[inst["id"]], start=SESSION, end=SESSION, commit=False)
        assert _count_fillers(conn, inst) == fillers_before  # 0 nowych provider_filler
        assert _stored(conn, inst, SESSION) == before
        r = summary.results[0]
        assert r.filler_kept == 1 and r.filler_identical == 0 and r.filler_missing == 0
        assert summary.filler_kept_total == 1
    finally:
        conn.rollback()


@pytest.mark.db
def test_7_notrade_with_insert_missing_uses_notrade_convention_volume_zero(db_conn, inst):  # noqa: F811
    conn = db_conn
    try:
        _add_filler_evidence(conn, inst, SESSION)
        vals = (Decimal("7"), Decimal("7"), Decimal("7"), Decimal("7"), Decimal("0"))
        s = apply_manual_corrections(
            _TxnProbe(conn), [_corr(inst, SESSION, vals)], dry_run=False, notrade=True, insert_missing=True
        )
        assert s.rows_inserted == 1 and s.rejected == []
        row = _stored(conn, inst, SESSION)
        assert row[:5] == vals and row[4] == 0 and row[5] == "tradingview_manual_notrade"
        details = _details(conn, inst)
        assert len(details) == 1 and details[0].startswith("B-78 insert:")
    finally:
        conn.rollback()

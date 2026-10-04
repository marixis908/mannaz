"""B-45 (brief CC-B45, K4): regula tozsamosci zdarzen korporacyjnych
(`classify_against_existing` / `apply_candidates` / indeks sql/013 / --dry-run).
Testy db: jedna transakcja, `conn.rollback()` w `finally`, zero sieci, daty
1990-xx. Indeks z sql/013 jest tworzony W transakcji testowej (baza lokalna moze
go jeszcze nie miec)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from mannaz import corp_actions
from mannaz.corp_actions import (
    CorpActionsSummary,
    DetectedEvent,
    _insert_event,
    apply_candidates,
    classify_against_existing,
    run_corp_actions,
)
from mannaz.db import get_connection

_SQL_013 = Path(__file__).resolve().parents[1] / "sql" / "013_corporate_events_klucz_splitu.sql"


def _sql_013_body() -> str:
    return "\n".join(
        ln for ln in _SQL_013.read_text(encoding="utf-8").splitlines() if ln.strip() not in ("BEGIN;", "COMMIT;")
    )


@pytest.fixture
def conn():
    try:
        c = get_connection()
    except Exception as exc:
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    c.rollback()
    try:
        with c.cursor() as cur:
            cur.execute(_sql_013_body())
        yield c
    finally:
        c.rollback()
        c.close()


def _free_instrument(c) -> tuple[int, str]:
    with c.cursor() as cur:
        cur.execute(
            "SELECT id, broker_ticker FROM instruments i "
            "WHERE NOT EXISTS (SELECT 1 FROM corporate_events e WHERE e.instrument_id = i.id) ORDER BY id LIMIT 1"
        )
        return cur.fetchone()


def _count(c) -> int:
    with c.cursor() as cur:
        cur.execute("SELECT count(*) FROM corporate_events")
        return cur.fetchone()[0]


def _store(c, inst, ticker, d, ratio, title, source="price_ratio_detector", etype="split"):
    with c.cursor() as cur:
        assert _insert_event(
            cur, instrument_id=inst, broker_ticker=ticker, event_date=d, event_type=etype,
            ratio=Decimal(ratio), source=source, date_source="inferred_boundary", title_raw=title,
        )


def _cand(inst, ticker, d, ratio, source, n=9, first_txn=None) -> DetectedEvent:
    return DetectedEvent(
        broker_ticker=ticker, instrument_id=inst, event_type="split", ratio=Decimal(ratio), event_date=d,
        date_source="inferred_boundary" if source == "price_ratio_detector" else "yfinance_splits",
        source=source, n_samples=n, median_ratio=Decimal(ratio),
        title_raw=f"[auto] {source}: {ticker} split median_ratio=24.9000 n={n} date_source=x",
        first_txn_date=first_txn,
    )


@pytest.mark.db
def test_spyi_like_title_change_same_date_is_known_no_write(conn):
    inst, t = _free_instrument(conn)
    d = date(1990, 4, 2)
    _store(conn, inst, t, d, "25", "[auto] price_ratio_detector: X split median_ratio=24.9000 n=8 date_source=x")
    before = _count(conn)
    with conn.cursor() as cur:
        res = apply_candidates(cur, [_cand(inst, t, d, "25", "price_ratio_detector", n=9)])
    assert len(res.known) == 1 and not res.saved and not res.date_drift
    assert _count(conn) == before


@pytest.mark.db
def test_price_ratio_other_date_is_date_drift_no_write(conn):
    inst, t = _free_instrument(conn)
    _store(conn, inst, t, date(1990, 4, 2), "25", "a", source="yfinance_splits")
    before = _count(conn)
    with conn.cursor() as cur:
        res = apply_candidates(cur, [_cand(inst, t, date(1990, 5, 7), "25", "price_ratio_detector")])
    assert [(d.stored_date, d.event.event_date) for d in res.date_drift] == [(date(1990, 4, 2), date(1990, 5, 7))]
    assert not res.saved and _count(conn) == before


@pytest.mark.db
def test_yfinance_new_saved_then_second_pass_known(conn):
    inst, t = _free_instrument(conn)
    cands = [_cand(inst, t, date(1990, 6, 4), "4", "yfinance_splits", n=0)]
    before = _count(conn)
    with conn.cursor() as cur:
        r1 = apply_candidates(cur, cands)
        r2 = apply_candidates(cur, cands)
    assert len(r1.saved) == 1 and not r1.known
    assert len(r2.known) == 1 and not r2.saved
    assert _count(conn) == before + 1


@pytest.mark.db
def test_same_run_price_ratio_after_yfinance_same_split_is_known(conn):
    inst, t = _free_instrument(conn)
    d = date(1990, 6, 4)
    with conn.cursor() as cur:
        r = apply_candidates(
            cur,
            [
                _cand(inst, t, d, "25", "yfinance_splits", n=0),
                _cand(inst, t, d, "25", "price_ratio_detector"),
            ],
        )
    assert len(r.saved) == 1 and len(r.known) == 1


@pytest.mark.db
def test_rule2_positive_control_ratio_2_vs_stored_25_is_new(conn):
    inst, t = _free_instrument(conn)
    _store(conn, inst, t, date(1990, 4, 2), "25", "a", source="yfinance_splits")
    with conn.cursor() as cur:
        state, stored = classify_against_existing(cur, _cand(inst, t, date(1990, 5, 7), "2", "price_ratio_detector"))
    assert (state, stored) == ("new", None)


@pytest.mark.db
def test_owner_fix_stored_split_before_first_txn_does_not_explain(conn):
    inst, t = _free_instrument(conn)
    _store(conn, inst, t, date(1990, 1, 10), "2", "a", source="yfinance_splits")  # przed pierwsza transakcja
    cand = _cand(inst, t, date(1990, 9, 3), "2", "price_ratio_detector", first_txn=date(1990, 5, 27))
    with conn.cursor() as cur:
        assert classify_against_existing(cur, cand) == ("new", None)
        # kontrolka: bez first_txn_date ten sam zapis wyjasnia rozbieznosc (date_drift)
        cand.first_txn_date = None
        assert classify_against_existing(cur, cand) == ("date_drift", date(1990, 1, 10))
    # i zapis PO pierwszej transakcji sie liczy
    _store(conn, inst, t, date(1990, 9, 3), "2", "b", source="yfinance_splits")
    cand.first_txn_date = date(1990, 5, 27)
    with conn.cursor() as cur:
        assert classify_against_existing(cur, cand) == ("known", date(1990, 9, 3))


@pytest.mark.db
def test_index_013_blocks_second_split_with_other_title_and_is_idempotent(conn):
    inst, t = _free_instrument(conn)
    d = date(1990, 7, 2)
    _store(conn, inst, t, d, "4", "tytul A")
    before = _count(conn)
    with conn.cursor() as cur:
        inserted = _insert_event(
            cur, instrument_id=inst, broker_ticker=t, event_date=d, event_type="split",
            ratio=Decimal(4), source="yfinance_splits", date_source="yfinance_splits", title_raw="tytul B",
        )
        assert inserted is False
        cur.execute(_sql_013_body())  # drugi raz bez bledu
    assert _count(conn) == before


@pytest.mark.db
def test_run_corp_actions_dry_run_writes_nothing(conn, monkeypatch):
    inst, t = _free_instrument(conn)
    s = CorpActionsSummary()
    s.candidates = [_cand(inst, t, date(1990, 8, 6), "4", "yfinance_splits", n=0)]
    monkeypatch.setattr(corp_actions, "collect_corp_action_candidates", lambda c: s)
    before = _count(conn)
    res = run_corp_actions(conn, dry_run=True)
    assert len(res.saved) == 1  # zaklasyfikowane jako do zapisu
    assert _count(conn) == before  # ale baza bez zmian

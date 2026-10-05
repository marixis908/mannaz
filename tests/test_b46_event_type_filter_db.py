"""B-46 (brief CC-B24, C2): odczyty `corporate_events` z `ratio` w FIFO
(`fifo._resolve_position`) i w risk (`risk._instrument_split_events`) filtrują
typ zdarzenia do ('split', 'reverse_split') — `share_exchange` z ratio jest
ignorowany. Filtr siedzi w SQL, więc testy db.

Jedna transakcja, ZERO commit, `conn.rollback()` w `finally`/fixture, dane
syntetyczne (instrument B46TEST, rachunek bez cyfr), daty 1990-xx, zero sieci.
Każde zdarzenie ma INNĄ datę (unikalność (instrument, data, typ) z sql/013)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from mannaz.db import get_connection
from mannaz.fifo import _resolve_position, positions_as_of
from mannaz.risk import _instrument_split_events

RACHUNEK = "AKCYJNY SYNTH-B46"
D_BUY = date(1990, 1, 2)
D_EXCHANGE = date(1990, 2, 1)
D_SPLIT = date(1990, 3, 1)
AS_OF_BETWEEN = date(1990, 2, 15)  # po share_exchange, przed splitem
AS_OF_AFTER = date(1990, 3, 15)    # po obu zdarzeniach


@pytest.fixture
def conn():
    try:
        c = get_connection()
    except Exception as exc:
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    c.rollback()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _setup(c, *, with_split: bool, with_exchange: bool) -> int:
    """Syntetyczny instrument + kupno 10 szt. za 100 PLN + wybrane zdarzenia (ratio=2)."""
    with c.cursor() as cur:
        cur.execute(
            "INSERT INTO instruments (broker_ticker, name, currency, instrument_type) "
            "VALUES ('B46TEST', 'SYNTH B46', 'PLN', 'equity') RETURNING id"
        )
        iid = cur.fetchone()[0]
        cur.execute(
            """
            INSERT INTO transactions (
                transaction_date, rachunek, currency, title_raw, amount,
                row_type, instrument_id, qty, price, source_file, source_sha256
            ) VALUES (%s, %s, 'PLN', 'B-46 TEST KUPNO', %s, 'kupno', %s, %s, %s, 'b46_test', %s)
            """,
            (D_BUY, RACHUNEK, Decimal("100"), iid, Decimal("10"), Decimal("10"), "0" * 64),
        )
        for flag, etype, d in ((with_exchange, "share_exchange", D_EXCHANGE), (with_split, "split", D_SPLIT)):
            if flag:
                cur.execute(
                    "INSERT INTO corporate_events (instrument_id, broker_ticker, event_date, event_type, ratio, title_raw) "
                    "VALUES (%s, 'B46TEST', %s, %s, %s, %s)",
                    (iid, d, etype, Decimal("2"), f"B-46 TEST {etype}"),
                )
    return iid


def _fifo_qty(c, iid: int, as_of: date) -> Decimal:
    with c.cursor() as cur:
        item = _resolve_position(cur, RACHUNEK, iid, "PLN", as_of)
    return item["pos"].qty


def _pit_qty(c, iid: int, as_of: date) -> Decimal:
    rows = [p for p in positions_as_of(c, as_of) if p["instrument_id"] == iid]
    assert len(rows) == 1
    return rows[0]["qty"]


@pytest.mark.db
def test_share_exchange_with_ratio_ignored_by_fifo(conn):
    try:
        iid = _setup(conn, with_split=False, with_exchange=True)
        assert _fifo_qty(conn, iid, AS_OF_BETWEEN) == Decimal("10")  # bez skalowania
        assert _pit_qty(conn, iid, AS_OF_BETWEEN) == Decimal("10")
    finally:
        conn.rollback()


@pytest.mark.db
def test_split_with_ratio_applied_by_fifo(conn):
    """Kontrolka dodatnia: ten sam wzorzec danych, typ 'split' -> skalowanie x2."""
    try:
        iid = _setup(conn, with_split=True, with_exchange=False)
        assert _fifo_qty(conn, iid, AS_OF_AFTER) == Decimal("20")
        assert _pit_qty(conn, iid, AS_OF_AFTER) == Decimal("20")
        assert _fifo_qty(conn, iid, D_SPLIT - date.resolution) == Decimal("10")  # przed splitem
    finally:
        conn.rollback()


@pytest.mark.db
def test_share_exchange_and_split_together_only_split_scales(conn):
    try:
        iid = _setup(conn, with_split=True, with_exchange=True)
        assert _fifo_qty(conn, iid, AS_OF_BETWEEN) == Decimal("10")
        assert _fifo_qty(conn, iid, AS_OF_AFTER) == Decimal("20")  # x2, nie x4
        assert _pit_qty(conn, iid, AS_OF_AFTER) == Decimal("20")
    finally:
        conn.rollback()


@pytest.mark.db
def test_risk_instrument_split_events_ignore_share_exchange(conn):
    """Ścieżka risk (`_instrument_split_events`, zasila `holding_period_start`)."""
    try:
        iid = _setup(conn, with_split=True, with_exchange=True)
        with conn.cursor() as cur:
            events = _instrument_split_events(cur, iid, AS_OF_AFTER)
            assert events == [{"date": D_SPLIT, "ratio": Decimal("2")}]
            # as_of przed splitem, po share_exchange -> pusto
            assert _instrument_split_events(cur, iid, AS_OF_BETWEEN) == []
    finally:
        conn.rollback()


@pytest.mark.db
def test_risk_instrument_split_events_only_share_exchange_is_empty(conn):
    try:
        iid = _setup(conn, with_split=False, with_exchange=True)
        with conn.cursor() as cur:
            assert _instrument_split_events(cur, iid, AS_OF_AFTER) == []
    finally:
        conn.rollback()

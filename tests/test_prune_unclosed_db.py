"""Testy bazodanowe C3 (brief CC-B48, B-48/T46) — `run_p3 prune-unclosed`.
Każdy test w JEDNEJ transakcji zakończonej `conn.rollback()` w `finally`
(`commit=False`) — zero trwałych zmian. Dane syntetyczne z datami 1990
(poza zakresem kalendarzy `exchange_calendars` -> reguła daty T46:
wiersz niezamknięty <=> `fetched_at::date <= price_date`). Zakres zawężony
do jednego istniejącego instrumentu (`instrument_ids`), bez ilości/kwot/
rachunków."""

from datetime import date, datetime, timezone

import pytest

from mannaz.db import get_connection
from mannaz.run_p3 import PruneCountMismatchError, find_unclosed_rows, run_prune_unclosed

D_PREV = date(1990, 1, 2)
D0 = date(1990, 1, 3)
FETCHED_D0_NOON = datetime(1990, 1, 3, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_conn():
    try:
        conn = get_connection()
    except Exception as exc:  # brak .env/hasla/serwera -> caly test pomijamy
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _instrument_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM instruments WHERE yahoo_symbol IS NOT NULL ORDER BY id LIMIT 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("brak instrumentu z yahoo_symbol w bazie")
    return row[0]


def _insert(conn, instrument_id: int, price_date: date, fetched_at: datetime) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO prices_daily (instrument_id, price_date, currency, source,
                                      close_split_adj, fetched_at)
            VALUES (%s, %s, 'PLN', 'yahoo', 10, %s)
            """,
            (instrument_id, price_date, fetched_at),
        )


def _yahoo_rows(conn, instrument_id: int) -> set[date]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT price_date FROM prices_daily WHERE instrument_id = %s AND source = 'yahoo' "
            "AND price_date BETWEEN '1990-01-01' AND '1990-12-31'",
            (instrument_id,),
        )
        return {r[0] for r in cur.fetchall()}


@pytest.mark.db
def test_prune_dry_run_writes_nothing(db_conn):
    iid = _instrument_id(db_conn)
    _insert(db_conn, iid, D_PREV, FETCHED_D0_NOON)  # zamknięta: D_PREV < data zapisu
    _insert(db_conn, iid, D0, FETCHED_D0_NOON)  # niezamknięta: zapis w dniu sesji
    keys = [(r.instrument_id, r.price_date) for r in find_unclosed_rows(db_conn, [iid])]
    assert (iid, D0) in keys
    assert (iid, D_PREV) not in keys

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM prices_daily")
        before = cur.fetchone()[0]
    result = run_prune_unclosed(db_conn, dry_run=True, instrument_ids=[iid], commit=False)
    assert result.deleted == 0
    assert any(r.price_date == D0 for r in result.rows)
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM prices_daily")
        after = cur.fetchone()[0]
    assert after == before
    assert _yahoo_rows(db_conn, iid) == {D_PREV, D0}


@pytest.mark.db
def test_prune_write_deletes_only_unclosed(db_conn):
    iid = _instrument_id(db_conn)
    _insert(db_conn, iid, D_PREV, FETCHED_D0_NOON)
    _insert(db_conn, iid, D0, FETCHED_D0_NOON)
    candidates = find_unclosed_rows(db_conn, [iid])
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM prices_daily WHERE instrument_id = %s", (iid,))
        before = cur.fetchone()[0]

    result = run_prune_unclosed(
        db_conn, dry_run=False, expect=len(candidates), instrument_ids=[iid], commit=False
    )

    assert result.deleted == len(candidates)
    assert _yahoo_rows(db_conn, iid) == {D_PREV}
    assert find_unclosed_rows(db_conn, [iid]) == []
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM prices_daily WHERE instrument_id = %s", (iid,))
        after = cur.fetchone()[0]
    assert before - after == result.deleted


@pytest.mark.db
def test_prune_expect_mismatch_rolls_back(db_conn):
    iid = _instrument_id(db_conn)
    _insert(db_conn, iid, D0, FETCHED_D0_NOON)
    n = len(find_unclosed_rows(db_conn, [iid]))
    with pytest.raises(PruneCountMismatchError):
        run_prune_unclosed(db_conn, dry_run=False, expect=n + 1, instrument_ids=[iid], commit=False)
    # rollback: syntetyczny wiersz wycofany razem z transakcją, nic nie usunięte trwale
    assert _yahoo_rows(db_conn, iid) == set()

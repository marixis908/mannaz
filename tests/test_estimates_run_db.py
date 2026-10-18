"""D1-D2 FPE-RUN: testy db, uruchamia CC; zawsze rollback (commit w D2 to licznik).

Bez stosowania migracji i bez HTTP. Wszystkie testy oznaczone db.
"""

from types import SimpleNamespace
import uuid

import pytest

from mannaz import estimates as est
from mannaz.db import get_connection

pytestmark = pytest.mark.db

BODY = (b'{"quoteSummary":{"result":[{"price":{"currency":"USD"},"earningsTrend":{"trend":['
        b'{"period":"0y","endDate":"2027-08-31","earningsEstimate":{"avg":2,"low":1,"high":3,'
        b'"numberOfAnalysts":5,"earningsCurrency":"USD"},"revenueEstimate":{}}]}}],"error":null}}')


@pytest.fixture
def conn():
    try:
        connection = get_connection()
    except Exception as exc:
        pytest.skip(f"polaczenie testowe niedostepne: {type(exc).__name__}")
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass('estimate_snapshots'), to_regclass('estimates'), "
                        "to_regclass('positions_fifo')")
            if any(table is None for table in cur.fetchone()):
                pytest.skip("migracja 020 niezastosowana")
        connection.rollback()
        yield connection
    finally:
        connection.rollback()
        connection.close()


def add_instrument(cur, tag, *, symbol=True, itype="equity", core=False, benchmark=False,
                   position=False):
    cur.execute(
        "INSERT INTO instruments (broker_ticker, currency, yahoo_symbol, instrument_type, "
        "is_core, is_benchmark) VALUES (%s, 'USD', %s, %s, %s, %s) RETURNING id",
        (f"{tag}_{uuid.uuid4().hex[:8]}", f"{tag}_{uuid.uuid4().hex}" if symbol else None,
         itype, core, benchmark),
    )
    instrument_id = cur.fetchone()[0]
    if position:
        cur.execute(
            "INSERT INTO positions_fifo (rachunek, instrument_id, currency, qty, residual_cost, "
            "first_entry_date, last_entry_date) VALUES ('SYNTHETIC', %s, 'USD', 1, 1, "
            "'2026-01-01', '2026-01-01')", (instrument_id,))
    return instrument_id


def test_d1_universe_filters(conn):
    with conn.cursor() as cur:
        good = add_instrument(cur, "D1G", position=True)
        bad = {
            "core": add_instrument(cur, "D1C", core=True, position=True),
            "benchmark": add_instrument(cur, "D1B", benchmark=True, position=True),
            "etf": add_instrument(cur, "D1E", itype="etf", position=True),
            "no_position": add_instrument(cur, "D1N"),
            "no_symbol": add_instrument(cur, "D1S", symbol=False, position=True),
        }
        universe = est.select_estimates_universe(cur)
        ids = [item[0] for item in universe]
        assert good in ids and not set(bad.values()) & set(ids)
        assert ids == sorted(ids)
        # kazdy zwrocony instrument spelnia kryteria (sprawdzenie niezalezne od danych bazy)
        cur.execute(
            "SELECT count(*) FROM instruments i WHERE i.id = ANY(%s) AND i.instrument_type = 'equity' "
            "AND i.yahoo_symbol IS NOT NULL AND NOT i.is_core AND NOT i.is_benchmark "
            "AND EXISTS (SELECT 1 FROM positions_fifo p WHERE p.instrument_id = i.id)", (ids,))
        assert cur.fetchone()[0] == len(ids)
        # z instrument_ids: tylko wskazane z symbolem; bez symbolu -> pominiete
        picked = est.select_estimates_universe(cur, [good, bad["no_symbol"], bad["etf"]])
        assert [item[0] for item in picked] == sorted([good, bad["etf"]])
        assert est.missing_universe_ids([good, bad["no_symbol"], bad["etf"]], picked) == [bad["no_symbol"]]
    conn.rollback()


class CountingConn:
    """Przekazuje do prawdziwego polaczenia, ale commit tylko liczy (test konczy rollback)."""

    def __init__(self, real):
        self.real = real
        self.commits = 0

    def cursor(self):
        return self.real.cursor()

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.real.rollback()


def test_d2_run_writes_both_tables_and_keeps_triggers(conn, tmp_path):
    with conn.cursor() as cur:
        first = add_instrument(cur, "D2A", position=True)
        second = add_instrument(cur, "D2B", position=True)
        cur.execute("SELECT id, yahoo_symbol FROM instruments WHERE id = ANY(%s) ORDER BY id",
                    ([first, second],))
        universe = cur.fetchall()
        cur.execute("SELECT tgname, tgenabled FROM pg_trigger WHERE tgname LIKE 'trg_estimate%%'")
        before = sorted(cur.fetchall())
    names = {name for name, _ in before}
    assert {"trg_estimate_snapshots_append_only", "trg_estimates_append_only",
            "trg_estimate_snapshots_no_truncate", "trg_estimates_no_truncate"} <= names
    assert all(enabled != "D" for _, enabled in before)
    counting = CountingConn(conn)

    def http_get(url, params=None, timeout=None):
        return SimpleNamespace(content=BODY, status_code=200,
                               headers={"Content-Type": "application/json", "Content-Encoding": None})

    summary = est.run_estimates(counting, universe, archive_root=tmp_path, http_get=http_get,
                                sleep=lambda s: None, pause_s=0)
    assert counting.commits == 2
    assert (summary.symbols, summary.snapshots, summary.in_db, summary.errors) == (2, 2, 2, 0)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM estimate_snapshots WHERE instrument_id = ANY(%s)",
                    ([first, second],))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT count(*) FROM estimates e JOIN estimate_snapshots s ON s.id = e.snapshot_id "
                    "WHERE s.instrument_id = ANY(%s)", ([first, second],))
        assert cur.fetchone()[0] == 2
        cur.execute("SELECT tgname, tgenabled FROM pg_trigger WHERE tgname LIKE 'trg_estimate%%'")
        assert sorted(cur.fetchall()) == before
    conn.rollback()

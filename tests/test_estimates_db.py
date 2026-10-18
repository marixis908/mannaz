"""S4: testy db, uruchamia CC po zastosowaniu 020; zawsze rollback.

Bez stosowania migracji i bez HTTP. Wszystkie testy oznaczone db.
"""

from dataclasses import replace
from decimal import Decimal
import uuid

import psycopg
import pytest

from mannaz import estimates as est
from mannaz.db import get_connection
from test_estimates import fetch, payload, trend

pytestmark = pytest.mark.db


@pytest.fixture
def conn():
    try:
        connection = get_connection()
    except Exception as exc:
        pytest.skip(f"polaczenie testowe niedostepne: {type(exc).__name__}")
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT to_regclass('estimate_snapshots'), to_regclass('estimates')")
            if any(table is None for table in cur.fetchone()):
                pytest.skip("migracja 020 niezastosowana")
        connection.rollback()
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def seeded(conn, tmp_path):
    fr = fetch(payload(trend()), symbol="SYNTHETIC_" + uuid.uuid4().hex)
    entry = est.archive_snapshot(tmp_path, fr)
    parsed = est.parse_quote_summary(fr.body)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO instruments (broker_ticker, currency) VALUES (%s, 'USD') RETURNING id",
                    (fr.source_symbol,))
        instrument_id = cur.fetchone()[0]
        summary = est.insert_snapshot(cur, instrument_id, entry, parsed, "synthetic-test")
    return instrument_id, entry, parsed, summary


def test_insert_read_roundtrip(conn, seeded):
    instrument_id, entry, parsed, summary = seeded
    with conn.cursor() as cur:
        cur.execute("SELECT instrument_id, raw_ref, raw_sha256, byte_count, target_mean, "
                    "price_currency, query_params, code_sha, quality_flag, parse_status, "
                    "skipped_labels, error FROM estimate_snapshots WHERE id=%s",
                    (summary.snapshot_id,))
        assert cur.fetchone() == (instrument_id, entry.raw_ref, entry.raw_sha256,
                                  entry.byte_count, None, "USD", entry.fetch.query_params, "synthetic-test",
                                  "low", parsed.status, [], None)
        cur.execute("SELECT period, period_type, period_end, fiscal_year, eps_avg, rev_avg, "
                    "n_analysts_eps, n_analysts_rev, eps_currency, rev_currency, "
                    "period_end_check, currency_check FROM estimates WHERE snapshot_id=%s",
                    (summary.snapshot_id,))
        row = parsed.rows[0]
        assert cur.fetchall() == [(row.period, row.period_type, row.period_end, row.fiscal_year,
                                   Decimal(2), Decimal(100), 5, 4, "USD", None,
                                   est.PERIOD_UNCONFIRMED, None)]
    assert summary.inserted_rows == 1 and summary.archive_only_count == 0


def test_meanings_not_merged(conn, tmp_path):
    fr = fetch(payload(trend("0q", end="zle"), trend("+5y")), symbol="SYNTHETIC_" + uuid.uuid4().hex,
               status=404)
    entry = est.archive_snapshot(tmp_path, fr)
    parsed = est.parse_quote_summary(fr.body)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO instruments (broker_ticker, currency) VALUES (%s, 'USD') RETURNING id",
                    (fr.source_symbol,))
        summary = est.insert_snapshot(cur, cur.fetchone()[0], entry, parsed, "synthetic-test")
        cur.execute("SELECT quality_flag, parse_status, skipped_labels, error "
                    "FROM estimate_snapshots WHERE id=%s", (summary.snapshot_id,))
        assert cur.fetchone() == ("low", "bledna data okresu", ["+5y"], "HTTP 404")
        cur.execute("SELECT period, period_end, fiscal_year, status FROM estimates "
                    "WHERE snapshot_id=%s", (summary.snapshot_id,))
        assert cur.fetchall() == [("0q", None, None, "bledna data okresu")]


@pytest.mark.parametrize("table", ["estimate_snapshots", "estimates"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_append_only(conn, seeded, table, operation):
    snapshot_id = seeded[3].snapshot_id
    # Kontrolka dodatnia: INSERT/SELECT w tych samych tabelach przed odmowami.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM estimate_snapshots WHERE id=%s", (snapshot_id,))
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM estimates WHERE snapshot_id=%s", (snapshot_id,))
        assert cur.fetchone()[0] == 1
        if operation == "UPDATE":
            statement = f"UPDATE {table} SET source=source" if table == "estimate_snapshots" else "UPDATE estimates SET period=period"
        elif operation == "DELETE":
            statement = f"DELETE FROM {table}"
        else:
            # Obie tabele dla FK, bez CASCADE na inne tabele repo.
            statement = "TRUNCATE estimate_snapshots, estimates" if table == "estimate_snapshots" else "TRUNCATE estimates"
        with pytest.raises(psycopg.errors.RaiseException, match="tylko do dopisywania"):
            with conn.transaction():
                cur.execute(statement)


@pytest.mark.parametrize("raw_ref,raw_sha256", [("synthetic.body", None), (None, "a" * 64)])
def test_raw_pair_check(conn, seeded, raw_ref, raw_sha256):
    instrument_id, entry, parsed, _ = seeded
    invalid = replace(entry, raw_ref=raw_ref, raw_sha256=raw_sha256,
                      fetch=replace(entry.fetch, source="synthetic-check"))
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                est.insert_snapshot(cur, instrument_id, invalid, parsed, "synthetic")


def test_instrument_not_null(conn, seeded):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.NotNullViolation):
            with conn.transaction():
                cur.execute(
                    "INSERT INTO estimate_snapshots (instrument_id, source, source_symbol, fetched_at_utc, "
                    "modules, query_params, library, library_version) "
                    "VALUES (NULL, 'synthetic', 'SYN', now(), '{}', '{}', 'yfinance', '1.7.0')")


def test_unmapped_archive_only(conn, seeded):
    _, entry, parsed, summary = seeded
    fr = replace(entry.fetch, source_symbol="UNMAPPED_" + uuid.uuid4().hex)
    with conn.cursor() as cur:
        # Kontrolka dodatnia zapisu na tym samym snapshotowym typie wejscia.
        cur.execute("SELECT count(*) FROM estimate_snapshots WHERE id=%s", (summary.snapshot_id,))
        assert cur.fetchone()[0] == 1
        result = est.insert_snapshot(cur, None, replace(entry, fetch=fr), parsed, "synthetic")
        cur.execute("SELECT count(*) FROM estimate_snapshots WHERE source_symbol=%s", (fr.source_symbol,))
        assert cur.fetchone()[0] == 0
    assert result.archive_only_count == 1 and result.archive_only_symbols == (fr.source_symbol,)
    assert result.inserted_rows == 0 and result.snapshot_id is None

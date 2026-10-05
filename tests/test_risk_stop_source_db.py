"""B-52 (brief CC-B52): CHECK risk_daily.stop_source (sql/014) dopuszcza
'two_n_held' i odrzuca inne wartosci. Rollback na koncu — zero trwalych zmian."""

from datetime import date

import psycopg
import pytest

from mannaz.db import get_connection


@pytest.mark.db
def test_stop_source_two_n_held_allowed_and_junk_rejected():
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM instruments LIMIT 1")
            row = cur.fetchone()
            assert row is not None
            iid = row[0] if not isinstance(row, dict) else row["id"]
            sql = "INSERT INTO risk_daily (rachunek, instrument_id, risk_date, stop_source) VALUES (%s, %s, %s, %s)"
            cur.execute(sql, ("__CC_B52__", iid, date(1990, 1, 1), "two_n_held"))
            cur.execute("SAVEPOINT sp")
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(sql, ("__CC_B52__", iid, date(1990, 1, 2), "xyz"))
            cur.execute("ROLLBACK TO SAVEPOINT sp")
    finally:
        conn.rollback()
        conn.close()

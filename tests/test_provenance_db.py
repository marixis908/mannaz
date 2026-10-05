"""B-23, B-54 (sql/015): kolumny pochodzenia w `source_runs` i `risk_daily`;
CHECK `triggered_by` dopuszcza tylko 'cycle'/'cli'/NULL. Rollback na końcu —
zero trwałych zmian."""

import psycopg
import pytest

from mannaz.db import get_connection


@pytest.mark.db
def test_source_runs_triggered_by_check_and_code_sha_columns():
    try:
        conn = get_connection()
    except Exception as exc:  # brak .env/hasla/serwera -> test pomijamy (marker db)
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name FROM information_schema.columns
                WHERE (table_name, column_name) IN
                      (('source_runs', 'triggered_by'), ('source_runs', 'code_sha'), ('risk_daily', 'code_sha'))
                """
            )
            assert len(cur.fetchall()) == 3, "brak kolumn z sql/015 — zastosuj migracje"
            sql = (
                "INSERT INTO source_runs (source, row_count, sha256_input, triggered_by, code_sha) "
                "VALUES (%s, 0, %s, %s, %s)"
            )
            for i, trig in enumerate(("cycle", "cli", None)):
                cur.execute(sql, ("__CC_B54__", f"k{i}", trig, "a" * 40))
            cur.execute("SAVEPOINT sp")
            with pytest.raises(psycopg.errors.CheckViolation):
                cur.execute(sql, ("__CC_B54__", "k9", "cron", None))
            cur.execute("ROLLBACK TO SAVEPOINT sp")
    finally:
        conn.rollback()
        conn.close()

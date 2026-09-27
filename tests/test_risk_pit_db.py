"""Testy bazodanowe S4/S5 (brief CC-S) — zapis `risk_daily` jako całość
(DELETE-then-rebuild) i determinizm zapadki Chandeliera. Każdy test w JEDNEJ
transakcji zakończonej `conn.rollback()` w `finally` — zero trwałych zmian w
bazie. Pomijane (skip), gdy `mannaz.db.get_connection()` zawiedzie (brak
.env/hasła/serwera) — patrz `db_conn` fixture."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from mannaz.db import get_connection
from mannaz.fifo import positions_as_of
from mannaz.risk import RATCHET_INIT_DATE, resolve_default_risk_date, run_risk


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
        conn.close()


@pytest.mark.db
def test_s4_ghost_row_removed_and_row_count_matches_positions_as_of(db_conn):
    """S4: sztuczny wiersz risk_daily dla pozycji nieistniejacej na D znika po
    run_risk (DELETE-then-rebuild); liczba wierszy D == len(positions_as_of(D));
    jeden computed_at dla calego przebiegu."""
    conn = db_conn
    d = date(2026, 9, 24)
    ghost_rachunek = "__CC_S_S4_GHOST__"
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM instruments LIMIT 1")
            row = cur.fetchone()
            assert row is not None, "brak jakichkolwiek instrumentow w bazie testowej"
            instrument_id = row[0]

            cur.execute(
                "INSERT INTO risk_daily (rachunek, instrument_id, risk_date) VALUES (%s, %s, %s)",
                (ghost_rachunek, instrument_id, d),
            )

            expected_positions = positions_as_of(cur, d)

        run_risk(conn, as_of=d, commit=False)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM risk_daily WHERE risk_date = %s AND rachunek = %s",
                (d, ghost_rachunek),
            )
            assert cur.fetchone()[0] == 0, "wiersz-widmo powinien zniknac po run_risk (DELETE-then-rebuild, S4)"

            cur.execute("SELECT count(*) FROM risk_daily WHERE risk_date = %s", (d,))
            actual_count = cur.fetchone()[0]
            assert actual_count == len(expected_positions)

            if actual_count > 0:
                cur.execute("SELECT DISTINCT computed_at FROM risk_daily WHERE risk_date = %s", (d,))
                computed_ats = cur.fetchall()
                assert len(computed_ats) == 1, "wszystkie wiersze przebiegu musza miec jeden computed_at (S4)"
    finally:
        conn.rollback()

    # baza nie zmieniona trwale — weryfikacja poza transakcja testowa
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM risk_daily WHERE rachunek = %s", (ghost_rachunek,))
        assert cur.fetchone()[0] == 0


@pytest.mark.db
def test_s5_ratchet_deterministic_regardless_of_prior_risk_daily_rows(db_conn):
    """S5: stop_effective na D=2026-09-25 identyczny niezaleznie od tego, czy
    risk_daily bylo puste, czy zawieralo juz wiersz dla D-1 (2026-09-24) —
    zapadka nie zalezy od tego, co juz przeliczono (naprawa F4)."""
    conn = db_conn
    d = date(2026, 9, 25)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")

        summary_direct = run_risk(conn, as_of=d, commit=False)
        stops_direct = {(r.rachunek, r.instrument_id): r.stop_effective for r in summary_direct.rows}

        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")

        run_risk(conn, as_of=RATCHET_INIT_DATE, commit=False)
        summary_chained = run_risk(conn, as_of=d, commit=False)
        stops_chained = {(r.rachunek, r.instrument_id): r.stop_effective for r in summary_chained.rows}

        assert stops_direct == stops_chained
    finally:
        conn.rollback()


@pytest.mark.db
def test_s5_run_risk_unaffected_by_transactions_and_prices_after_d(db_conn):
    """S5: sprzedaz calej pozycji i absurdalna cena wstawione z data D+10 nie
    zmieniaja wyniku run_risk(D) — brak look-ahead (F1/F3/F4 razem)."""
    conn = db_conn
    d = resolve_default_risk_date(conn)
    if d is None:
        pytest.skip("brak sesji z kompletnymi cenami satelity — nie da sie wyznaczyc D")

    try:
        with conn.cursor() as cur:
            positions = positions_as_of(cur, d)
        if not positions:
            pytest.skip("brak otwartych pozycji na D w biezacej bazie testowej")
        target = positions[0]

        summary_before = run_risk(conn, as_of=d, commit=False)
        rows_before = {
            (r.rachunek, r.instrument_id): (r.stop_effective, r.risk_pln, r.qty) for r in summary_before.rows
        }
        capital_before = summary_before.capital_satelite_pln

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM risk_daily WHERE risk_date = %s", (d,))
            db_count_before = cur.fetchone()[0]

        future_date = d + timedelta(days=10)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transactions (
                    transaction_date, rachunek, currency, title_raw, amount,
                    row_type, instrument_id, qty, price, source_file, source_sha256
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    future_date, target["rachunek"], target["currency"],
                    "CC-S TEST GHOST FUTURE SALE", Decimal("999999"),
                    "sprzedaz", target["instrument_id"], abs(target["qty"]), Decimal("1"),
                    "cc_s_test", "0" * 64,
                ),
            )
            cur.execute(
                """
                INSERT INTO prices_daily (
                    instrument_id, price_date, currency, source,
                    open_split_adj, high_split_adj, low_split_adj, close_split_adj, volume_split_adj
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    target["instrument_id"], future_date, "TEST", "cc_s_test",
                    Decimal("999999"), Decimal("999999"), Decimal("1"), Decimal("999999"), Decimal("1"),
                ),
            )

        summary_after = run_risk(conn, as_of=d, commit=False)
        rows_after = {
            (r.rachunek, r.instrument_id): (r.stop_effective, r.risk_pln, r.qty) for r in summary_after.rows
        }

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM risk_daily WHERE risk_date = %s", (d,))
            db_count_after = cur.fetchone()[0]

        assert db_count_after == db_count_before
        assert rows_after == rows_before
        assert summary_after.capital_satelite_pln == capital_before
    finally:
        conn.rollback()

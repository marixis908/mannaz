"""Testy bazodanowe S4/S5 (brief CC-S) — zapis `risk_daily` jako całość
(DELETE-then-rebuild) i determinizm zapadki Chandeliera. Każdy test w JEDNEJ
transakcji zakończonej `conn.rollback()` w `finally` — zero trwałych zmian w
bazie. Pomijane (skip), gdy `mannaz.db.get_connection()` zawiedzie (brak
.env/hasła/serwera) — patrz `db_conn` fixture."""

from datetime import date, timedelta
from decimal import Decimal

import pytest

from mannaz.db import get_connection
from mannaz.fifo import KONTRAKTOWY_PREFIX, positions_as_of
from mannaz.risk import (
    RATCHET_INIT_DATE,
    IncompleteRiskDateError,
    is_risk_budget_eligible,
    kontraktowy_account_value,
    resolve_default_risk_date,
    run_risk,
)


@pytest.fixture
def db_conn():
    """U4 (brief CC-U): baza testowa NIE ma jeszcze migracji sql/008
    (price_is_stale/price_date_used) — aplikujemy jej treść w TEJ SAMEJ
    transakcji przed testem (DDL w Postgresie jest transakcyjny), żeby
    `run_risk`/`_write_row` mogły zapisywać te kolumny. Po teście: rollback
    (obronnie, testy i tak robią własny rollback) + weryfikacja przez
    information_schema, że kolumny NIE zostały trwale dodane."""
    try:
        conn = get_connection()
    except Exception as exc:  # brak .env/hasla/serwera -> caly test pomijamy
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS price_is_stale BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE risk_daily ADD COLUMN IF NOT EXISTS price_date_used DATE")
    try:
        yield conn
    finally:
        conn.rollback()  # obronnie — testy robia wlasny rollback, ale gdyby nie zrobily
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'risk_daily' AND column_name IN ('price_is_stale', 'price_date_used')"
            )
            leftover_columns = cur.fetchall()
        conn.close()
        assert leftover_columns == [], (
            "U4 (brief CC-U): migracja 008 nie zostala w pelni cofnieta po tescie — "
            f"kolumny nadal istnieja trwale: {leftover_columns}"
        )


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
    """S5: stop_effective na D identyczny niezaleznie od tego, czy risk_daily
    bylo puste, czy zawieralo juz wiersz dla D-1 (RATCHET_INIT_DATE) — zapadka
    nie zalezy od tego, co juz przeliczono (naprawa F4). D = najpozniejsza
    data kompletna wg T27 (brief CC-U, U2/U3) — NIE sztywne 2026-09-25: ta
    data ma od 2026-09-27 pozycje bez ceny na D (rynek otwarty bez ceny),
    wiec run_risk(2026-09-25) rzuca IncompleteRiskDateError (patrz
    `test_u5_...`), a ten test dotyczy determinizmu zapadki, nie pokrycia
    cen."""
    conn = db_conn
    d = resolve_default_risk_date(conn)
    if d is None:
        pytest.skip("brak sesji kompletnej wg T27 — nie da sie wyznaczyc D")
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


# ---------------------------------------------------------------------------
# U5 (7)/(8) (brief CC-U) — testy bazodanowe reguly ceny na D (T27) na
# biezacej bazie. Zero ilosci/kwot/numerow rachunkow wpisanych na sztywno:
# (7) porownuje TYLKO liczbe pozycji niekompletnych i wspolna przyczyne;
# (8) porownuje sumy C/R policzone W TESCIE z biezacej tabeli risk_daily
# (sprzed przebiegu) z wynikiem run_risk — nie z zadnej stalej.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_u5_2026_09_25_incomplete_market_open_no_price_zero_writes(db_conn):
    """U5 (7): run_risk(2026-09-25, commit=False) na obecnej bazie ->
    IncompleteRiskDateError; wszystkie pozycje niekompletne z ta sama
    przyczyna 'rynek otwarty bez ceny' (market_open_no_price — GPW/gieldy w
    tym dniu byly otwarte, ale 22 instrumenty maja swiece bez zamkniecia);
    count(risk_daily WHERE risk_date=D) identyczny przed i po (run_risk robi
    conn.rollback() PRZED rzuceniem, zero zapisu, S4/U2)."""
    conn = db_conn
    d = date(2026, 9, 25)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM risk_daily WHERE risk_date = %s", (d,))
            count_before = cur.fetchone()[0]

        with pytest.raises(IncompleteRiskDateError) as exc_info:
            run_risk(conn, as_of=d, commit=False)

        items = exc_info.value.items
        assert len(items) == 23
        assert all(it.reason == "market_open_no_price" for it in items)

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM risk_daily WHERE risk_date = %s", (d,))
            count_after = cur.fetchone()[0]
        assert count_after == count_before
    finally:
        conn.rollback()


@pytest.mark.db
def test_u5_2026_09_24_regression_capital_and_risk_pct_unchanged(db_conn):
    """U5 (8): run_risk(2026-09-24, commit=False) — dzien inicjalizacji
    systemu, wg [Z] kazda pozycja ma komplet na D (cena dokladnie na D) —
    57 wierszy, stale_positions_count=0, a `capital_satelite_positions_pln`/
    `total_risk_pct_satellite_capital` rowne wartosci referencyjnej policzonej
    W TYM TESCIE z BIEZACEJ (sprzed przebiegu) tabeli risk_daily + instruments
    (sumy C = kapital pozycji, R = suma risk_pln pozycji uprawnionych do
    budzetow) — regresja 24.09 nie moze sie zmienic (brief CC-U, fakt [Z])."""
    conn = db_conn
    d = date(2026, 9, 24)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.qty, r.close_d, r.fx_rate, r.risk_pln, i.instrument_type, i.is_core
                FROM risk_daily r JOIN instruments i ON i.id = r.instrument_id
                WHERE r.risk_date = %s
                """,
                (d,),
            )
            existing_rows = cur.fetchall()
        assert existing_rows, "brak istniejacych wierszy risk_daily na 24.09 w bazie testowej — nie da sie zbudowac referencji"

        ref_capital_positions = sum(
            (
                qty * close_d * fx_rate
                for (qty, close_d, fx_rate, risk_pln, itype, is_core) in existing_rows
                if itype in ("equity", "etf") and not is_core and close_d is not None and fx_rate is not None
            ),
            Decimal(0),
        )
        ref_satellite_risk_total = sum(
            (
                risk_pln
                for (qty, close_d, fx_rate, risk_pln, itype, is_core) in existing_rows
                if is_risk_budget_eligible(itype, bool(is_core)) and risk_pln is not None
            ),
            Decimal(0),
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT t.transaction_date, t.currency, t.row_type, t.amount, t.qty, t.price,
                       i.multiplier, i.broker_ticker
                FROM transactions t
                LEFT JOIN instruments i ON i.id = t.instrument_id
                WHERE t.rachunek LIKE %s AND t.transaction_date <= %s
                ORDER BY t.transaction_date, t.id
                """,
                (f"{KONTRAKTOWY_PREFIX}%", d),
            )
            cols = ("transaction_date", "currency", "row_type", "amount", "qty", "price", "multiplier", "broker_ticker")
            kontraktowy_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        ref_kontraktowy = kontraktowy_account_value(kontraktowy_rows, as_of=d)
        ref_capital_total = ref_capital_positions + ref_kontraktowy
        ref_total_risk_pct = (
            (ref_satellite_risk_total / ref_capital_total * Decimal(100)) if ref_capital_total else None
        )

        summary = run_risk(conn, as_of=d, commit=False)

        assert len(summary.rows) == 57
        assert summary.stale_positions_count == 0
        assert summary.capital_satelite_positions_pln == ref_capital_positions
        assert summary.total_risk_pct_satellite_capital == ref_total_risk_pct
    finally:
        conn.rollback()

"""B-38B (C4): testy bazodanowe rejestru M89 i instrumentu progu. Jedna
transakcja na test, `conn.rollback()` w `finally` (polaczenie testowe nigdy nie
commituje). Pomijane, gdy `get_connection()` zawiedzie albo migracja 017 nie
jest zastosowana. Dane syntetyczne (daty 1990, rachunek `TEST 000001`)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from mannaz import cycle, prices, risk
from mannaz import satellite as sat
from mannaz.cycle import run_cycle
from mannaz.db import get_connection
from mannaz.risk import RiskSummary

from test_cycle_db import D_B05, RACHUNEK, _run_cycle_kwargs, _stub_risk_fn

RUN_AT = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn():
    try:
        c = get_connection()
    except Exception as exc:
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    try:
        with c.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('satellite_mandate_events'), to_regclass('satellite_mandate_violations'), "
                "(SELECT count(*) FROM information_schema.columns "
                " WHERE table_name = 'instruments' AND column_name = 'is_benchmark')"
            )
            ev, vi, col = cur.fetchone()
        if ev is None or vi is None or not col:
            pytest.skip("migracja sql/017 niezastosowana w bazie testowej")
        c.rollback()
        yield c
    finally:
        c.rollback()
        c.close()


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _analysis_1990():
    """Seria syntetyczna w 1990 (dni poza zakresem prawdziwych danych), z alertami i naruszeniami."""
    axis = _weekdays(date(1990, 8, 6), 45)
    nav, v = [], 100000.0
    for i in range(45):
        if 10 <= i < 35:
            v *= 0.98
        nav.append(v)
    flows = [0.0] * 45
    bench = {"sp500": [100.0] * 45, "spyi": [100.0] * 45}
    return sat.analyze_series(axis, nav, flows, bench, True), axis


def _count(cur, table: str) -> int:
    cur.execute(f"SELECT count(*) FROM {table} WHERE EXTRACT(YEAR FROM {'event_date' if 'events' in table else 'window_end'}) = 1990")
    return cur.fetchone()[0]


_EV_ROW = (1, "abs", date(1990, 1, 2), "alert", 0.4, 0.35, "zweryfikowane")
_EV_SQL = (
    "INSERT INTO satellite_mandate_events (registry, component, event_date, level, dd_current, limit_value, data_status) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
_VI_ROW = (1, "abs", date(1989, 12, 4), date(1990, 1, 5), 0.4, 0.35, "zweryfikowane")
_VI_SQL = (
    "INSERT INTO satellite_mandate_violations (registry, component, window_start, window_end, max_dd, limit_value, data_status) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s)"
)


@pytest.mark.db
@pytest.mark.parametrize(
    "insert_sql,row,update_sql,update_params,delete_sql,delete_params",
    [
        (_EV_SQL, _EV_ROW,
         "UPDATE satellite_mandate_events SET dd_current = 0.1 WHERE event_date = %s", (date(1990, 1, 2),),
         "DELETE FROM satellite_mandate_events WHERE event_date = %s", (date(1990, 1, 2),)),
        (_VI_SQL, _VI_ROW,
         "UPDATE satellite_mandate_violations SET max_dd = 0.1 WHERE window_end = %s", (date(1990, 1, 5),),
         "DELETE FROM satellite_mandate_violations WHERE window_end = %s", (date(1990, 1, 5),)),
    ],
    ids=["events", "violations"],
)
def test_e_trigger_rejects_update_and_delete(conn, insert_sql, row, update_sql, update_params, delete_sql, delete_params):
    with conn.cursor() as cur:
        cur.execute(insert_sql, row)  # INSERT dozwolony
        with pytest.raises(psycopg.errors.RaiseException, match="tylko do dopisywania"):
            with conn.transaction():
                cur.execute(update_sql, update_params)
        with pytest.raises(psycopg.errors.RaiseException, match="tylko do dopisywania"):
            with conn.transaction():
                cur.execute(delete_sql, delete_params)
    conn.rollback()


@pytest.mark.db
def test_e2_trigger_rejects_truncate(conn):
    """Rew. 2 briefu: TRUNCATE obu tabel M89 odrzucony; kontrolka dodatnia —
    TRUNCATE tabeli tymczasowej w tej samej transakcji przechodzi."""
    with conn.cursor() as cur:
        for table in ("satellite_mandate_events", "satellite_mandate_violations"):
            with pytest.raises(psycopg.errors.RaiseException, match="tylko do dopisywania: TRUNCATE"):
                with conn.transaction():
                    cur.execute(f"TRUNCATE {table}")
        cur.execute("CREATE TEMP TABLE t_b38b_truncate_ctl (x int) ON COMMIT DROP")
        cur.execute("INSERT INTO t_b38b_truncate_ctl VALUES (1)")
        with conn.transaction():
            cur.execute("TRUNCATE t_b38b_truncate_ctl")
        cur.execute("SELECT count(*) FROM t_b38b_truncate_ctl")
        assert cur.fetchone()[0] == 0
    conn.rollback()


@pytest.mark.db
def test_f_unique_and_on_conflict_do_nothing_no_duplicates(conn):
    an, axis = _analysis_1990()
    d = axis[-1]
    with conn.cursor() as cur:
        p1 = sat.persist_mandate_registry(cur, 1, an["alerts"], an["violation_registry"], d, "niezweryfikowane",
                                          RUN_AT, "sha-test", True)
        n_ev, n_vi = len(p1["events"]["new"]), len(p1["violations"]["new"])
        assert n_ev > 0 and n_vi > 0
        assert (_count(cur, "satellite_mandate_events"), _count(cur, "satellite_mandate_violations")) == (n_ev, n_vi)
        # drugi zapis tych samych danych w tej samej transakcji: zero nowych, bez duplikatow
        p2 = sat.persist_mandate_registry(cur, 1, an["alerts"], an["violation_registry"], d, "niezweryfikowane",
                                          RUN_AT, "sha-test", True)
        assert p2["events"]["new"] == [] and p2["violations"]["new"] == []
        assert (_count(cur, "satellite_mandate_events"), _count(cur, "satellite_mandate_violations")) == (n_ev, n_vi)
        # wprost: ten sam INSERT z ON CONFLICT DO NOTHING nie dodaje wiersza ...
        cur.execute(_EV_SQL + " ON CONFLICT (registry, component, event_date, level) DO NOTHING", _EV_ROW)
        cur.execute(_EV_SQL + " ON CONFLICT (registry, component, event_date, level) DO NOTHING", _EV_ROW)
        assert cur.rowcount == 0
        # ... a bez ON CONFLICT UNIQUE odrzuca duplikat
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                cur.execute(_EV_SQL, _EV_ROW)
    conn.rollback()


_TEST_ISIN = "XS0000000BMK"


@pytest.mark.db
def test_h_benchmark_instrument_mapped_for_prices_but_not_a_position(conn):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO instruments (broker_ticker, name, isin, yahoo_symbol, currency, exchange, instrument_type, "
            "is_core, is_benchmark) VALUES ('TSTBMK', 'SYNTH BENCH', %s, 'TSTBMK.DE', 'EUR', 'XETRA', 'etf', false, true) "
            "RETURNING id",
            (_TEST_ISIN,),
        )
        bench_id = cur.fetchone()[0]
        # kontrolka ujemna: zwykly instrument bez pozycji, bez rdzenia, bez is_benchmark
        cur.execute(
            "INSERT INTO instruments (broker_ticker, name, yahoo_symbol, currency, exchange, instrument_type) "
            "VALUES ('TSTPLAIN', 'SYNTH PLAIN', 'TSTPLAIN.DE', 'EUR', 'XETRA', 'etf') RETURNING id"
        )
        plain_id = cur.fetchone()[0]
        # kontrolka dodatnia: instrument z pozycja (transakcja + positions_fifo) musi byc widoczny po stronie pozycji
        cur.execute(
            "INSERT INTO instruments (broker_ticker, name, yahoo_symbol, currency, exchange, instrument_type) "
            "VALUES ('TSTPOS', 'SYNTH POS', 'TSTPOS.DE', 'EUR', 'XETRA', 'etf') RETURNING id"
        )
        pos_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO transactions (transaction_date, rachunek, currency, title_raw, amount, row_type, instrument_id, "
            "qty, price, source_file, source_sha256) VALUES (%s, %s, 'EUR', 'C4 TEST KUPNO', 10, 'kupno', %s, 1, 10, "
            "'c4_test', %s)",
            (date(1990, 3, 1), RACHUNEK, pos_id, "0" * 64),
        )
        cur.execute(
            "INSERT INTO positions_fifo (rachunek, instrument_id, currency, qty, residual_cost, first_entry_date, "
            "last_entry_date) VALUES (%s, %s, 'EUR', 1, 10, DATE '1990-03-01', DATE '1990-03-01')",
            (RACHUNEK, pos_id),
        )

        mapped = {m["id"] for m in prices._mapped_instruments(cur, None)}
        assert bench_id in mapped  # cykl pobiera ceny progu
        assert pos_id in mapped  # kontrolka dodatnia
        assert plain_id not in mapped  # kontrolka ujemna: sam warunek is_benchmark robi roznice

        reg_tickers = {r.broker_ticker for r in cycle._registration_rows(cur)}
        assert "TSTPOS" in reg_tickers  # kontrolka dodatnia bramki rejestracji
        assert "TSTBMK" not in reg_tickers

        open_ids = {p["instrument_id"] for p in risk.open_positions_as_of(cur, date(1990, 3, 10))}
        assert pos_id in open_ids
        assert bench_id not in open_ids
        fifo_ids = {p["instrument_id"] for p in sat_positions(cur)}
        assert bench_id not in fifo_ids
    conn.rollback()


def sat_positions(cur):
    from mannaz.fifo import positions_as_of

    return positions_as_of(cur, date(1990, 3, 10))


@pytest.mark.db
def test_g_satellite_stage_exception_is_data_failure_other_sections_kept(conn, tmp_path, monkeypatch):
    monkeypatch.setattr("mannaz.cycle.run_fifo", lambda *a, **k: None)
    monkeypatch.setattr("mannaz.cycle._registration_rows", lambda cur: [])

    def boom(c, d, **kwargs):
        raise RuntimeError("blad testowy etapu")

    try:
        kwargs = _run_cycle_kwargs(
            tmp_path, conn, risk_fn=_stub_risk_fn(D_B05), resolve_date_fn=lambda c, **k: D_B05, satellite_fn=boom
        )
        result = run_cycle(**kwargs)
        assert result.exit_code == 2
        assert any(f.startswith("błąd etapu satelity: RuntimeError") for f in result.state.import_failures)
        assert result.state.risk_aggregates is not None  # sekcja ryzyka policzona przed satelita
        assert cycle.STAGE_SATELLITE in result.state.stage_not_executed
        text = result.report_path.read_text(encoding="utf-8")
        assert "## Ryzyko\n- heat ogółem" in text
        assert "## Satelita (§21.6)\nnie wykonano (błąd etapu satelity" in text
        assert "błąd etapu satelity: RuntimeError" in text
    finally:
        conn.rollback()

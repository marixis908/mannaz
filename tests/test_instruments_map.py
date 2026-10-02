"""Testy czystej logiki mapowania instrumentów — wyłącznie dane syntetyczne,
bez sieci/bazy. Brief CC-P, P3.1."""

from decimal import Decimal

from mannaz.instruments_map import (
    EXPLICIT_SYMBOL_OVERRIDES,
    candidate_yahoo_symbol,
    exchange_fallback_from_suffix,
    futures_base_mapping,
    normalize_exchange,
)


# ---------------------------------------------------------------------------
# candidate_yahoo_symbol
# ---------------------------------------------------------------------------


def test_gpw_isin_gets_wa_suffix():
    assert (
        candidate_yahoo_symbol(
            broker_ticker="CDR", isin="PLOPTTC00011", instrument_type="equity", is_core=False
        )
        == "CDR.WA"
    )


def test_core_etf_gets_de_suffix_regardless_of_isin():
    assert (
        candidate_yahoo_symbol(
            broker_ticker="SPYI", isin="IE00B3YLTY66", instrument_type="etf", is_core=True
        )
        == "SPYI.DE"
    )


def test_explicit_european_override_takes_precedence_over_default_us_rule():
    for ticker, expected in EXPLICIT_SYMBOL_OVERRIDES.items():
        assert (
            candidate_yahoo_symbol(
                broker_ticker=ticker, isin="NL0000000000", instrument_type="equity", is_core=False
            )
            == expected
        )


def test_non_pl_non_core_non_override_defaults_to_bare_us_ticker():
    # Cayman-domiciled ADR listowany na NYSE/Nasdaq w USD — jeden instrument,
    # bez sufiksu (brief P3.1: "akcje US, jeden instrument").
    assert (
        candidate_yahoo_symbol(
            broker_ticker="NU", isin="KYG6683N1034", instrument_type="equity", is_core=False
        )
        == "NU"
    )


def test_future_instrument_type_returns_none_candidate():
    assert (
        candidate_yahoo_symbol(
            broker_ticker="FPGEZ26", isin="PL0GF0034793", instrument_type="future", is_core=False
        )
        is None
    )


def test_certificate_instrument_type_returns_none_candidate():
    assert (
        candidate_yahoo_symbol(
            broker_ticker="INTLALE74484", isin="PLINGNV74484", instrument_type="certificate", is_core=False
        )
        is None
    )


# ---------------------------------------------------------------------------
# futures_base_mapping
# ---------------------------------------------------------------------------


def test_futures_base_mapping_pge_known_multiplier():
    base_symbol, multiplier, note = futures_base_mapping("FPGEZ26")
    assert base_symbol == "PGE.WA"
    assert multiplier == Decimal(1000)
    assert note == ""


def test_futures_base_mapping_cdr_known_multiplier():
    base_symbol, multiplier, note = futures_base_mapping("FCDRZ26")
    assert base_symbol == "CDR.WA"
    assert multiplier == Decimal(100)
    assert note == ""


def test_futures_base_mapping_unknown_base_multiplier_none_and_flagged():
    base_symbol, multiplier, note = futures_base_mapping("FXYZZ26")
    assert base_symbol == "XYZ.WA"
    assert multiplier is None
    assert "XYZ" in note


def test_futures_base_mapping_unrecognized_series_code():
    base_symbol, multiplier, note = futures_base_mapping("NOTAFUTURE")
    assert base_symbol is None
    assert multiplier is None
    assert note != ""


# ---------------------------------------------------------------------------
# normalize_exchange
# ---------------------------------------------------------------------------


def test_normalize_exchange_known_yahoo_names():
    assert normalize_exchange("Warsaw") == "GPW"
    assert normalize_exchange("NasdaqGS") == "NASDAQ"
    assert normalize_exchange("NYSE") == "NYSE"
    assert normalize_exchange("XETRA") == "XETRA"
    assert normalize_exchange("Amsterdam") == "AMSTERDAM"
    assert normalize_exchange("Toronto") == "TSX"


def test_normalize_exchange_unknown_passthrough_and_none():
    assert normalize_exchange("SomeNewExchange") == "SomeNewExchange"
    assert normalize_exchange(None) is None


# ---------------------------------------------------------------------------
# exchange_fallback_from_suffix — Yahoo bywa puste we fullExchangeName
# (empiria P3.1: SHO.WA) mimo poprawnych currency/quoteType.
# ---------------------------------------------------------------------------


def test_exchange_fallback_known_non_us_suffixes():
    assert exchange_fallback_from_suffix("SHO.WA") == "GPW"
    assert exchange_fallback_from_suffix("SPYI.DE") == "XETRA"
    assert exchange_fallback_from_suffix("ASML.AS") == "AMSTERDAM"
    assert exchange_fallback_from_suffix("CSU.TO") == "TSX"


def test_exchange_fallback_bare_us_ticker_is_ambiguous_returns_none():
    # NASDAQ vs NYSE nie da się odróżnić po samym tickerze bez sufiksu.
    assert exchange_fallback_from_suffix("NVDA") is None


def test_exchange_fallback_none_symbol():
    assert exchange_fallback_from_suffix(None) is None


# ---------------------------------------------------------------------------
# B-28: run_instrument_mapping na bazie (transakcja + rollback, zero sieci)
# ---------------------------------------------------------------------------

import pytest

from mannaz import instruments_map
from mannaz.db import get_connection


@pytest.fixture
def map_conn(monkeypatch):
    try:
        conn = get_connection()
    except Exception as exc:
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    conn.rollback()
    monkeypatch.setattr(conn, "commit", lambda: None)  # nic nie utrwalamy
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _insert_instrument(cur, ticker, itype, *, yahoo_symbol=None, multiplier=None, source=None,
                       name="SYNTH", currency="PLN", exchange=None, isin=None):
    cur.execute(
        """
        INSERT INTO instruments (broker_ticker, name, isin, yahoo_symbol, currency, exchange,
                                 instrument_type, multiplier, multiplier_source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
        """,
        (ticker, name, isin, yahoo_symbol, currency, exchange, itype, multiplier, source),
    )
    iid = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO positions_fifo (rachunek, instrument_id, currency, qty, residual_cost,
                                    first_entry_date, last_entry_date)
        VALUES ('__B28_TEST__', %s, %s, 1, 1, DATE '2026-01-02', DATE '2026-01-02')
        """,
        (iid, currency),
    )
    return iid


def _row(cur, iid):
    cur.execute(
        "SELECT yahoo_symbol, currency, exchange, instrument_type, name, base_symbol, "
        "multiplier, multiplier_source FROM instruments WHERE id = %s",
        (iid,),
    )
    return cur.fetchone()


@pytest.fixture
def yf_calls(monkeypatch):
    calls = []

    def fake(symbol):
        calls.append(symbol)
        return instruments_map.YfinanceVerification(
            currency="USD", name="Mock Name", exchange_raw="NYSE", quote_type="EQUITY"
        )

    monkeypatch.setattr(instruments_map, "verify_via_yfinance", fake)
    return calls


@pytest.mark.db
def test_b28_future_multiplier_with_source_unchanged(map_conn, yf_calls):
    with map_conn.cursor() as cur:
        iid = _insert_instrument(cur, "FPGEZ26", "future", multiplier=Decimal(100),
                                 source="sql/007 test")
        summary = instruments_map.run_instrument_mapping(map_conn)
        row = _row(cur, iid)
    assert row[6] == Decimal(100) and row[7] == "sql/007 test"
    assert row[5] == "PGE.WA"  # base_symbol uzupelniony, bo byl NULL
    out = [o for o in summary.mapped if o.instrument_id == iid][0]
    assert out.multiplier == Decimal(100)
    assert iid not in [o.instrument_id for o in summary.flagged_for_owner]


@pytest.mark.db
def test_b28_future_base_outside_override_list_multiplier_not_zeroed(map_conn, yf_calls):
    with map_conn.cursor() as cur:
        iid = _insert_instrument(cur, "FKGHZ26", "future", multiplier=Decimal(100),
                                 source="sql/007 test")
        instruments_map.run_instrument_mapping(map_conn)
        row = _row(cur, iid)
    assert row[6] == Decimal(100) and row[7] == "sql/007 test"


@pytest.mark.db
def test_b28_future_without_multiplier_flagged_not_filled(map_conn, yf_calls):
    with map_conn.cursor() as cur:
        iid = _insert_instrument(cur, "FPGEZ26", "future")
        summary = instruments_map.run_instrument_mapping(map_conn)
        row = _row(cur, iid)
    assert row[6] is None
    assert iid in [o.instrument_id for o in summary.flagged_for_owner]


@pytest.mark.db
def test_b28_equity_without_yahoo_symbol_is_processed(map_conn, yf_calls):
    with map_conn.cursor() as cur:
        iid = _insert_instrument(cur, "B28NEWEQ", "equity", currency="USD")
        instruments_map.run_instrument_mapping(map_conn)
        row = _row(cur, iid)
    assert row[0] == "B28NEWEQ" and row[4] == "Mock Name"
    assert "B28NEWEQ" in yf_calls


@pytest.mark.db
def test_b28_equity_with_yahoo_symbol_untouched(map_conn, yf_calls):
    with map_conn.cursor() as cur:
        iid = _insert_instrument(cur, "B28SETEQ", "equity", yahoo_symbol="B28SETEQ.XX",
                                 currency="EUR", exchange="XETRA", name="Orig Name")
        before = _row(cur, iid)
        instruments_map.run_instrument_mapping(map_conn)
        after = _row(cur, iid)
    assert after == before
    assert "B28SETEQ" not in yf_calls and "B28SETEQ.XX" not in yf_calls


# ---------------------------------------------------------------------------
# CC-OS2 M1: plan_mapping_from_file (czyste) + run_instrument_mapping_from_file
# ---------------------------------------------------------------------------

from mannaz.instruments_map import plan_mapping_from_file, run_instrument_mapping_from_file


def _frow(iid, ticker, yahoo="", exchange="", before="", after=""):
    return {"instrument_id": str(iid), "broker_ticker": ticker, "yahoo_symbol": yahoo,
            "exchange": exchange, "currency_before": before, "currency_after": after}


def _cur(ticker="AAA", yahoo=None, exchange=None, currency="PLN"):
    return {"broker_ticker": ticker, "yahoo_symbol": yahoo, "exchange": exchange, "currency": currency}


def test_plan_null_fields_get_set_positive_control():
    plan = plan_mapping_from_file([_frow(1, "AAA", "AAA.WA", "GPW")], {1: _cur()})
    assert not plan.errors and not plan.conflicts
    got = {(c.instrument_id, c.column, c.before, c.after) for c in plan.changes}
    assert got == {(1, "yahoo_symbol", None, "AAA.WA"), (1, "exchange", None, "GPW")}


def test_plan_instrument_outside_file_untouched():
    plan = plan_mapping_from_file([_frow(1, "AAA", "AAA.WA", "GPW")],
                                  {1: _cur(), 2: _cur("BBB")})
    assert {c.instrument_id for c in plan.changes} == {1}


def test_plan_nonnull_equal_is_unchanged_and_different_is_conflict():
    cur = {1: _cur(yahoo="AAA.WA", exchange="GPW"), 2: _cur("BBB", yahoo="B.DE", exchange="XETRA")}
    plan = plan_mapping_from_file(
        [_frow(1, "AAA", "AAA.WA", "GPW"), _frow(2, "BBB", "BBB.WA", "GPW")], cur)
    assert plan.changes == []
    assert len(plan.unchanged) == 2
    assert len(plan.conflicts) == 2 and not plan.errors


def test_plan_currency_changes_only_when_before_matches():
    plan = plan_mapping_from_file([_frow(1, "AAA", before="PLN", after="USD")], {1: _cur(currency="PLN")})
    assert [(c.column, c.before, c.after) for c in plan.changes] == [("currency", "PLN", "USD")]


def test_plan_currency_conflict_when_before_mismatch():
    plan = plan_mapping_from_file([_frow(1, "AAA", before="EUR", after="USD")], {1: _cur(currency="PLN")})
    assert plan.changes == [] and len(plan.conflicts) == 1


def test_plan_empty_currency_columns_no_change():
    plan = plan_mapping_from_file([_frow(1, "AAA")], {1: _cur(currency="PLN")})
    assert plan.changes == [] and not plan.conflicts and not plan.errors


def test_plan_wrong_ticker_is_error_and_no_changes():
    plan = plan_mapping_from_file([_frow(1, "ZZZ", "AAA.WA", "GPW")], {1: _cur()})
    assert len(plan.errors) == 1 and plan.changes == []


def test_plan_unknown_id_is_error():
    plan = plan_mapping_from_file([_frow(9, "AAA", "AAA.WA", "GPW")], {1: _cur()})
    assert len(plan.errors) == 1 and plan.changes == []


def test_plan_exchange_outside_calendar_map_is_error():
    plan = plan_mapping_from_file([_frow(1, "AAA", "AAA.WA", "MARS")], {1: _cur()})
    assert len(plan.errors) == 1 and plan.changes == []


def test_plan_duplicate_id_is_error_and_no_changes_for_id():
    plan = plan_mapping_from_file(
        [_frow(1, "AAA", "AAA.WA", "GPW"), _frow(1, "AAA", "AAA.WA", "GPW")], {1: _cur()})
    assert len(plan.errors) == 1 and plan.changes == []


def test_plan_only_three_columns_ever_changed():
    plan = plan_mapping_from_file(
        [_frow(1, "AAA", "AAA.WA", "GPW", "PLN", "EUR")], {1: _cur()})
    assert {c.column for c in plan.changes} <= {"yahoo_symbol", "exchange", "currency"}
    assert len(plan.changes) == 3


def test_run_p3_parser_map_from_file_and_dry_run():
    from pathlib import Path
    from mannaz.run_p3 import build_parser

    p = build_parser()
    a = p.parse_args(["map", "--from-file", "x.csv", "--dry-run"])
    assert a.from_file == Path("x.csv") and a.dry_run is True
    b = p.parse_args(["map"])
    assert b.from_file is None and b.dry_run is False


def _write_csv(tmp_path, lines):
    f = tmp_path / "map.csv"
    f.write_text("instrument_id,broker_ticker,yahoo_symbol,exchange,currency_before,currency_after\n"
                 + "\n".join(lines) + "\n", encoding="utf-8")
    return f


@pytest.mark.db
def test_from_file_writes_only_listed_null_fields(map_conn, tmp_path):
    with map_conn.cursor() as cur:
        a = _insert_instrument(cur, "OS2NULL", "equity", name="Orig A", currency="PLN")
        b = _insert_instrument(cur, "OS2SET", "equity", yahoo_symbol="OS2SET.XX", currency="EUR",
                               exchange="XETRA", name="Orig B")
        before_a, before_b = _row(cur, a), _row(cur, b)
        f = _write_csv(tmp_path, [f"{a},OS2NULL,OS2NULL.WA,GPW,,"])
        plan = run_instrument_mapping_from_file(map_conn, f, dry_run=False, commit=False)
        after_a, after_b = _row(cur, a), _row(cur, b)
    assert len(plan.changes) == 2 and not plan.errors and not plan.conflicts
    assert after_a[0] == "OS2NULL.WA" and after_a[2] == "GPW"
    assert (after_a[1], after_a[3:]) == (before_a[1], before_a[3:])
    assert after_b == before_b


@pytest.mark.db
def test_from_file_dry_run_changes_nothing(map_conn, tmp_path):
    with map_conn.cursor() as cur:
        a = _insert_instrument(cur, "OS2DRY", "equity", name="Orig A", currency="PLN")
        before = _row(cur, a)
        f = _write_csv(tmp_path, [f"{a},OS2DRY,OS2DRY.WA,GPW,,"])
        plan = run_instrument_mapping_from_file(map_conn, f, dry_run=True, commit=False)
        after = _row(cur, a)
    assert len(plan.changes) == 2
    assert after == before


@pytest.mark.db
def test_cli_map_from_file_commit_visible_from_new_connection(tmp_path, capsys):
    """Sciezka CLI (run_p3 map --from-file) utrwala zapis: osobne, nowe polaczenie
    widzi zmiane. Jedyny test, ktory commituje — syntetyczny instrument jest
    usuwany (commit) w finally."""
    import uuid

    from mannaz.run_p3 import main as run_p3_main

    try:
        setup = get_connection()
    except Exception as exc:
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return
    ticker = f"OS2CLI{uuid.uuid4().hex[:8].upper()}"
    iid = None
    try:
        with setup.cursor() as cur:
            cur.execute(
                "INSERT INTO instruments (broker_ticker, name, currency, instrument_type) "
                "VALUES (%s, 'SYNTH CLI', 'EUR', 'equity') RETURNING id",
                (ticker,),
            )
            iid = cur.fetchone()[0]
        setup.commit()

        f = _write_csv(tmp_path, [f"{iid},{ticker},{ticker}.WA,GPW,EUR,USD"])
        run_p3_main(["map", "--from-file", str(f)])
        assert "mode: WRITE" in capsys.readouterr().out

        fresh = get_connection()
        try:
            with fresh.cursor() as cur:
                cur.execute("SELECT yahoo_symbol, exchange, currency FROM instruments WHERE id = %s", (iid,))
                assert cur.fetchone() == (f"{ticker}.WA", "GPW", "USD")
        finally:
            fresh.close()
    finally:
        setup.rollback()
        if iid is not None:
            with setup.cursor() as cur:
                cur.execute("DELETE FROM instruments WHERE id = %s AND broker_ticker = %s", (iid, ticker))
            setup.commit()
        setup.close()

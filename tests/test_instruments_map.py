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

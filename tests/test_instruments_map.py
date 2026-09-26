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
    base_symbol, multiplier, note = futures_base_mapping("FMDVZ26")
    assert base_symbol == "MDV.WA"
    assert multiplier is None
    assert "MDV" in note


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

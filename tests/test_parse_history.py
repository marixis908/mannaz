"""Testy parsera historii transakcji — WYŁĄCZNIE na syntetycznych wierszach
(żadnych prawdziwych ilości/kwot/tickerów z data/raw/). Brief CC-P P2.3."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from mannaz.parse_history import (
    CORE_TICKERS,
    classify_instrument_type,
    compute_futures_expiry,
    parse_amount,
    parse_certificate_base_symbol,
    parse_futures_ticker,
    parse_source_date,
    parse_source_file,
    parse_title,
)


# ---------------------------------------------------------------------------
# parsery liczb / dat
# ---------------------------------------------------------------------------


def test_parse_amount_negative_comma_decimal():
    assert parse_amount("-1234,56") == Decimal("-1234.56")


def test_parse_amount_positive():
    assert parse_amount("12,00") == Decimal("12.00")


def test_parse_source_date():
    assert parse_source_date("31.01.2024") == date(2024, 1, 31)


# ---------------------------------------------------------------------------
# compute_futures_expiry — brief CC-P, poprawka P2.5 (kontrakty)
# ---------------------------------------------------------------------------


def test_futures_expiry_third_friday_quarterly_codes():
    # H=III, M=VI, U=IX, Z=XII — trzeci piątek miesiąca
    assert compute_futures_expiry("FKGHZ25") == date(2025, 12, 19)
    assert compute_futures_expiry("FKGHH26") == date(2026, 3, 20)
    assert compute_futures_expiry("FKGHM26") == date(2026, 6, 19)
    assert compute_futures_expiry("FKGHU26") == date(2026, 9, 18)


def test_futures_expiry_unknown_month_code_returns_none():
    # litery spoza cyklu kwartalnego (np. F=styczeń) — brief nie definiuje reguły
    assert compute_futures_expiry("FKGHF26") is None


def test_futures_expiry_non_futures_ticker_returns_none():
    assert compute_futures_expiry("CDR") is None


# ---------------------------------------------------------------------------
# kupno / sprzedaż — z tickerem
# ---------------------------------------------------------------------------


def test_buy_with_ticker():
    title = (
        "Rozliczenie transakcji kupna: SYNTETYK SA (PLSYNTK00019) SYT "
        "10.000000 x 5.500000 PLN nr 000000000001"
    )
    p = parse_title(title)
    assert p.row_type == "kupno"
    assert p.ticker == "SYT"
    assert p.name == "SYNTETYK SA"
    assert p.isin == "PLSYNTK00019"
    assert p.qty == Decimal("10.000000")
    assert p.price == Decimal("5.500000")
    assert p.title_currency_hint == "PLN"
    assert p.order_no == "000000000001"


def test_sell_with_ticker_foreign():
    title = (
        "Rozliczenie transakcji sprzedaży: Syntetic Corp. (US0000000019) SYNT "
        "3.000000 x 100.000000 USD nr Z00000000001"
    )
    p = parse_title(title)
    assert p.row_type == "sprzedaz"
    assert p.ticker == "SYNT"
    assert p.title_currency_hint == "USD"


# ---------------------------------------------------------------------------
# kupno / sprzedaż — BEZ osobnego tickera (kontrakty, ETF/certyfikaty GPW)
# -> instrument budowany z nazwy, zgodnie z brief P2.3
# ---------------------------------------------------------------------------


def test_buy_future_no_ticker_uses_name_as_ticker():
    title = "Rozliczenie transakcji kupna: FABCM99 (PL0GF0099999) 2.000000 x 10.000000 PLN nr 000000000199"
    p = parse_title(title)
    assert p.row_type == "kupno"
    assert p.ticker == "FABCM99"  # brak osobnego tickera -> nazwa staje się tickerem
    assert p.name == "FABCM99"
    assert p.isin == "PL0GF0099999"
    assert classify_instrument_type(p.name, p.ticker, p.isin) == "future"


def test_buy_gpw_etf_no_ticker_uses_name_as_ticker():
    title = "Rozliczenie transakcji kupna: ETFSYNPL (PLSYNET00011) 50.000000 x 12.000000 PLN nr 000000000042"
    p = parse_title(title)
    assert p.ticker == "ETFSYNPL"
    assert classify_instrument_type(p.name, p.ticker, p.isin) == "etf"


def test_buy_certificate_no_ticker():
    title = "Rozliczenie transakcji kupna: INTLSYN12345 (PLINGNV12345) 100.000000 x 3.000000 PLN nr 000000000007"
    p = parse_title(title)
    assert p.ticker == "INTLSYN12345"
    assert classify_instrument_type(p.name, p.ticker, p.isin) == "certificate"
    assert parse_certificate_base_symbol(p.ticker, p.isin) == "SYN"


# ---------------------------------------------------------------------------
# dywidendy / podatek
# ---------------------------------------------------------------------------


def test_dividend_netto():
    p = parse_title("Wypłata dywidendy netto SYT 70% USD")
    assert p.row_type == "dywidenda_netto"
    assert p.ticker == "SYT"
    assert p.extra["retention_pct"] == 70
    assert p.title_currency_hint == "USD"


def test_dividend_brutto():
    p = parse_title("Wypłata dywidendy brutto SYT EUR")
    assert p.row_type == "dywidenda_brutto"
    assert p.ticker == "SYT"


def test_dividend_gpw_plain():
    p = parse_title("Wypłata dywidendy SYNTETYK")
    assert p.row_type == "dywidenda_gpw"
    assert p.ticker == "SYNTETYK"


def test_tax_on_dividend():
    p = parse_title("Podatek od odsetek lub dywidendy SYNTETYK")
    assert p.row_type == "podatek_dywidenda"
    assert p.ticker == "SYNTETYK"


# ---------------------------------------------------------------------------
# opłaty
# ---------------------------------------------------------------------------


def test_fee_transaction():
    p = parse_title("Opłata za transakcję (XNAS)")
    assert p.row_type == "oplata_transakcyjna"
    assert p.extra["venue"] == "XNAS"


def test_fee_account():
    p = parse_title("Opłata za rachunek IV 2025")
    assert p.row_type == "oplata_rachunek"
    assert p.extra == {"roman": "IV", "year": 2025}


def test_fee_custody():
    p = parse_title("Opłata za przechowywanie papierów wartościowych II PÓŁR.2024")
    assert p.row_type == "oplata_przechowanie"
    assert p.extra == {"roman": "II", "year": 2024}


def test_fee_maintenance_double_space():
    p = parse_title("Opłata za prowadzenie rachunku -  01/2024")
    assert p.row_type == "oplata_prowadzenie_rachunku"
    assert p.extra == {"month": 1, "year": 2024}


# ---------------------------------------------------------------------------
# depozyt / wygaśnięcie kontraktu
# ---------------------------------------------------------------------------


def test_margin_loss():
    p = parse_title("Dopłata do depozytu - strata FABCM99")
    assert p.row_type == "depozyt_doplata"
    assert p.ticker == "FABCM99"


def test_margin_gain():
    p = parse_title("Zwrot z depozytu - zysk FABCM99")
    assert p.row_type == "depozyt_zwrot"
    assert p.ticker == "FABCM99"


def test_expiry_commission():
    p = parse_title("Prowizja za wygaśnięcie FABCM99 kurs: 12.3400 ilość: 5")
    assert p.row_type == "prowizja_wygasniecie"
    assert p.ticker == "FABCM99"
    assert p.price == Decimal("12.3400")
    assert p.qty == Decimal("5")


def test_futures_ticker_parsing():
    parts = parse_futures_ticker("FABCM99")
    assert parts == {"base_symbol": "ABC", "month_code": "M", "year_2d": "99"}


def test_futures_ticker_rejects_bad_month_code():
    # "I" nie jest w standardowym zestawie kodów miesięcy futures
    assert parse_futures_ticker("FABCI99") is None


# ---------------------------------------------------------------------------
# przelewy
# ---------------------------------------------------------------------------


def test_transfer_internal_cash():
    p = parse_title("Przelew wewnętrzny z rachunku kasowego")
    assert p.row_type == "przelew_wewnetrzny"
    assert p.extra["subtype"] == "kasowego"


def test_transfer_internal_derivatives():
    p = parse_title("Przelew wewnętrzny z rachunku praw pochodnych")
    assert p.extra["subtype"] == "praw pochodnych"


def test_transfer_external():
    p = parse_title("Przelew na zewnątrz")
    assert p.row_type == "przelew_zewnetrzny"


def test_transfer_to_broker():
    p = parse_title("Przelew do DM BOŚ")
    assert p.row_type == "przelew_do_domu_maklerskiego"


# ---------------------------------------------------------------------------
# zdarzenia korporacyjne
# ---------------------------------------------------------------------------


def test_certificate_redemption_is_corporate_event_without_ratio():
    p = parse_title("Wykup certyfikatów INTLSYN12345 (kwota brutto)")
    assert p.row_type == "wykup_certyfikatow"
    assert p.corporate_event == {
        "event_type": "certificate_redemption",
        "ratio": None,
        "ticker": "INTLSYN12345",
    }


def test_share_exchange_is_corporate_event_without_ratio():
    p = parse_title("Zamiana akcji - wyrównanie SYNTETYK")
    assert p.row_type == "zamiana_akcji"
    assert p.corporate_event == {
        "event_type": "share_exchange",
        "ratio": None,
        "ticker": "SYNTETYK",
    }


def test_split_detection_is_generic_not_hardcoded_25_to_1():
    """Kontrolka dodatnia z briefu: parser MUSI wykryć split 25:1 z samej treści
    tytułu, bez tablicy {ticker: ratio}. Używamy syntetycznego tickera (nie
    prawdziwego SPYI) właśnie po to, żeby dowieść, że mechanizm jest ogólny —
    zmiana tickera w tytule nie wymaga zmiany kodu."""
    p = parse_title("Podział akcji ZZZ99 w stosunku 25:1")
    assert p.row_type == "split_akcji"
    assert p.corporate_event["event_type"] == "split"
    assert p.corporate_event["ratio"] == Decimal("25")
    assert p.corporate_event["ticker"] == "ZZZ99"


def test_split_detection_generic_2_to_1():
    p = parse_title("Podział akcji YYY01 w stosunku 2:1")
    assert p.corporate_event["ratio"] == Decimal("2")


def test_reverse_split_detection_generic():
    p = parse_title("Scalenie akcji XXX02 w stosunku 1:5")
    assert p.row_type == "scalenie_akcji"
    assert p.corporate_event["event_type"] == "reverse_split"
    assert p.corporate_event["ratio"] == Decimal("1") / Decimal("5")


# ---------------------------------------------------------------------------
# nieznany tytuł
# ---------------------------------------------------------------------------


def test_unknown_title_falls_back():
    p = parse_title("Zupełnie nowy typ operacji którego nie znamy 123")
    assert p.row_type == "unknown"
    assert p.ticker is None
    assert p.corporate_event is None


# ---------------------------------------------------------------------------
# klasyfikacja instrumentu
# ---------------------------------------------------------------------------


def test_classify_equity_default():
    assert classify_instrument_type("Syntetic Corp.", "SYNT", "US0000000019") == "equity"


def test_classify_etf_by_name():
    assert classify_instrument_type("Syntetic World UCITS ETF", "SWLD", "IE0000000011") == "etf"


def test_core_tickers_flag():
    assert "SPYI" in CORE_TICKERS
    assert "V80A" in CORE_TICKERS
    assert "V60A" in CORE_TICKERS
    assert "APH" not in CORE_TICKERS


# ---------------------------------------------------------------------------
# czytanie pliku CSV: BOM, separator, dwa warianty nagłówka, dedup wewnątrz pliku
# ---------------------------------------------------------------------------


SYNTHETIC_CSV_A = (
    "Data;Rachunek;Waluta;Tytuł przelewu;Wartość\n"
    "01.02.2024;TESTOWY 000001;PLN;Rozliczenie transakcji kupna: SYNTETYK SA "
    "(PLSYNTK00019) SYT 10.000000 x 5.500000 PLN nr 000000000001;-55,00\n"
    "02.02.2024;TESTOWY 000001;PLN;Opłata za rachunek I 2024;-10,00\n"
    "02.02.2024;TESTOWY 000001;PLN;Opłata za rachunek I 2024;-10,00\n"  # prawdziwy duplikat WEWNĄTRZ pliku
)

SYNTHETIC_CSV_B_HEADER = (
    "Data;Rachunek;Waluta;Tytuł operacji;Wartość\n"
    "03.02.2024;TESTOWY 000001;PLN;Przelew na zewnątrz;-100,00\n"
)


def _write_bom_csv(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
    return p


def test_parse_source_file_variant_a_header_and_occurrence_numbering(tmp_path):
    path = _write_bom_csv(tmp_path, "synthetic_a.csv", SYNTHETIC_CSV_A)
    result = parse_source_file(path)
    assert result.row_count == 3
    assert len(result.sha256) == 64
    occ_nos = [r.occurrence_no for r in result.rows if r.title_raw == "Opłata za rachunek I 2024"]
    assert sorted(occ_nos) == [1, 2]  # dwa identyczne wiersze -> occurrence 1 i 2
    buy_row = result.rows[0]
    assert buy_row.parsed.row_type == "kupno"
    assert buy_row.parsed.ticker == "SYT"
    assert result.unknown_titles == []


def test_parse_source_file_variant_b_header(tmp_path):
    path = _write_bom_csv(tmp_path, "synthetic_b.csv", SYNTHETIC_CSV_B_HEADER)
    result = parse_source_file(path)
    assert result.row_count == 1
    assert result.rows[0].parsed.row_type == "przelew_zewnetrzny"


def test_parse_source_file_detects_unknown_titles(tmp_path):
    content = (
        "Data;Rachunek;Waluta;Tytuł przelewu;Wartość\n"
        "01.02.2024;TESTOWY 000001;PLN;Coś czego parser nie zna;-1,00\n"
    )
    path = _write_bom_csv(tmp_path, "synthetic_unknown.csv", content)
    result = parse_source_file(path)
    assert result.unknown_titles == ["Coś czego parser nie zna"]

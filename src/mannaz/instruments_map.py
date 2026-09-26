"""Mapowanie instruments.yahoo_symbol / currency / exchange / instrument_type /
name dla WSZYSTKICH instrumentów z otwartymi pozycjami (`positions_fifo`) oraz
rdzenia (`is_core`). Brief CC-P, P3.1.

Zakres celowo węższy niż "wszystkie instrumenty w tabeli" — `instruments` zawiera
też walory bez żadnej otwartej pozycji (sprzedane w całości, albo instrumenty
utworzone z wierszy opłat/dywidend bez własnego kupna); te nie są tu dotykane.

Reguły przypisania symbolu Yahoo (bez zgadywania — jeśli reguła nie pasuje,
instrument zostaje NIEZMAPOWANY i jest zwracany w raporcie, nigdy po cichu
pominięty):

  1. `EXPLICIT_SYMBOL_OVERRIDES` — jawna lista europejska/kanadyjska z briefu
     (ASML, ADYEN, BESI, IFX, CSU) — ISIN tych spółek nie zaczyna się od "PL"
     i nie są rdzeniem, więc bez tej listy trafiłyby w regułę US-domyślną.
  2. `is_core` (SPYI/V80A/V60A) -> `<ticker>.DE` (Xetra, EUR) — zweryfikowane
     empirycznie w P2.3 (SPYI) i tu ponownie przez yfinance dla całej trójki.
  3. ISIN zaczyna się od "PL" (GPW) -> `<ticker>.WA`.
  4. W pozostałych przypadkach (equity/etf spoza PL, spoza listy 1./2., w tym
     ADR-y i spółki zarejestrowane poza USA ale notowane na NYSE/Nasdaq w USD,
     np. Cayman/UK/Kanada-jako-ADR) -> ticker AMERYKAŃSKI bez sufiksu. Jeden
     instrument, niezależnie od waluty ROZLICZENIA konkretnej pozycji (EUR czy
     USD) — `positions_fifo.currency` niesie walutę rozliczenia osobno.

Kontrakty terminowe (`instrument_type = 'future'`) NIE dostają `yahoo_symbol`
(GPW derywaty nie mają odpowiednika na Yahoo) — zamiast tego `base_symbol`
(spółka bazowa + `.WA`) i `multiplier`, jeśli znany z `FUTURES_MULTIPLIER_OVERRIDES`
(brief podaje tylko PGE i CDR; inne bazy zostają z `multiplier IS NULL` i trafiają
do `flagged_for_owner`).

Certyfikaty strukturyzowane (`instrument_type = 'certificate'`, np. ING Turbo
`INTL*`/`INTS*`) nie mają reguły w briefie -> zawsze niezmapowane.

Weryfikacja przez yfinance: WYŁĄCZNIE `currency` + `longName`/`shortName` z
`Ticker(...).info`. `Ticker(...).isin` jest odrzucony jako źródło — zwraca "-"
dla większości symboli i BŁĘDNE dopasowania dla części (empiria P3.1: ASML.AS ->
"AR0725224551", ADYEN.AS -> "JP3122380003", żadne z nich nie zgadza się z ISIN
zapisanym z tytułu transakcji brokera).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import psycopg
import yfinance as yf

from mannaz.parse_history import parse_futures_ticker

# ---------------------------------------------------------------------------
# Reguły / stałe (brief P3.1)
# ---------------------------------------------------------------------------

EXPLICIT_SYMBOL_OVERRIDES: dict[str, str] = {
    "CSU": "CSU.TO",
    "ASML": "ASML.AS",
    "ADYEN": "ADYEN.AS",
    "BESI": "BESI.AS",
    "IFX": "IFX.DE",
}

# Instrumenty, dla których yfinance longName jest zwracane w raporcie do
# jawnego potwierdzenia tożsamości przez ownera (brief P3.1), niezależnie od
# tego, że weryfikacja waluty/nazwy wypadła pozytywnie.
IDENTITY_CONFIRMATION_REQUIRED = frozenset({"MDV", "GMT"})

# Multiplier kontraktów terminowych GPW, znany z briefu tylko dla dwóch baz;
# reszta zostaje NULL i jest zgłaszana do ownera (brak zgadywania).
FUTURES_MULTIPLIER_OVERRIDES: dict[str, Decimal | None] = {
    "PGE": Decimal(1000),
    "CDR": Decimal(100),
}

# Normalizacja fullExchangeName z yfinance do krótkich kodów używanych też w
# P3.4 (calendar_check.py) do doboru kalendarza exchange_calendars.
EXCHANGE_FULLNAME_TO_CODE: dict[str, str] = {
    "Warsaw": "GPW",
    "NasdaqGS": "NASDAQ",
    "NasdaqGM": "NASDAQ",
    "NasdaqCM": "NASDAQ",
    "NYSE": "NYSE",
    "XETRA": "XETRA",
    "GER": "XETRA",
    "Amsterdam": "AMSTERDAM",
    "Toronto": "TSX",
}


def normalize_exchange(full_exchange_name: str | None) -> str | None:
    if full_exchange_name is None:
        return None
    return EXCHANGE_FULLNAME_TO_CODE.get(full_exchange_name, full_exchange_name)


# Yahoo nie zawsze wypełnia `fullExchangeName` (empiria P3.1: SHO.WA ->
# quoteType EQUITY / currency PLN poprawne, ale fullExchangeName=None).
# Sufiks symbolu jest w tych przypadkach jednoznaczny dla giełd spoza USA —
# fallback WYŁĄCZNIE dla tych sufiksów (US bez sufiksu zostaje pominięty:
# NASDAQ vs NYSE nie da się odróżnić po samym tickerze).
SYMBOL_SUFFIX_EXCHANGE_FALLBACK: dict[str, str] = {
    ".WA": "GPW",
    ".DE": "XETRA",
    ".AS": "AMSTERDAM",
    ".TO": "TSX",
}


def exchange_fallback_from_suffix(symbol: str | None) -> str | None:
    if not symbol:
        return None
    for suffix, code in SYMBOL_SUFFIX_EXCHANGE_FALLBACK.items():
        if symbol.endswith(suffix):
            return code
    return None


# ---------------------------------------------------------------------------
# Reguła kandydata symbolu Yahoo (equity/etf) — czysta funkcja, testowalna
# ---------------------------------------------------------------------------


def candidate_yahoo_symbol(
    *, broker_ticker: str, isin: str | None, instrument_type: str, is_core: bool
) -> str | None:
    """Zwraca kandydata na symbol Yahoo dla equity/etf, albo None dla
    instrument_type w ('future', 'certificate') — te mają osobną ścieżkę
    (base_symbol / brak reguły)."""
    if instrument_type in ("future", "certificate"):
        return None
    if broker_ticker in EXPLICIT_SYMBOL_OVERRIDES:
        return EXPLICIT_SYMBOL_OVERRIDES[broker_ticker]
    if is_core:
        return f"{broker_ticker}.DE"
    if isin and isin.startswith("PL"):
        return f"{broker_ticker}.WA"
    return broker_ticker


def futures_base_mapping(broker_ticker: str) -> tuple[str | None, Decimal | None, str]:
    """Dla kontraktu terminowego GPW: (base_symbol, multiplier, note).
    note niepusta gdy multiplier nieznany (do potwierdzenia przez ownera) albo
    gdy kod serii nierozpoznany."""
    info = parse_futures_ticker(broker_ticker)
    if info is None:
        return None, None, "nie rozpoznano kodu serii kontraktu (parse_futures_ticker)"
    base = info["base_symbol"]
    base_symbol = f"{base}.WA"
    multiplier = FUTURES_MULTIPLIER_OVERRIDES.get(base)
    note = "" if multiplier is not None else (
        f"multiplier nieznany dla bazy {base} — brief podaje tylko PGE=1000/CDR=100, "
        f"do potwierdzenia przez ownera"
    )
    return base_symbol, multiplier, note


# ---------------------------------------------------------------------------
# Weryfikacja przez yfinance
# ---------------------------------------------------------------------------


@dataclass
class YfinanceVerification:
    currency: str | None
    name: str | None
    exchange_raw: str | None
    quote_type: str | None


def verify_via_yfinance(symbol: str) -> YfinanceVerification | None:
    """None gdy yfinance nie zwrócił użytecznych danych (brak `currency`) albo
    wyjątek sieciowy/parsowania — traktowane identycznie jako "brak weryfikacji",
    nigdy nie zgadujemy dalej."""
    try:
        info = yf.Ticker(symbol).info
    except Exception:
        return None
    if not info:
        return None
    currency = info.get("currency")
    if not currency:
        return None
    name = info.get("longName") or info.get("shortName")
    return YfinanceVerification(
        currency=currency,
        name=name,
        exchange_raw=info.get("fullExchangeName"),
        quote_type=info.get("quoteType"),
    )


# ---------------------------------------------------------------------------
# Orkiestracja / DB
# ---------------------------------------------------------------------------


@dataclass
class MappingOutcome:
    instrument_id: int
    broker_ticker: str
    yahoo_symbol: str | None = None
    base_symbol: str | None = None
    multiplier: Decimal | None = None
    currency: str | None = None
    exchange: str | None = None
    instrument_type: str | None = None
    name: str | None = None
    note: str = ""


@dataclass
class MappingSummary:
    mapped: list[MappingOutcome] = field(default_factory=list)
    unmapped: list[MappingOutcome] = field(default_factory=list)
    flagged_for_owner: list[MappingOutcome] = field(default_factory=list)


def _target_instruments(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT DISTINCT i.id, i.broker_ticker, i.isin, i.currency, i.instrument_type,
               i.name, i.is_core
        FROM instruments i
        WHERE i.is_core
           OR EXISTS (SELECT 1 FROM positions_fifo p WHERE p.instrument_id = i.id)
        ORDER BY i.id
        """
    )
    cols = ("id", "broker_ticker", "isin", "currency", "instrument_type", "name", "is_core")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# quoteType z yfinance -> instrument_type w naszej bazie. Nadpisujemy tylko gdy
# yfinance mówi EQUITY/ETF (dane jednoznaczne) — nigdy nie nadpisujemy
# future/certificate (Yahoo o nich nic nie wie).
_QUOTE_TYPE_TO_INSTRUMENT_TYPE = {"EQUITY": "equity", "ETF": "etf"}


def run_instrument_mapping(conn: psycopg.Connection) -> MappingSummary:
    summary = MappingSummary()

    with conn.cursor() as cur:
        instruments = _target_instruments(cur)

        for inst in instruments:
            instrument_id = inst["id"]
            ticker = inst["broker_ticker"]
            instrument_type = inst["instrument_type"]

            if instrument_type == "future":
                base_symbol, multiplier, note = futures_base_mapping(ticker)
                cur.execute(
                    "UPDATE instruments SET base_symbol = %s, multiplier = %s WHERE id = %s",
                    (base_symbol, multiplier, instrument_id),
                )
                outcome = MappingOutcome(
                    instrument_id=instrument_id,
                    broker_ticker=ticker,
                    base_symbol=base_symbol,
                    multiplier=multiplier,
                    instrument_type=instrument_type,
                    note=note,
                )
                summary.mapped.append(outcome)
                if note:
                    summary.flagged_for_owner.append(outcome)
                continue

            if instrument_type == "certificate":
                outcome = MappingOutcome(
                    instrument_id=instrument_id,
                    broker_ticker=ticker,
                    instrument_type=instrument_type,
                    note="certyfikat strukturyzowany — brak reguły symbolu Yahoo w briefie P3.1",
                )
                summary.unmapped.append(outcome)
                continue

            candidate = candidate_yahoo_symbol(
                broker_ticker=ticker,
                isin=inst["isin"],
                instrument_type=instrument_type,
                is_core=inst["is_core"],
            )
            verification = verify_via_yfinance(candidate) if candidate else None

            if verification is None:
                outcome = MappingOutcome(
                    instrument_id=instrument_id,
                    broker_ticker=ticker,
                    yahoo_symbol=candidate,
                    note="yfinance: brak danych / wyjątek przy weryfikacji kandydata",
                )
                summary.unmapped.append(outcome)
                continue

            new_instrument_type = _QUOTE_TYPE_TO_INSTRUMENT_TYPE.get(
                verification.quote_type, instrument_type
            )
            new_exchange = normalize_exchange(verification.exchange_raw)
            if new_exchange is None:
                new_exchange = exchange_fallback_from_suffix(candidate)

            cur.execute(
                """
                UPDATE instruments
                SET yahoo_symbol = %s, currency = %s, exchange = %s,
                    instrument_type = %s, name = %s
                WHERE id = %s
                """,
                (
                    candidate,
                    verification.currency,
                    new_exchange,
                    new_instrument_type,
                    verification.name,
                    instrument_id,
                ),
            )

            note = ""
            if ticker in IDENTITY_CONFIRMATION_REQUIRED:
                note = (
                    f"tozsamosc do potwierdzenia przez ownera: yfinance longName={verification.name!r}"
                )

            outcome = MappingOutcome(
                instrument_id=instrument_id,
                broker_ticker=ticker,
                yahoo_symbol=candidate,
                currency=verification.currency,
                exchange=new_exchange,
                instrument_type=new_instrument_type,
                name=verification.name,
                note=note,
            )
            summary.mapped.append(outcome)
            if note:
                summary.flagged_for_owner.append(outcome)

        conn.commit()

    return summary

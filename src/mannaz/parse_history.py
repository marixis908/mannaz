"""Parser historii przepływów gotówki (CSV eksportowany z platformy brokerskiej).

Zakres (brief CC-P, P2.3):
  - czytanie CSV UTF-8 z BOM, separator ';', dwa warianty nagłówka tytułu
    ("Tytuł przelewu" / "Tytuł operacji"),
  - klasyfikacja każdego wiersza po treści tytułu (kupno/sprzedaż/dywidenda/
    podatek/opłata/depozyt/przelew/zdarzenie korporacyjne/nieznany),
  - wykrywanie instrumentu z tytułu (ticker + ISIN, albo — dla kontraktów
    terminowych i części ETF/certyfikatów GPW, gdzie w tytule nie ma osobnego
    tickera — z samej nazwy),
  - wykrywanie zdarzeń korporacyjnych (split/scalenie/zamiana akcji/wykup
    certyfikatów) i próba wyliczenia współczynnika z treści tytułu.

Ten moduł jest CZYSTY (bez zależności od bazy) — cała logika parsowania jest
testowalna na syntetycznych wierszach (patrz tests/test_parse_history.py).
Import do Postgresa (P2.4) i dedup między plikami żyje w import_history.py,
bo wymaga stanu (rejestr instrumentów, źródła już zaimportowane).

WAŻNE — wykrywanie splitów/scaleń NIE jest hardkodowane pod konkretne tickery.
`SPLIT_RE` / `REVERSE_SPLIT_RE` to ogólny wzorzec tekstowy (viz. docstring przy
tych stałych) — współczynnik zawsze wynika z liczb w tytule, nigdy z tablicy
{ticker: ratio}. Zob. `_infer_corporate_event()`.
"""

from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Stałe / nagłówki
# ---------------------------------------------------------------------------

EXPECTED_COLUMNS_A = ("Data", "Rachunek", "Waluta", "Tytuł przelewu", "Wartość")
EXPECTED_COLUMNS_B = ("Data", "Rachunek", "Waluta", "Tytuł operacji", "Wartość")

# Instrumenty rdzenia satelity (brief P2.2) — flaga biznesowa is_core, NIE ma
# nic wspólnego z detekcją splitów/scaleń.
CORE_TICKERS = frozenset({"SPYI", "V80A", "V60A"})

FUTURES_MONTH_CODES = frozenset("FGHJKMNQUVXZ")

# ---------------------------------------------------------------------------
# Regexy klasyfikujące tytuł
# ---------------------------------------------------------------------------

BUY_SELL_RE = re.compile(
    r"^Rozliczenie transakcji (?P<direction>kupna|sprzedaży): "
    r"(?P<name>.+?) \((?P<isin>[A-Z0-9]{9,14})\)"
    r"(?: (?P<ticker>[A-Z0-9][A-Z0-9\-]*))?"
    r" (?P<qty>\d+(?:\.\d+)?) x (?P<price>\d+(?:\.\d+)?) (?P<ccy>[A-Z]{3})"
    r" nr (?P<order>\S+)$"
)

DIV_NETTO_RE = re.compile(
    r"^Wypłata dywidendy netto (?P<ticker>[A-Z0-9]+) (?P<pct>\d+)% (?P<ccy>[A-Z]{3})$"
)
DIV_BRUTTO_RE = re.compile(
    r"^Wypłata dywidendy brutto (?P<ticker>[A-Z0-9]+) (?P<ccy>[A-Z]{3})$"
)
DIV_PLAIN_RE = re.compile(r"^Wypłata dywidendy (?P<ticker>[A-Z0-9]+)$")
TAX_DIV_RE = re.compile(
    r"^Podatek od odsetek lub dywidendy (?P<ticker>[A-Z0-9]+)$"
)
FEE_TXN_RE = re.compile(r"^Opłata za transakcję \((?P<venue>[A-Z]+)\)$")
FEE_ACCOUNT_RE = re.compile(
    r"^Opłata za rachunek (?P<roman>[IVXLCDM]+) (?P<year>\d{4})$"
)
FEE_CUSTODY_RE = re.compile(
    r"^Opłata za przechowywanie papierów wartościowych "
    r"(?P<roman>[IVXLCDM]+) PÓŁR\.(?P<year>\d{4})$"
)
FEE_MAINTENANCE_RE = re.compile(
    r"^Opłata za prowadzenie rachunku - {1,2}(?P<month>\d{2})/(?P<year>\d{4})$"
)
MARGIN_LOSS_RE = re.compile(r"^Dopłata do depozytu - strata (?P<ticker>[A-Z0-9]+)$")
MARGIN_GAIN_RE = re.compile(r"^Zwrot z depozytu - zysk (?P<ticker>[A-Z0-9]+)$")
EXPIRY_FEE_RE = re.compile(
    r"^Prowizja za wygaśnięcie (?P<ticker>[A-Z0-9]+) kurs: (?P<price>[\d.]+) "
    r"ilość: (?P<qty>\d+)$"
)
TRANSFER_INTERNAL_RE = re.compile(
    r"^Przelew wewnętrzny z rachunku "
    r"(?P<subtype>kasowego zagranicznego|kasowego|praw pochodnych)$"
)
TRANSFER_EXTERNAL_RE = re.compile(r"^Przelew na zewnątrz$")
TRANSFER_TO_BROKER_RE = re.compile(r"^Przelew do DM BOŚ$")

# --- zdarzenia korporacyjne -------------------------------------------------

CERT_REDEMPTION_RE = re.compile(
    r"^Wykup certyfikatów (?P<ticker>[A-Z0-9]+) \(kwota brutto\)$"
)
SHARE_EXCHANGE_RE = re.compile(
    r"^Zamiana akcji - wyrównanie (?P<ticker>[A-Z0-9]+)$"
)

# Wzorzec ogólny dla splitu / scalenia akcji: "Podział akcji <TICKER> w stosunku
# <N>:<M>" / "Scalenie akcji <TICKER> w stosunku <N>:<M>". W żadnym z 6 dostarczonych
# plików CSV (financeHistory (4)..(9), 2785 wierszy razem) nie znaleziono wiersza
# pasującego do żadnego wzorca korporacyjnego poza "Wykup certyfikatów ..." i
# "Zamiana akcji - wyrównanie SHOPER" — patrz raport P2.3 w handoffie. Ten format
# tekstowy jest udokumentowanym miejscem podłączenia realnego formatu brokera,
# gdy się pojawi w kolejnych eksportach; współczynnik ZAWSZE liczony z liczb N/M
# w tytule, nigdy z tablicy ticker->ratio.
SPLIT_RE = re.compile(
    r"^Podział akcji (?P<ticker>[A-Z0-9]+) w stosunku (?P<a>\d+):(?P<b>\d+)$"
)
REVERSE_SPLIT_RE = re.compile(
    r"^Scalenie akcji (?P<ticker>[A-Z0-9]+) w stosunku (?P<a>\d+):(?P<b>\d+)$"
)


# ---------------------------------------------------------------------------
# Pomocnicze parsery liczb/dat
# ---------------------------------------------------------------------------


def parse_amount(raw: str) -> Decimal:
    """"-4187,54" -> Decimal("-4187.54"). Brak separatora tysięcy w danych źródłowych."""
    cleaned = raw.strip().replace(" ", "").replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise ValueError(f"Nie mogę sparsować kwoty: {raw!r}") from exc


def parse_source_date(raw: str) -> date:
    """"12.10.2023" -> date(2023, 10, 12)."""
    return datetime.strptime(raw.strip(), "%d.%m.%Y").date()


# ---------------------------------------------------------------------------
# Klasyfikacja instrumentu z tickera
# ---------------------------------------------------------------------------


def parse_futures_ticker(ticker: str) -> dict[str, str] | None:
    """FDNPM26 -> {'base_symbol': 'DNP', 'month_code': 'M', 'year_2d': '26'}."""
    m = re.match(r"^F(?P<base>[A-Z]{3})(?P<month>[A-Z])(?P<year>\d{2})$", ticker)
    if not m:
        return None
    if m.group("month") not in FUTURES_MONTH_CODES:
        return None
    return {
        "base_symbol": m.group("base"),
        "month_code": m.group("month"),
        "year_2d": m.group("year"),
    }


FUTURES_QUARTERLY_EXPIRY_MONTHS = {"H": 3, "M": 6, "U": 9, "Z": 12}


def compute_futures_expiry(ticker: str) -> date | None:
    """Data wygaśnięcia kontraktu terminowego z kodu serii (brief CC-P,
    poprawka P2.5): litera miesiąca H=III, M=VI, U=IX, Z=XII + 2-cyfrowy rok,
    trzeci piątek miesiąca. Tylko cykl kwartalny GPW (jedyne litery obecne w
    danych na tym etapie) — inne litery miesięcy, które `parse_futures_ticker`
    toleruje formatowo, nie mają tu zdefiniowanej reguły wygaśnięcia i zwracają
    None (brief nie precyzuje ich daty rozliczenia)."""
    import calendar

    info = parse_futures_ticker(ticker)
    if info is None:
        return None
    month = FUTURES_QUARTERLY_EXPIRY_MONTHS.get(info["month_code"])
    if month is None:
        return None
    year = 2000 + int(info["year_2d"])
    fridays = [
        d
        for d in calendar.Calendar().itermonthdates(year, month)
        if d.month == month and d.weekday() == 4
    ]
    return fridays[2]


def parse_certificate_base_symbol(ticker: str | None, isin: str | None) -> str | None:
    """INTLW2087767 / PLINGNV87767 -> 'W20' (skrót instrumentu bazowego)."""
    if not ticker or not isin or not isin.startswith("PLINGNV"):
        return None
    for prefix in ("INTL", "INTS"):
        if ticker.startswith(prefix):
            suffix = isin[-5:]
            if ticker.endswith(suffix) and len(ticker) > len(prefix) + len(suffix):
                mid = ticker[len(prefix) : -len(suffix)]
                return mid or None
    return None


def classify_instrument_type(name: str | None, ticker: str | None, isin: str | None) -> str:
    if isin and isin.startswith("PL0GF"):
        return "future"
    if isin and isin.startswith("PLINGNV"):
        return "certificate"
    if ticker and parse_futures_ticker(ticker) is not None and isin is None:
        return "future"
    haystack = f"{name or ''} {ticker or ''}".upper()
    if "ETF" in haystack:
        return "etf"
    return "equity"


# ---------------------------------------------------------------------------
# Wynik parsowania jednego tytułu
# ---------------------------------------------------------------------------


@dataclass
class ParsedTitle:
    row_type: str
    ticker: str | None = None
    name: str | None = None
    isin: str | None = None
    qty: Decimal | None = None
    price: Decimal | None = None
    order_no: str | None = None
    title_currency_hint: str | None = None
    corporate_event: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _infer_corporate_event(title: str) -> dict[str, Any] | None:
    """Zwraca {'event_type', 'ratio', 'ticker'} albo None. ratio może być None,
    gdy tytuł nie pozwala jednoznacznie wyliczyć współczynnika (np. samo
    "Zamiana akcji - wyrównanie X" bez liczb — wymaga ręcznej weryfikacji)."""
    m = SPLIT_RE.match(title)
    if m:
        a, b = Decimal(m.group("a")), Decimal(m.group("b"))
        return {"event_type": "split", "ratio": a / b, "ticker": m.group("ticker")}
    m = REVERSE_SPLIT_RE.match(title)
    if m:
        a, b = Decimal(m.group("a")), Decimal(m.group("b"))
        return {"event_type": "reverse_split", "ratio": a / b, "ticker": m.group("ticker")}
    m = CERT_REDEMPTION_RE.match(title)
    if m:
        return {"event_type": "certificate_redemption", "ratio": None, "ticker": m.group("ticker")}
    m = SHARE_EXCHANGE_RE.match(title)
    if m:
        return {"event_type": "share_exchange", "ratio": None, "ticker": m.group("ticker")}
    return None


def parse_title(title: str) -> ParsedTitle:
    title = title.strip()

    m = BUY_SELL_RE.match(title)
    if m:
        direction = m.group("direction")
        row_type = "kupno" if direction == "kupna" else "sprzedaz"
        ticker = m.group("ticker")
        name = m.group("name")
        return ParsedTitle(
            row_type=row_type,
            ticker=ticker or name,  # brak osobnego tickera (kontrakty, ETF/certy GPW) -> instrument z nazwy
            name=name,
            isin=m.group("isin"),
            qty=Decimal(m.group("qty")),
            price=Decimal(m.group("price")),
            order_no=m.group("order"),
            title_currency_hint=m.group("ccy"),
        )

    m = DIV_NETTO_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="dywidenda_netto",
            ticker=m.group("ticker"),
            title_currency_hint=m.group("ccy"),
            extra={"retention_pct": int(m.group("pct"))},
        )

    m = DIV_BRUTTO_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="dywidenda_brutto",
            ticker=m.group("ticker"),
            title_currency_hint=m.group("ccy"),
        )

    m = DIV_PLAIN_RE.match(title)
    if m:
        return ParsedTitle(row_type="dywidenda_gpw", ticker=m.group("ticker"))

    m = TAX_DIV_RE.match(title)
    if m:
        return ParsedTitle(row_type="podatek_dywidenda", ticker=m.group("ticker"))

    m = FEE_TXN_RE.match(title)
    if m:
        return ParsedTitle(row_type="oplata_transakcyjna", extra={"venue": m.group("venue")})

    m = FEE_ACCOUNT_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="oplata_rachunek",
            extra={"roman": m.group("roman"), "year": int(m.group("year"))},
        )

    m = FEE_CUSTODY_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="oplata_przechowanie",
            extra={"roman": m.group("roman"), "year": int(m.group("year"))},
        )

    m = FEE_MAINTENANCE_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="oplata_prowadzenie_rachunku",
            extra={"month": int(m.group("month")), "year": int(m.group("year"))},
        )

    m = MARGIN_LOSS_RE.match(title)
    if m:
        return ParsedTitle(row_type="depozyt_doplata", ticker=m.group("ticker"))

    m = MARGIN_GAIN_RE.match(title)
    if m:
        return ParsedTitle(row_type="depozyt_zwrot", ticker=m.group("ticker"))

    m = EXPIRY_FEE_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="prowizja_wygasniecie",
            ticker=m.group("ticker"),
            price=Decimal(m.group("price")),
            qty=Decimal(m.group("qty")),
        )

    m = TRANSFER_INTERNAL_RE.match(title)
    if m:
        return ParsedTitle(row_type="przelew_wewnetrzny", extra={"subtype": m.group("subtype")})

    if TRANSFER_EXTERNAL_RE.match(title):
        return ParsedTitle(row_type="przelew_zewnetrzny")

    if TRANSFER_TO_BROKER_RE.match(title):
        return ParsedTitle(row_type="przelew_do_domu_maklerskiego")

    m = CERT_REDEMPTION_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="wykup_certyfikatow",
            ticker=m.group("ticker"),
            corporate_event=_infer_corporate_event(title),
        )

    m = SHARE_EXCHANGE_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="zamiana_akcji",
            ticker=m.group("ticker"),
            corporate_event=_infer_corporate_event(title),
        )

    m = SPLIT_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="split_akcji",
            ticker=m.group("ticker"),
            corporate_event=_infer_corporate_event(title),
        )

    m = REVERSE_SPLIT_RE.match(title)
    if m:
        return ParsedTitle(
            row_type="scalenie_akcji",
            ticker=m.group("ticker"),
            corporate_event=_infer_corporate_event(title),
        )

    return ParsedTitle(row_type="unknown")


# ---------------------------------------------------------------------------
# Czytanie pliku źródłowego
# ---------------------------------------------------------------------------


@dataclass
class RawRow:
    transaction_date: date
    rachunek: str
    currency: str
    title_raw: str
    amount: Decimal
    occurrence_no: int  # numer wystąpienia identycznej krotki WEWNĄTRZ pliku
    source_file: str
    source_sha256: str
    parsed: ParsedTitle


@dataclass
class ParsedFile:
    path: Path
    sha256: str
    row_count: int
    rows: list[RawRow]
    unknown_titles: list[str]


def _detect_title_column(fieldnames: list[str]) -> str:
    if "Tytuł przelewu" in fieldnames:
        return "Tytuł przelewu"
    if "Tytuł operacji" in fieldnames:
        return "Tytuł operacji"
    raise ValueError(f"Nieznany nagłówek CSV, brak kolumny tytułu: {fieldnames!r}")


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_source_file(path: Path) -> ParsedFile:
    path = Path(path)
    sha256 = _sha256_of_file(path)
    rows: list[RawRow] = []
    unknown_titles: list[str] = []
    occurrence_counter: dict[tuple, int] = {}

    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=";")
        if reader.fieldnames is None:
            raise ValueError(f"Pusty plik lub brak nagłówka: {path}")
        titlecol = _detect_title_column(list(reader.fieldnames))

        for row in reader:
            tdate = parse_source_date(row["Data"])
            rachunek = row["Rachunek"].strip()
            currency = row["Waluta"].strip()
            title_raw = row[titlecol]
            amount = parse_amount(row["Wartość"])

            key = (tdate, rachunek, currency, title_raw, amount)
            occurrence_counter[key] = occurrence_counter.get(key, 0) + 1
            occurrence_no = occurrence_counter[key]

            parsed = parse_title(title_raw)
            if parsed.row_type == "unknown":
                unknown_titles.append(title_raw)

            rows.append(
                RawRow(
                    transaction_date=tdate,
                    rachunek=rachunek,
                    currency=currency,
                    title_raw=title_raw,
                    amount=amount,
                    occurrence_no=occurrence_no,
                    source_file=path.name,
                    source_sha256=sha256,
                    parsed=parsed,
                )
            )

    return ParsedFile(
        path=path,
        sha256=sha256,
        row_count=len(rows),
        rows=rows,
        unknown_titles=unknown_titles,
    )

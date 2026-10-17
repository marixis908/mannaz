"""Ceny dzienne (`prices_daily`) z yfinance — brief CC-P, P3.2.

Konwencja kolumn (patrz też komentarz tabeli `prices_daily` w `sql/001_schema.sql`):

  - `*_split_adj` = Open/High/Low/Close z yfinance (`auto_adjust=False,
    actions=True`) DOKŁADNIE TAK, JAK ZWRACA JE YAHOO. UWAGA [Z] ustalona w
    P2.3 (`corp_actions.py`): mimo `auto_adjust=False` Yahoo bywa WEWNĘTRZNIE
    już split-adjusted retroaktywnie dla instrumentów bez odpowiadającego
    rekordu w `Ticker.splits` (empiria: SPYI.DE ~7 EUR w 2023-10 wobec zapisu
    brokera ~170 EUR — Yahoo nie ma śladu tego splitu w API, ale sama seria
    Close jest mimo to podzielona). Ta kolumna jest więc ZAWSZE "tak jak dziś
    widzi to Yahoo", nigdy nie jest gwarancją "ceny z dnia sesji" bez
    odniesienia do `corporate_events`.

  - `*_raw` = odtworzone cofnięciem znanych zdarzeń korporacyjnych
    (`corporate_events`, `ratio IS NOT NULL`, `event_type IN ('split',
    'reverse_split')`): dla sesji z datą wcześniejszą niż `event_date`
    mnożymy `*_split_adj` przez iloczyn `ratio` wszystkich takich zdarzeń
    O DACIE PÓŹNIEJSZEJ niż ta sesja (patrz `cumulative_ratio_after` —
    kierunek wynika z tego, że Yahoo dzieli historyczne ceny przez `ratio`
    przy KAŻDYM kolejnym splicie, żeby seria była ciągła; cofnięcie = mnożenie
    przez te same ratio). Cel: `*_raw` ma odpowiadać temu, co broker faktycznie
    zapisał w dniu transakcji. Gdy dla instrumentu nie ma żadnych zdarzeń ze
    znanym `ratio`, `*_raw == *_split_adj` (brak czego cofać).

  - `adj_close_total_return` = kolumna "Adj Close" z yfinance (dywidendy +
    splity). BEZ dalszej korekty — brief: "dywidendy nie korygowane" —
    to jest osobna warstwa total-return, nie mieszamy jej z `*_raw`/`*_split_adj`.

  - `adjustment_convention` — jawny opis: `'yahoo_split_adjusted'` gdy
    instrument NIE MA żadnych zdarzeń ze znanym ratio (raw == split_adj z
    definicji, nic nie odtwarzaliśmy), `'raw_reconstructed_from_corporate_events'`
    gdy przynajmniej jedno zdarzenie ze znanym ratio zostało użyte do
    odtworzenia `*_raw` dla co najmniej jednej sesji.

  - Wolumen: WYŁĄCZNIE `volume_split_adj` (Volume z Yahoo, bez zmian).
    `volume_raw` CELOWO zostaje NULL na tym etapie — brief nie precyzuje
    konwencji odwrócenia wolumenu przy splicie (kierunek przeciwny do ceny,
    ale Yahoo nie zawsze retroaktywnie koryguje wolumen wcale — nie ma tu
    empirycznego potwierdzenia analogicznego do `*_split_adj` cen), więc NULL
    zamiast zgadywanej wartości.

T7 (asercja waluty, §T7 dokumentu projektowego): `check_currency` — GBp jest
TRAKTOWANY JAKO ODDZIELNY STAN (`gbp_pence_conversion_needed`), nigdy po cichu
utożsamiany z GBP (pułapka 100x z dokumentacji yfinance, patrz §9.4).

T8 (sanity-check rzędu wielkości, §T8): `detect_log_return_outliers` —
kontrolka dodatnia/ujemna w `tests/test_prices.py` (kopia jednej serii,
OSTATNIA sesja ×100 -> dokładnie 1 trafienie, bo nie ma sesji PO niej, która
utworzyłaby drugi próg zwrotu).

T12 (bezpiecznik importera, brief CC-I B-21, I2/I3): "zero wierszy albo same
NaN -> nie zapisuj, zaloguj do `ingest_errors`". `validate_price_row` +
`split_valid_and_rejected` dzielą odpowiedź `fetch_ohlc` na wiersze do
zapisu i odrzucone (None/NaN/±Infinity w `high_split_adj`/`low_split_adj`/
`close_split_adj` — `close_raw` jest z nich odtwarzany, więc pusty
`close_split_adj` pociąga za sobą pusty `close_raw`). Wiersz odrzucony NIGDY
nie trafia do `INSERT ... ON CONFLICT DO UPDATE` — stąd gwarancja I4: wiersz
już zapisany z pełną ceną nie jest nigdy nadpisany pustą wartością. Migracja
`sql/009_ingest_errors.sql` (I5) jest WYMAGANA — jej brak w bazie przerywa
`run_prices_fetch` czytelnym błędem PRZED jakimkolwiek zapisem cen (nie
chcemy cicho pomijać logowania T12)."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import logging
import math
import re
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, TypeVar

import curl_cffi.requests.exceptions as curl_exceptions
import exchange_calendars as xcals
import exchange_calendars.errors as xcals_errors
import pandas as pd
import psycopg
import requests
import yfinance as yf
from yfinance.exceptions import YFException, YFRateLimitError

from mannaz.calendar_check import EXCHANGE_TO_CALENDAR_CODE
from mannaz.provenance import code_sha, current_trigger

DEFAULT_START = date(2023, 10, 1)
LOG_RETURN_THRESHOLD = Decimal(4)

CurrencyCheck = Literal["match", "gbp_pence_conversion_needed", "mismatch", "no_data"]
# I3 (brief CC-I): status agregatu wierszy jednego instrumentu w jednym przebiegu.
SymbolPricesStatus = Literal["ok", "rows_rejected", "empty_response", "connection_error"]
# I2: kolumny, których pustość/nieskończoność dyskwalifikuje cały wiersz.
_REQUIRED_PRICE_COLUMNS = ("high_split_adj", "low_split_adj", "close_split_adj")


# ---------------------------------------------------------------------------
# Pobieranie z yfinance
# ---------------------------------------------------------------------------


@dataclass
class OhlcRow:
    price_date: date
    open_split_adj: Decimal | None
    high_split_adj: Decimal | None
    low_split_adj: Decimal | None
    close_split_adj: Decimal | None
    volume_split_adj: Decimal | None
    adj_close_total_return: Decimal | None


def _to_decimal(value: Any) -> Decimal | None:
    """I2 (brief CC-I): NaN ORAZ ±Infinity (np. `float('inf')`, dzielenie
    przez zero po stronie Yahoo) -> None. Bez tego ±Infinity przechodziłby
    dalej jako `Decimal('Infinity')` i mógłby trafić do zapisu."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        return Decimal(str(value))
    except (ValueError, TypeError, ArithmeticError):
        return None


def fetch_ohlc(symbol: str, start: date, end: date) -> list[OhlcRow]:
    """`auto_adjust=False, actions=True` (brief P3.2). `end` traktowany
    inkluzywnie (Yahoo `history(end=...)` jest wyłączający, stąd +1 dzień)."""
    ticker = yf.Ticker(symbol)
    # B-48 C2a: yfinance domyslnie POLYKA wyjatki GET (history.py: `raise_errors or
    # not YfConfig.debug.hide_exceptions`). `raise_errors=True` dziala tylko dla
    # tego wywolania (bez zmiany globalnej konfiguracji); wywoluje
    # DeprecationWarning, wyciszany lokalnie (kontekst jest przywracany).
    # Mapowanie: YFRateLimitError -> propaguje (ponowienie w run_prices_fetch);
    # pozostale YFException -> pusta odpowiedz (`empty_response`); reszta propaguje.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            hist = ticker.history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
                actions=True,
                raise_errors=True,
            )
    except YFRateLimitError:
        raise
    except YFException:
        return []
    rows: list[OhlcRow] = []
    if hist is None or hist.empty:
        return rows
    for idx, row in hist.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        rows.append(
            OhlcRow(
                price_date=d,
                open_split_adj=_to_decimal(row.get("Open")),
                high_split_adj=_to_decimal(row.get("High")),
                low_split_adj=_to_decimal(row.get("Low")),
                close_split_adj=_to_decimal(row.get("Close")),
                volume_split_adj=_to_decimal(row.get("Volume")),
                adj_close_total_return=_to_decimal(row.get("Adj Close")),
            )
        )
    return rows


def fetch_currency(symbol: str) -> str | None:
    """`fast_info['currency']` — lżejsze niż `.info` pełne, wystarcza do T7."""
    try:
        fast = yf.Ticker(symbol).fast_info
        return fast.get("currency")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# B-49 — błędy sieci: jedna ponowna próba, potem wpis `connection_error`
# ---------------------------------------------------------------------------

# Łapiemy WYŁĄCZNIE te klasy (nigdy gołe Exception ani OSError).
NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    curl_exceptions.ConnectionError,
    curl_exceptions.Timeout,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)
# B-48 C2a: sciezka Yahoo ponawia dodatkowo limit zapytan (YFRateLimitError).
YAHOO_RETRYABLE_ERRORS: tuple[type[BaseException], ...] = NETWORK_ERRORS + (YFRateLimitError,)
NETWORK_RETRY_SLEEP_SECONDS = 5

_T = TypeVar("_T")


def call_with_network_retry(
    func: Callable[[], _T],
    sleep: Callable[[float], None] = time.sleep,
    errors: tuple[type[BaseException], ...] = NETWORK_ERRORS,
) -> _T:
    """Wywołuje `func()`; po błędzie z `errors` (domyślnie `NETWORK_ERRORS`) czeka
    `NETWORK_RETRY_SLEEP_SECONDS` i próbuje JEDEN raz ponownie. Druga porażka
    propaguje wyjątek (łapie go wołający). Inne wyjątki propagują od razu."""
    try:
        return func()
    except errors:
        sleep(NETWORK_RETRY_SLEEP_SECONDS)
        return func()


def _error_code(exc: BaseException) -> int | None:
    """Kod błędu bez URL: dla curl_cffi atrybut `code` (jeśli != 0), w
    przeciwnym razie `errno` pierwszego OSError w łańcuchu `__cause__`/
    `__context__`/`reason`/`args` (np. 10054), głębokość ograniczona."""
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code:
        return int(code)
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack and len(seen) < 20:
        cur = stack.pop(0)
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        errno_ = getattr(cur, "errno", None) if isinstance(cur, OSError) else None
        if isinstance(errno_, int):
            return errno_
        nxt: list[Any] = [cur.__cause__, cur.__context__, getattr(cur, "reason", None), *cur.args]
        stack.extend(x for x in nxt if isinstance(x, BaseException))
    return None


def describe_network_error(exc: BaseException) -> str:
    """`detail` wpisu `connection_error`: pełna nazwa klasy wyjątku i kod
    błędu. Celowo BEZ `str(exc)` (może zawierać URL z parametrami), BEZ
    nagłówków."""
    cls = type(exc)
    code = _error_code(exc)
    return f"{cls.__module__}.{cls.__qualname__} code={code if code is not None else 'brak'}"


# ---------------------------------------------------------------------------
# I2 (brief CC-I, B-21) — bezpiecznik wiersza: walidacja + podział odpowiedzi
# ---------------------------------------------------------------------------


def validate_price_row(row: OhlcRow) -> list[str]:
    """Zwraca listę nazw kolumn spośród `high_split_adj`/`low_split_adj`/
    `close_split_adj`, które są `None` albo nieskończone (`Decimal.is_finite()
    == False` — pokrywa NaN i ±Infinity; `_to_decimal` już zamienia oba na
    None dla wejść typu `float`, ale wiersz może też trafić tu skonstruowany
    wprost, np. w testach, z `Decimal('Infinity')`). Pusta lista -> wiersz do
    zapisu. `close_raw`/`open_raw`/`high_raw`/`low_raw` są odtwarzane z
    kolumn `*_split_adj` (`reconstruct_raw`), więc pusty `close_split_adj`
    pociąga za sobą pusty `close_raw` — nie trzeba go sprawdzać osobno."""
    bad: list[str] = []
    for col_name in _REQUIRED_PRICE_COLUMNS:
        value: Decimal | None = getattr(row, col_name)
        if value is None or not value.is_finite():
            bad.append(col_name)
    return bad


def split_valid_and_rejected(
    rows: list[OhlcRow],
) -> tuple[list[OhlcRow], list[tuple[OhlcRow, list[str]]]]:
    """Dzieli odpowiedź `fetch_ohlc` na (do_zapisu, odrzucone) wg
    `validate_price_row` (I2). `odrzucone` = `[(row, [nazwy_zlych_kolumn]), ...]`
    — zachowuje wiersz razem z powodem odrzucenia, do wpisu w `ingest_errors`."""
    valid: list[OhlcRow] = []
    rejected: list[tuple[OhlcRow, list[str]]] = []
    for row in rows:
        bad_cols = validate_price_row(row)
        if bad_cols:
            rejected.append((row, bad_cols))
        else:
            valid.append(row)
    return valid, rejected


# ---------------------------------------------------------------------------
# Odtwarzanie `*_raw` z corporate_events (kierunek — patrz docstring modułu)
# ---------------------------------------------------------------------------


def cumulative_ratio_after(price_date: date, events: list[tuple[date, Decimal]]) -> Decimal:
    result = Decimal(1)
    for event_date, ratio in events:
        if event_date > price_date:
            result *= ratio
    return result


def reconstruct_raw(
    value: Decimal | None, price_date: date, events: list[tuple[date, Decimal]]
) -> Decimal | None:
    if value is None:
        return None
    return value * cumulative_ratio_after(price_date, events)


# ---------------------------------------------------------------------------
# T7 — asercja waluty
# ---------------------------------------------------------------------------


def check_currency(yahoo_currency: str | None, expected_currency: str) -> CurrencyCheck:
    if yahoo_currency is None:
        return "no_data"
    if yahoo_currency == expected_currency:
        return "match"
    if expected_currency == "GBP" and yahoo_currency in ("GBp", "GBX"):
        return "gbp_pence_conversion_needed"
    return "mismatch"


# ---------------------------------------------------------------------------
# T8 — sanity-check rzędu wielkości (|log return| > 4)
# ---------------------------------------------------------------------------


def detect_log_return_outliers(
    closes: list[tuple[date, Decimal]]
) -> list[tuple[date, Decimal]]:
    """closes: [(price_date, close_split_adj), ...], nieposortowane. Zwraca
    [(price_date_konca_pary, log_return), ...] dla par sesji sąsiednich (po
    posortowaniu), gdzie |ln(c_t / c_{t-1})| > 4."""
    ordered = sorted((c for c in closes if c[1] is not None and c[1] > 0), key=lambda c: c[0])
    hits: list[tuple[date, Decimal]] = []
    for (d0, c0), (d1, c1) in zip(ordered, ordered[1:]):
        log_return = Decimal(str(math.log(float(c1) / float(c0))))
        if abs(log_return) > LOG_RETURN_THRESHOLD:
            hits.append((d1, log_return))
    return hits


# ---------------------------------------------------------------------------
# Orkiestracja / DB
# ---------------------------------------------------------------------------


@dataclass
class SymbolPricesResult:
    instrument_id: int
    yahoo_symbol: str
    rows_fetched: int = 0
    rows_inserted: int = 0
    rows_rejected: int = 0  # I2/I3 — wiersze odrzucone przez validate_price_row
    status: SymbolPricesStatus = "ok"  # I3 — "ok" / "rows_rejected" / "empty_response"
    currency_check: CurrencyCheck = "no_data"
    log_return_outliers: list[tuple[date, Decimal]] = field(default_factory=list)
    adjustment_convention: str = "yahoo_split_adjusted"
    note: str = ""
    revisions: int = 0  # B-05 (Y3) — wiersze z wpisem `provider_revision`
    rows_unclosed_session: int = 0  # B-48 (T46) — świece sesji niezamkniętej pominięte
    # B-48 (D2 rew. 3): wiersze ocenione regułą daty, bo kalendarza nie dało się
    # zastosować (nieznany kod / data poza zakresem / data niebędąca sesją).
    # Instrument bez kalendarza (calendar_code None) NIE liczy się tutaj.
    rows_calendar_fallback: int = 0
    # B-76: wypełniacze dostawcy (O=H=L=C, volume 0) — patrz `is_provider_filler`.
    filler_identical: int = 0  # zapisany wiersz identyczny na OHLC -> pominięty
    filler_kept: int = 0  # zapisany wiersz inny -> NIE nadpisany
    filler_missing: int = 0  # brak zapisanego wiersza -> niezapisany + wpis provider_filler
    # B-84: wiersze bez zmian (OHLC w tolerancji, ta sama konwencja) -> bez UPDATE.
    rows_unchanged: int = 0
    # B-84: wiersze korekty ręcznej (tradingview_manual*) pominięte przez importer.
    rows_manual_protected: int = 0


# B-84: konflikt korekty ręcznej z close dostawcy (informacja, NIE DATA FAILURE).
# delta_pct = (provider_close - stored_close) / stored_close * 100 (znak: kierunek dostawcy).
@dataclass
class ManualConflict:
    yahoo_symbol: str
    price_date: date
    stored_close: Decimal
    provider_close: Decimal
    delta_pct: Decimal


@dataclass
class PricesSummary:
    results: list[SymbolPricesResult] = field(default_factory=list)
    t7_pass: int = 0
    t7_total: int = 0
    # I3 — liczniki instrumentów wg statusu tego przebiegu, + suma wierszy odrzuconych.
    instruments_ok: int = 0
    instruments_with_rejected_rows: int = 0
    instruments_empty_response: int = 0
    rows_rejected_total: int = 0
    # B-05 (Y3) — rewizje dostawcy w tym przebiegu: liczba wierszy z wpisem
    # `provider_revision` (per instrument: `SymbolPricesResult.revisions`).
    revisions_total: int = 0
    # B-48 (T46): świece sesji niezamkniętej pominięte w tym przebiegu
    # (suma wierszy i liczba instrumentów z >0 pominiętych).
    rows_unclosed_session_total: int = 0
    instruments_unclosed_session: int = 0
    # B-48 (D2 rew. 3): wiersze ocenione regułą daty zamiast kalendarza i liczba
    # instrumentów z >0 takich wierszy.
    rows_calendar_fallback_total: int = 0
    instruments_calendar_fallback: int = 0
    # B-49: instrumenty, dla których fetch_ohlc padł dwukrotnie na błędzie sieci.
    instruments_connection_error: int = 0
    # B-76: wypełniacze dostawcy — przypadek (b) (zapisany wiersz inny, nie nadpisany)
    # i (c) (brak zapisanego wiersza, wpis `provider_filler`).
    filler_kept_total: int = 0
    instruments_filler_kept: int = 0
    filler_missing_total: int = 0
    # B-84: konflikty z korektą ręczną (close dostawcy vs zapisany > MANUAL_CONFLICT_REL).
    manual_conflicts: list[ManualConflict] = field(default_factory=list)


# B-76: wypełniacz dostawcy. Yahoo dla brakujących notowań zwraca O=H=L=C =
# poprzednie zamknięcie i volume 0 (zmierzone: 262 takie wiersze w bazie, 0
# wierszy z volume 0 innych niż wypełniacz; legalne płaskie sesje mają volume > 0).
# volume None != 0, więc None to nie wypełniacz.
def is_provider_filler(open_: Any, high: Any, low: Any, close: Any, volume: Any) -> bool:
    if open_ is None or high is None or low is None or close is None or volume is None:
        return False
    return open_ == high == low == close and volume == 0


# B-05 (Y3): kolumny OHLC split_adj wchodzące do porównania rewizji dostawcy
# (kolejność = kolejność w `detail`). volume_split_adj i total return NIE wchodzą.
_REVISION_COLUMNS = ("open_split_adj", "high_split_adj", "low_split_adj", "close_split_adj")


# B-84: tolerancja rewizji — zmiana O/H/L/C split_adj o <= 1e-6 względnie
# (|new-old|/|old|) to szum zmiennoprzecinkowy dostawcy, nie rewizja.
REVISION_REL_TOLERANCE = Decimal("1e-6")
# B-84: próg konfliktu korekty ręcznej z close dostawcy (względnie, 0,5%).
MANUAL_CONFLICT_REL = Decimal("0.005")
MANUAL_CONVENTION_PREFIX = "tradingview_manual"
NOTRADE_CONVENTION = "tradingview_manual_notrade"  # B-78


def _exceeds_tolerance(old: Decimal, new: Decimal, tolerance: Decimal) -> bool:
    """B-84: |new-old| > tolerance * |old| (dla old = 0 każda różnica przekracza)."""
    return abs(new - old) > tolerance * abs(old)


def _format_revision_detail(
    stored: dict[str, Decimal | None],
    row: "OhlcRow",
    tolerance: Decimal = REVISION_REL_TOLERANCE,
) -> str | None:
    """B-05 (Y3): rewizja dostawcy = zmiana ZAPISANEJ, kompletnej (nie NULL)
    wartości którejkolwiek z kolumn OHLC split_adj przy ponownym pobraniu.
    B-84: zmiana liczy się, gdy przekracza `tolerance` względnie
    (|new-old|/|old| > tolerance, domyślnie `REVISION_REL_TOLERANCE` = 1e-6);
    `tolerance=0` daje porównanie dokładne. Decimal bez kwantyzacji: kolumny
    NUMERIC w `prices_daily` nie mają skali (sql/001_schema.sql), więc wartość
    z bazy jest dokładnie tym Decimalem, który został zapisany.
    Zwraca `detail` w stałym formacie `col=<kolumna> old=<x> new=<y>; ...`
    (tylko kolumny powyżej tolerancji) albo None, gdy rewizji nie ma."""
    parts: list[str] = []
    for col in _REVISION_COLUMNS:
        old = stored.get(col)
        new = getattr(row, col)
        if old is None or new is None:
            continue
        if _exceeds_tolerance(old, new, tolerance):
            parts.append(f"col={col} old={old} new={new}")
    return "; ".join(parts) if parts else None


_RAW_COLUMNS = ("open_raw", "high_raw", "low_raw", "close_raw")


def _row_unchanged(
    stored: dict[str, Decimal | None],
    row: "OhlcRow",
    adjustment_convention: str,
    events: list[tuple[date, Decimal]],
) -> bool:
    """B-84: wiersz bez zmian = wszystkie 4 kolumny OHLC split_adj zapisane i nowe
    nie-NULL, każda różnica w tolerancji, zapisana konwencja == wyliczona ORAZ
    zapisane OHLC raw == rekonstrukcja z zapisanego split_adj i BIEŻĄCYCH zdarzeń
    (Codex R1 M1: kolejny split dopisany do `corporate_events` nie zmienia nazwy
    konwencji, a zmienia raw; porównanie dokładne — `reconstruct_raw` to iloczyn
    Decimal, deterministyczny dla tego samego wejścia). Wtedy importer nie robi
    UPDATE (konwencja, fetched_at, volume bez zmian)."""
    if stored.get("adjustment_convention") != adjustment_convention:
        return False
    for col in _REVISION_COLUMNS:
        if stored.get(col) is None or getattr(row, col) is None:
            return False
    for adj_col, raw_col in zip(_REVISION_COLUMNS, _RAW_COLUMNS):
        if stored.get(raw_col) != reconstruct_raw(stored[adj_col], row.price_date, events):
            return False
    return _format_revision_detail(stored, row) is None


def _is_manual_row(stored: dict[str, Decimal | None] | None) -> bool:
    """B-84: zapisany wiersz z korekty ręcznej (tradingview_manual*)."""
    return stored is not None and str(stored.get("adjustment_convention") or "").startswith(
        MANUAL_CONVENTION_PREFIX
    )


def _manual_conflict(symbol: str, row: "OhlcRow", stored: dict[str, Decimal | None]) -> ManualConflict | None:
    """B-84: konflikt, gdy |close_dostawcy - close_zapisany| / |close_zapisany| > 0,5%."""
    old = stored.get("close_split_adj")
    new = row.close_split_adj
    if old is None or new is None or old == 0:
        return None
    if not _exceeds_tolerance(old, new, MANUAL_CONFLICT_REL):
        return None
    return ManualConflict(
        yahoo_symbol=symbol,
        price_date=row.price_date,
        stored_close=old,
        provider_close=new,
        delta_pct=(new - old) / abs(old) * 100,
    )


# B-48 (T46): świeca sesji niezamkniętej nie wchodzi do bazy.
SESSION_CLOSE_GRACE = timedelta(minutes=30)


@functools.lru_cache(maxsize=None)
def _get_calendar(calendar_code: str):
    """Kalendarz `exchange_calendars` cache'owany per kod (domyślny zakres
    biblioteki: ok. 20 lat wstecz do roku w przód)."""
    return xcals.get_calendar(calendar_code)


def _session_close_utc(calendar_code: str, price_date: date) -> datetime | None:
    """Zamknięcie sesji `price_date` w UTC wg kalendarza `calendar_code` albo
    None, gdy kalendarza nie da się zastosować: nieznany kod
    (`InvalidCalendarName`), `price_date` poza zakresem kalendarza
    (`DateOutOfBounds`, sprawdzane też jawnie przez first/last_session) albo
    `price_date` nie jest sesją. Inne wyjątki propagują (wstrzykiwalne w
    testach przez monkeypatch)."""
    try:
        cal = _get_calendar(calendar_code)
        ts = pd.Timestamp(price_date)
        if ts < cal.first_session or ts > cal.last_session or not cal.is_session(ts):
            return None
        return cal.closes.loc[ts].to_pydatetime()
    except (xcals_errors.InvalidCalendarName, xcals_errors.DateOutOfBounds):
        return None


def _session_closed_ex(
    calendar_code: str | None, price_date: date, now_utc: datetime
) -> tuple[bool, bool]:
    """(closed, fallback). `fallback` = True tylko gdy `calendar_code` jest
    podany, a kalendarza nie dało się zastosować (D2 rew. 3) i użyto reguły
    daty `price_date < now_utc.date()`. `calendar_code` None to reguła
    podstawowa, nie fallback (fallback=False)."""
    if now_utc.tzinfo is None or now_utc.utcoffset() is None:
        raise ValueError("now_utc musi byc tz-aware (UTC)")
    now_utc = now_utc.astimezone(timezone.utc)
    if calendar_code is not None:
        close = _session_close_utc(calendar_code, price_date)
        if close is not None:
            return now_utc >= close + SESSION_CLOSE_GRACE, False
        return price_date < now_utc.date(), True
    return price_date < now_utc.date(), False


def session_closed(calendar_code: str | None, price_date: date, now_utc: datetime) -> bool:
    """B-48 (T46): czy świeca sesji `price_date` jest sesją zamkniętą.

    - `calendar_code` podany i `price_date` jest sesją: True <=> `now_utc >=
      zamknięcie sesji (UTC) + SESSION_CLOSE_GRACE` (30 min).
    - `calendar_code` None: True <=> `price_date < now_utc.date()`.
    - Przypadek brzegowy niewymieniony w T46 (D2 rew. 3): `price_date` nie jest
      sesją kalendarza (albo poza jego zakresem / nieznany kod) -> reguła jak
      dla None (`price_date < now_utc.date()`); `run_prices_fetch` liczy takie
      wiersze (`rows_calendar_fallback`), patrz `_session_closed_ex`.

    `now_utc` musi być tz-aware (inaczej ValueError)."""
    return _session_closed_ex(calendar_code, price_date, now_utc)[0]


# ---------------------------------------------------------------------------
# B-81: komunikaty dostawcy (yfinance) — przechwycenie zamiast gubienia na konsoli
# ---------------------------------------------------------------------------

_PROVIDER_SYMBOL_RE = re.compile(r"^\$?([A-Za-z0-9.^=\-]+):")


@dataclass(frozen=True)
class ProviderMessage:
    """B-81: jedna linia komunikatu dostawcy (np. `$SHO.WA: Data doesn't exist...`);
    `symbol` z tresci wg `_PROVIDER_SYMBOL_RE` (wtedy `text` bez tego prefiksu),
    inaczej "?" i cala linia."""

    symbol: str
    text: str


def _provider_message(line: str) -> ProviderMessage | None:
    text = line.strip()
    if not text:
        return None
    m = _PROVIDER_SYMBOL_RE.match(text)
    if m is None:
        return ProviderMessage("?", text)
    # tresc bez prefiksu `$SYMBOL:` (symbol jest w osobnym polu; HEARTBEAT: "<symbol>: <tresc>")
    return ProviderMessage(m.group(1), text[m.end():].strip() or text)


class _ProviderLogHandler(logging.Handler):
    def __init__(self, sink: list[ProviderMessage]) -> None:
        super().__init__(level=logging.WARNING)
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001 — komunikat dostawcy nigdy nie przerywa etapu
            return
        for line in text.splitlines():
            msg = _provider_message(line)
            if msg is not None:
                self._sink.append(msg)


class _ProviderStderr(io.TextIOBase):
    """Bufor stderr dzielony na linie; niepelna ostatnia linia trafia do listy przy `close_buffer`."""

    def __init__(self, sink: list[ProviderMessage]) -> None:
        self._sink = sink
        self._partial = ""

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self._partial += text
        *lines, self._partial = self._partial.split("\n")
        for line in lines:
            msg = _provider_message(line)
            if msg is not None:
                self._sink.append(msg)
        return len(text)

    def flush(self) -> None:
        pass

    def close_buffer(self) -> None:
        msg = _provider_message(self._partial)
        self._partial = ""
        if msg is not None:
            self._sink.append(msg)


class ProviderMessageCapture:
    """B-81: menedzer kontekstu z `capture_provider_messages()`. Na czas bloku:
    handler (poziom >= WARNING) na loggerze `yfinance` z `propagate=False` oraz
    `contextlib.redirect_stderr` do bufora; po wyjsciu (takze przy wyjatku)
    stan loggera przywrocony. Wynik: `messages` (kolejnosc wystapienia).
    Komunikat to nie wyjatek — nic nie przerywa."""

    def __init__(self) -> None:
        self.messages: list[ProviderMessage] = []
        self._logger = logging.getLogger("yfinance")
        self._handler = _ProviderLogHandler(self.messages)
        self._stderr = _ProviderStderr(self.messages)
        self._redirect = contextlib.redirect_stderr(self._stderr)
        self._saved: tuple[bool, int] | None = None

    def __enter__(self) -> "ProviderMessageCapture":
        self._saved = (self._logger.propagate, self._logger.level)
        self._logger.propagate = False
        if self._logger.getEffectiveLevel() > logging.WARNING:
            self._logger.setLevel(logging.WARNING)
        self._logger.addHandler(self._handler)
        self._redirect.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._redirect.__exit__(exc_type, exc, tb)
        finally:
            self._stderr.close_buffer()
            self._logger.removeHandler(self._handler)
            if self._saved is not None:
                self._logger.propagate = self._saved[0]
                self._logger.setLevel(self._saved[1])
                self._saved = None
        return None


def capture_provider_messages() -> ProviderMessageCapture:
    """B-81: `with capture_provider_messages() as cap: ...; cap.messages`."""
    return ProviderMessageCapture()


def _positions_window_ids(
    cur: psycopg.Cursor, today: date
) -> tuple[list[int], list[str]] | None:
    """B-70: instrumenty z pozycją (satelita lub rdzeń) otwartą w DOWOLNYM dniu
    kalendarzowym przedziału [`prev_D`, `today`], `prev_D` = `MAX(risk_date)` z
    `risk_daily`. Pozycje wyłącznie przez `mannaz.risk.open_positions_as_of`
    (zero własnej definicji „pozycji otwartej”). Zwraca (id instrumentów,
    `base_symbol` kontraktów futures) albo None, gdy przedział jest pusty
    (`risk_daily` pusta albo `prev_D > today`) — wtedy obowiązuje dotychczasowe
    zapytanie."""
    from mannaz.risk import open_positions_as_of  # lokalnie: unikamy cyklu importów

    cur.execute("SELECT MAX(risk_date) FROM risk_daily")
    prev_d = cur.fetchone()[0]
    if prev_d is None or prev_d > today:
        return None
    ids: set[int] = set()
    bases: set[str] = set()
    d = prev_d
    while d <= today:
        for p in open_positions_as_of(cur, d):
            ids.add(p["instrument_id"])
            if p["instrument_type"] == "future" and p["base_symbol"]:
                bases.add(p["base_symbol"])
        d += timedelta(days=1)
    return sorted(ids), sorted(bases)


def _mapped_instruments(
    cur: psycopg.Cursor,
    instrument_ids: list[int] | None,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Zbiór instrumentów do pobrania cen. Z jawną listą `instrument_ids` — ta
    lista. Bez niej: dotychczasowe zapytanie (rdzeń, pozycje w `positions_fifo`,
    bazy otwartych futures, benchmarki) ∪ (B-70) instrumenty z pozycją otwartą
    w dowolnym dniu przedziału [`prev_D`, `today`] wraz z bazami futures —
    żeby pozycja otwarta i zamknięta między ocenami miała ceny. `today` None
    albo `risk_daily` pusta -> dokładnie dotychczasowe zachowanie."""
    if instrument_ids:
        cur.execute(
            """
            SELECT id, yahoo_symbol, currency, exchange FROM instruments
            WHERE id = ANY(%s) AND yahoo_symbol IS NOT NULL
            ORDER BY id
            """,
            (instrument_ids,),
        )
    else:
        window = _positions_window_ids(cur, today) if today is not None else None
        win_ids, win_bases = window if window is not None else ([], [])
        cur.execute(
            """
            SELECT i.id, i.yahoo_symbol, i.currency, i.exchange FROM instruments i
            WHERE i.yahoo_symbol IS NOT NULL
              AND (i.is_core OR EXISTS (
                  SELECT 1 FROM positions_fifo p WHERE p.instrument_id = i.id
              ) OR i.yahoo_symbol IN (
                  -- underlyings of open futures (P4.2: stop/ATR on the base)
                  SELECT f.base_symbol FROM instruments f
                  JOIN positions_fifo p ON p.instrument_id = f.id
                  WHERE f.instrument_type = 'future'
              ) OR i.is_benchmark
                -- B-70: pozycje otwarte w oknie [prev_D, today] i bazy ich futures
                OR i.id = ANY(%s) OR i.yahoo_symbol = ANY(%s))
            ORDER BY i.id
            """,
            (win_ids, win_bases),
        )
    cols = ("id", "yahoo_symbol", "currency", "exchange")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _instrument_split_events(cur: psycopg.Cursor, instrument_id: int) -> list[tuple[date, Decimal]]:
    cur.execute(
        """
        SELECT event_date, ratio FROM corporate_events
        WHERE instrument_id = %s AND ratio IS NOT NULL
          AND event_type IN ('split', 'reverse_split')
        ORDER BY event_date
        """,
        (instrument_id,),
    )
    return [(d, r) for d, r in cur.fetchall()]


class MissingIngestErrorsTableError(RuntimeError):
    """I5 (brief CC-I, B-21): tabela `ingest_errors` nie istnieje w bazie —
    migracja `sql/009_ingest_errors.sql` nie została zastosowana.
    `run_prices_fetch` rzuca ten wyjątek PRZED jakimkolwiek zapisem do
    `prices_daily` (nie chcemy cicho pomijać logowania T12 tylko dlatego, że
    tabela na logi jeszcze nie istnieje)."""


def _ensure_ingest_errors_table(cur: psycopg.Cursor) -> None:
    cur.execute("SELECT to_regclass('ingest_errors')")
    if cur.fetchone()[0] is None:
        raise MissingIngestErrorsTableError(
            "Tabela ingest_errors nie istnieje — zastosuj sql/009_ingest_errors.sql "
            "(I5, brief CC-I) przed uruchomieniem run_prices_fetch."
        )


def _log_ingest_error(
    cur: psycopg.Cursor,
    *,
    source: str,
    instrument_id: int | None,
    price_date: date | None,
    error_type: str,
    detail: str,
    run_started_at: datetime,
) -> None:
    """Append-only wpis do `ingest_errors` (I2/I3/I5) — `degraded_state`
    zawsze FALSE, o degradacji stanu decyduje warstwa odczytu (`risk_daily`)."""
    cur.execute(
        """
        INSERT INTO ingest_errors (
            source, instrument_id, price_date, error_type, detail,
            degraded_state, run_started_at
        ) VALUES (%s, %s, %s, %s, %s, FALSE, %s)
        """,
        (source, instrument_id, price_date, error_type, detail, run_started_at),
    )


def run_prices_fetch(
    conn: psycopg.Connection,
    instrument_ids: list[int] | None = None,
    start: date = DEFAULT_START,
    end: date | None = None,
    commit: bool = True,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> PricesSummary:
    """Pobiera i zapisuje `prices_daily` dla instrumentów z wypełnionym
    `yahoo_symbol` (P3.1). `instrument_ids=None` -> WSZYSTKIE zmapowane
    instrumenty (pełne pobranie — brief zastrzega, że wykonuje je inny agent);
    lista id -> próba na wybranym podzbiorze (brief P3.2: 2 symbole).

    Okno domyślne (I4, brief CC-I): `start=DEFAULT_START` (2023-10-01) ->
    `end=dzisiaj`, czyli PEŁNA historia przy każdym przebiegu (upsert) —
    ZNACZNIE szersze niż okno samonaprawy D-5 z M67 dokumentu projektowego
    ("dane sesji D są odświeżane w oknie D-5 przy każdym przebiegu"). Okno
    ZOSTAJE bez zmian: M67 opisuje MINIMALNE okno samonaprawy dla przebiegu
    codziennego ("straży cenowej"), nie ogranicza zakresu tego pełnego
    pobrania — a I2 gwarantuje, że nawet przy pełnej historii wiersz w bazie
    z kompletną ceną nigdy nie zostanie nadpisany wierszem pustym/NaN/
    Infinity, bo taki wiersz w ogóle nie dociera do INSERT/UPDATE poniżej.

    I5: wymaga istnienia tabeli `ingest_errors` (sql/009) — jej brak przerywa
    przebieg PRZED jakimkolwiek zapisem cen (`MissingIngestErrorsTableError`).

    `commit=False` (jak `run_risk`, brief CC-S) — do testów DB z rollbackiem.

    B-49: `fetch_ohlc` przy błędzie sieci (`YAHOO_RETRYABLE_ERRORS` = `NETWORK_ERRORS` + `YFRateLimitError`) jest ponawiany raz
    po `NETWORK_RETRY_SLEEP_SECONDS` (`sleep` wstrzykiwalne); druga porażka =
    wpis `ingest_errors` (`connection_error`), status instrumentu
    "connection_error", zero zapisów, przejście do następnego instrumentu.
    Wyjątek spoza tej krotki przerywa etap (inne `YFException` -> `fetch_ohlc` zwraca []).

    B-76: wiersz-wypełniacz dostawcy (`is_provider_filler`: O=H=L=C i volume 0)
    NIGDY nie jest zapisywany ani nie nadpisuje zapisanego wiersza i nie daje
    `provider_revision`: (a) zapisany identyczny na OHLC -> pominięty po cichu;
    (b) zapisany inny -> zostaje bez zmian (`filler_kept`); (c) brak zapisanego
    -> niezapisany + wpis `ingest_errors` `provider_filler` (w cyklu to DATA
    FAILURE). `rows_inserted` nie liczy pominiętych.

    B-77: zapisany wiersz + wypełniacz = "identyczny" (a) tylko gdy OHLC w
    tolerancji I zapisany volume == volume wypełniacza (0); zapisany płaski wiersz
    z volume > 0 to (b) `filler_kept`.

    B-84: rewizja = zmiana O/H/L/C split_adj o > `REVISION_REL_TOLERANCE` względnie.
    Zapisany wiersz kompletny, w tolerancji i z konwencją == wyliczona -> bez
    UPDATE i bez `provider_revision` (`rows_unchanged`; konwencja, fetched_at,
    volume bez zmian); konwencja inna -> UPDATE bez rewizji. Wiersz z konwencją
    `tradingview_manual*` NIGDY nie jest nadpisywany; close dostawcy rozjechany
    o > 0,5% = wpis w `PricesSummary.manual_conflicts` (informacja, nie DATA FAILURE)."""
    if end is None:
        end = date.today()

    run_started_at = datetime.now(timezone.utc)
    if now is None:
        now = datetime.now(timezone.utc)
    summary = PricesSummary()

    with conn.cursor() as cur:
        _ensure_ingest_errors_table(cur)  # I5 — przed jakimkolwiek zapisem cen

        instruments = _mapped_instruments(cur, instrument_ids, today=end)

        for inst in instruments:
            instrument_id = inst["id"]
            symbol = inst["yahoo_symbol"]
            expected_currency = inst["currency"]

            result = SymbolPricesResult(instrument_id=instrument_id, yahoo_symbol=symbol)

            yahoo_currency = fetch_currency(symbol)
            result.currency_check = check_currency(yahoo_currency, expected_currency)
            summary.t7_total += 1
            if result.currency_check == "match":
                summary.t7_pass += 1

            events = _instrument_split_events(cur, instrument_id)
            adjustment_convention = (
                "raw_reconstructed_from_corporate_events" if events else "yahoo_split_adjusted"
            )
            result.adjustment_convention = adjustment_convention

            try:
                rows = call_with_network_retry(
                    lambda: fetch_ohlc(symbol, start, end), sleep, YAHOO_RETRYABLE_ERRORS
                )
            except YAHOO_RETRYABLE_ERRORS as exc:
                _log_ingest_error(
                    cur,
                    source="yahoo",
                    instrument_id=instrument_id,
                    price_date=None,
                    error_type="connection_error",
                    detail=describe_network_error(exc),
                    run_started_at=run_started_at,
                )
                result.status = "connection_error"
                summary.instruments_connection_error += 1
                summary.results.append(result)
                continue
            result.rows_fetched = len(rows)

            # B-48 (T46): świeca sesji niezamkniętej (przed zamknięciem + 30 min)
            # nie wchodzi do bazy — odfiltrowanie PRZED outlierami, I2, rewizjami
            # i zapisem; bez wpisu w ingest_errors i bez wpływu na status.
            calendar_code = EXCHANGE_TO_CALENDAR_CODE.get(inst.get("exchange"))
            closed_rows: list[OhlcRow] = []
            for r in rows:
                is_closed, used_fallback = _session_closed_ex(calendar_code, r.price_date, now)
                if used_fallback:
                    result.rows_calendar_fallback += 1
                if is_closed:
                    closed_rows.append(r)
            result.rows_unclosed_session = len(rows) - len(closed_rows)
            if result.rows_calendar_fallback:
                summary.rows_calendar_fallback_total += result.rows_calendar_fallback
                summary.instruments_calendar_fallback += 1
            if result.rows_unclosed_session:
                summary.rows_unclosed_session_total += result.rows_unclosed_session
                summary.instruments_unclosed_session += 1
            provider_returned_rows = bool(rows)
            rows = closed_rows

            closes_for_t8 = [(r.price_date, r.close_split_adj) for r in rows]
            result.log_return_outliers = detect_log_return_outliers(closes_for_t8)

            # I2/I3 — bezpiecznik wiersza/odpowiedzi (brief CC-I, B-21).
            valid_rows, rejected_rows = split_valid_and_rejected(rows)
            result.rows_rejected = len(rejected_rows)

            if not provider_returned_rows:
                # I3 [S]: brak jakiejkolwiek odpowiedzi -> jeden wpis
                # 'empty_response', zero prób zapisu.
                _log_ingest_error(
                    cur,
                    source="yahoo",
                    instrument_id=instrument_id,
                    price_date=None,
                    error_type="empty_response",
                    detail="fetch_ohlc zwrocil zero wierszy",
                    run_started_at=run_started_at,
                )
                result.status = "empty_response"
                summary.instruments_empty_response += 1
            elif rows and not valid_rows:
                # I3 [S]: WSZYSTKIE wiersze odrzucone w I2 traktujemy jak
                # pustą odpowiedź — JEDEN wpis 'empty_response' z liczbą
                # odrzuconych w treści, zamiast N wpisów 'row_missing_price'
                # (byłby to szum: cały instrument i tak nie ma nic do
                # zapisania, interesuje nas fakt "zero użytecznych danych",
                # nie lista N identycznych powodów).
                _log_ingest_error(
                    cur,
                    source="yahoo",
                    instrument_id=instrument_id,
                    price_date=None,
                    error_type="empty_response",
                    detail=f"wszystkie {len(rejected_rows)} wierszy odrzucone przez walidacje I2 (brief CC-I)",
                    run_started_at=run_started_at,
                )
                result.status = "empty_response"
                summary.instruments_empty_response += 1
                summary.rows_rejected_total += len(rejected_rows)
            else:
                for bad_row, bad_cols in rejected_rows:
                    _log_ingest_error(
                        cur,
                        source="yahoo",
                        instrument_id=instrument_id,
                        price_date=bad_row.price_date,
                        error_type="row_missing_price",
                        detail="puste/niepoprawne kolumny: " + ", ".join(bad_cols),
                        run_started_at=run_started_at,
                    )
                summary.rows_rejected_total += len(rejected_rows)
                if rejected_rows:
                    result.status = "rows_rejected"
                    summary.instruments_with_rejected_rows += 1
                else:
                    result.status = "ok"
                    summary.instruments_ok += 1

            # B-05 (Y3): JEDNO zapytanie o zapisane OHLC split_adj tego
            # instrumentu (źródło yahoo) w zakresie dat pobranych, poprawnych
            # wierszy — porównanie per wiersz PRZED upsertem. Wiersze
            # odrzucone przez T12 nie są tu (nie ma ich w `valid_rows`).
            stored_by_date: dict[date, dict[str, Decimal | None]] = {}
            if valid_rows:
                cur.execute(
                    """
                    SELECT price_date, open_split_adj, high_split_adj, low_split_adj, close_split_adj,
                           adjustment_convention, volume_split_adj,
                           open_raw, high_raw, low_raw, close_raw
                    FROM prices_daily
                    WHERE instrument_id = %s AND source = 'yahoo'
                      AND price_date BETWEEN %s AND %s
                    """,
                    (
                        instrument_id,
                        min(r.price_date for r in valid_rows),
                        max(r.price_date for r in valid_rows),
                    ),
                )
                for pd_, o_, h_, l_, c_, conv_, v_, ro_, rh_, rl_, rc_ in cur.fetchall():
                    stored_by_date[pd_] = {
                        "open_split_adj": o_,
                        "high_split_adj": h_,
                        "low_split_adj": l_,
                        "close_split_adj": c_,
                        "adjustment_convention": conv_,
                        "volume_split_adj": v_,
                        "open_raw": ro_,
                        "high_raw": rh_,
                        "low_raw": rl_,
                        "close_raw": rc_,
                    }

            inserted = 0
            for row in valid_rows:
                stored = stored_by_date.get(row.price_date)
                # B-76: wypełniacz dostawcy nigdy nie trafia do INSERT/UPDATE ani do
                # wykrywania rewizji (08.10: filler nadpisał poprawny wiersz).
                if is_provider_filler(
                    row.open_split_adj,
                    row.high_split_adj,
                    row.low_split_adj,
                    row.close_split_adj,
                    row.volume_split_adj,
                ):
                    if stored is not None:
                        # B-77: "identyczny" tylko gdy OHLC w tolerancji I zapisany
                        # volume == volume wypełniacza (0); zapisany płaski wiersz
                        # z volume > 0 (albo NULL) to "zapisany inny".
                        if (
                            _format_revision_detail(stored, row) is None
                            and stored.get("volume_split_adj") == row.volume_split_adj
                        ):
                            result.filler_identical += 1
                        else:
                            result.filler_kept += 1
                            summary.filler_kept_total += 1
                        # B-84: wypełniacz na korekcie ręcznej NIE daje konfliktu — wypełniacz
                        # nie jest notowaniem (B-76); K4(ii) 2026-10-17: 37 fałszywych konfliktów
                        # na 43 wierszach tradingview_manual_b76, dla których Yahoo nadal zwraca wypełniacz.
                    else:
                        _log_ingest_error(
                            cur,
                            source="yahoo",
                            instrument_id=instrument_id,
                            price_date=row.price_date,
                            error_type="provider_filler",
                            detail=f"Yahoo: O=H=L=C={row.close_split_adj}, volume 0; wiersz niezapisany (B-76)",
                            run_started_at=run_started_at,
                        )
                        result.filler_missing += 1
                        summary.filler_missing_total += 1
                    continue
                if _is_manual_row(stored):
                    # B-84: korekta ręczna nigdy nie jest nadpisywana przez importer
                    # (ani UPDATE, ani provider_revision); rozjazd close > 0,5% = konflikt.
                    result.rows_manual_protected += 1
                    conflict = _manual_conflict(symbol, row, stored)
                    if conflict is not None:
                        summary.manual_conflicts.append(conflict)
                    continue
                if stored is not None:
                    revision_detail = _format_revision_detail(stored, row)
                    if revision_detail is None and _row_unchanged(stored, row, adjustment_convention, events):
                        # B-84: w tolerancji i ta sama konwencja -> wiersz bez zmian
                        # (bez UPDATE: konwencja, fetched_at i volume zostają).
                        result.rows_unchanged += 1
                        continue
                    if revision_detail is not None:
                        _log_ingest_error(
                            cur,
                            source="yahoo",
                            instrument_id=instrument_id,
                            price_date=row.price_date,
                            error_type="provider_revision",
                            detail=revision_detail,
                            run_started_at=run_started_at,
                        )
                        result.revisions += 1
                        summary.revisions_total += 1

                close_raw = reconstruct_raw(row.close_split_adj, row.price_date, events)
                open_raw = reconstruct_raw(row.open_split_adj, row.price_date, events)
                high_raw = reconstruct_raw(row.high_split_adj, row.price_date, events)
                low_raw = reconstruct_raw(row.low_split_adj, row.price_date, events)

                cur.execute(
                    """
                    INSERT INTO prices_daily (
                        instrument_id, price_date, currency, source,
                        open_raw, high_raw, low_raw, close_raw, volume_raw,
                        open_split_adj, high_split_adj, low_split_adj,
                        close_split_adj, volume_split_adj, adjustment_convention
                    ) VALUES (%s, %s, %s, 'yahoo', %s, %s, %s, %s, NULL,
                              %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (instrument_id, price_date, source) DO UPDATE SET
                        open_raw = EXCLUDED.open_raw,
                        high_raw = EXCLUDED.high_raw,
                        low_raw = EXCLUDED.low_raw,
                        close_raw = EXCLUDED.close_raw,
                        open_split_adj = EXCLUDED.open_split_adj,
                        high_split_adj = EXCLUDED.high_split_adj,
                        low_split_adj = EXCLUDED.low_split_adj,
                        close_split_adj = EXCLUDED.close_split_adj,
                        volume_split_adj = EXCLUDED.volume_split_adj,
                        adjustment_convention = EXCLUDED.adjustment_convention,
                        fetched_at = now()
                    """,
                    (
                        instrument_id,
                        row.price_date,
                        expected_currency,
                        open_raw,
                        high_raw,
                        low_raw,
                        close_raw,
                        row.open_split_adj,
                        row.high_split_adj,
                        row.low_split_adj,
                        row.close_split_adj,
                        row.volume_split_adj,
                        adjustment_convention,
                    ),
                )
                inserted += 1
            result.rows_inserted = inserted
            if result.filler_kept:
                summary.instruments_filler_kept += 1

            # source_runs.sha256_input jest zdefiniowany dla plików (P2.4); dla API
            # bez pliku wejściowego używamy sha256 deterministycznego opisu zapytania
            # (symbol + zakres dat) jako klucza deduplikacji tego samego uruchomienia.
            run_key = hashlib.sha256(
                f"prices_daily:{symbol}:{start.isoformat()}:{end.isoformat()}".encode("utf-8")
            ).hexdigest()
            cur.execute(
                """
                INSERT INTO source_runs (source, row_count, sha256_input, triggered_by, code_sha)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source, sha256_input) DO NOTHING
                """,
                (f"yahoo:{symbol}", inserted, run_key, current_trigger(), code_sha()),
            )

            summary.results.append(result)

        if commit:
            conn.commit()

    return summary


# ---------------------------------------------------------------------------
# B-76 — detektor wypełniaczy w oknie sesji (K1)
# ---------------------------------------------------------------------------


def find_window_fillers(rows: Any, d: date, window: int = 22) -> list[tuple[str, str | None, date]]:
    """B-76: czysty detektor. `rows` = iterowalne krotki (symbol, exchange,
    price_date, open, high, low, close, volume[, adjustment_convention]).
    B-78: wiersz z konwencją `tradingview_manual_notrade` (brak obrotu
    potwierdzony w TV) NIE jest zgłaszany, ale jego data nadal liczy się jako
    sesja okna. Okno liczone PER GIEŁDA
    (exchange None = osobna grupa): ostatnie `window` RÓŻNYCH `price_date` <= d
    wśród wierszy tej giełdy (sesja = data z ceną, jak w risk.py). Zwraca
    (symbol, exchange, price_date) wierszy-wypełniaczy w oknie swojej giełdy,
    posortowane (symbol, data). Wiersze z datą > d są ignorowane."""
    by_exchange: dict[str | None, list[tuple[str, date, bool]]] = {}
    for symbol, exchange, price_date, open_, high, low, close, volume, *rest in rows:
        if price_date > d:
            continue
        convention = rest[0] if rest else None
        by_exchange.setdefault(exchange, []).append(
            (
                symbol,
                price_date,
                is_provider_filler(open_, high, low, close, volume) and convention != NOTRADE_CONVENTION,
            )
        )
    found: list[tuple[str, str | None, date]] = []
    for exchange, items in by_exchange.items():
        sessions = sorted({pd_ for _, pd_, _ in items}, reverse=True)
        in_window = {pd_ for rank, pd_ in enumerate(sessions, start=1) if rank <= window}
        for symbol, pd_, is_filler in items:
            if is_filler and pd_ in in_window:
                found.append((symbol, exchange, pd_))
    found.sort(key=lambda t: (t[0], t[2]))
    return found


def detect_window_fillers(cur: psycopg.Cursor, d: date, window: int = 22) -> list[tuple[str, str | None, date]]:
    """B-76: wersja bazodanowa `find_window_fillers` — jedno zapytanie
    (price_date w [d - 120 dni, d], bez filtra source; symbol =
    COALESCE(yahoo_symbol, broker_ticker))."""
    cur.execute(
        """
        SELECT COALESCE(i.yahoo_symbol, i.broker_ticker), i.exchange, p.price_date,
               p.open_split_adj, p.high_split_adj, p.low_split_adj, p.close_split_adj,
               p.volume_split_adj, p.adjustment_convention
        FROM prices_daily p
        JOIN instruments i ON i.id = p.instrument_id
        WHERE p.price_date <= %s AND p.price_date >= %s
        """,
        (d, d - timedelta(days=120)),
    )
    return find_window_fillers(cur.fetchall(), d, window)


# ---------------------------------------------------------------------------
# B-76 — korekta ręczna wypełniaczy (N1)
# ---------------------------------------------------------------------------


@dataclass
class ManualCorrectionSummary:
    rows_input: int = 0
    rows_to_write: int = 0
    # (yahoo_symbol, price_date, powód): "symbol" / "brak wiersza" / "nie filler" / "ohlc" / "duplikat"
    rejected: list[tuple[str, date | None, str]] = field(default_factory=list)
    rows_written: int = 0
    per_symbol: dict[str, int] = field(default_factory=dict)
    dates_per_symbol: dict[str, tuple[date, date]] = field(default_factory=dict)


class ManualCorrectionRejectedError(RuntimeError):
    """B-76: korekta ręczna z >0 odrzuconych wierszy — rollback całości."""

    def __init__(self, summary: ManualCorrectionSummary):
        super().__init__(f"korekta reczna odrzucona: {len(summary.rejected)} wierszy odrzuconych (rollback)")
        self.summary = summary


def apply_manual_corrections(
    conn: psycopg.Connection,
    rows: list[tuple[str, date, Any, Any, Any, Any, Any]],
    *,
    convention: str | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
    notrade: bool = False,
) -> ManualCorrectionSummary:
    """B-76 (N1): ręczna korekta WYŁĄCZNIE wierszy-wypełniaczy (`prices_daily`,
    source='yahoo'). `rows` = (yahoo_symbol, price_date, open, high, low, close,
    volume). Odrzucane (powód): "symbol" (nie dokładnie jeden instrument z tym
    yahoo_symbol), "brak wiersza", "nie filler" (zapisany wiersz nie jest
    wypełniaczem), "ohlc" (wartości niedodatnie/niespójne), "duplikat" (ten sam
    symbol+data dwa razy w wejściu). Wszystko albo nic: jedna transakcja;
    commit tylko gdy nie `dry_run` i 0 odrzuconych; `dry_run` -> rollback
    zawsze (zwraca podsumowanie, nic nie zapisuje); przy >0 odrzuconych i
    `dry_run=False` -> rollback + `ManualCorrectionRejectedError`.

    `convention=None` -> `tradingview_manual_b76` (B-76). B-78 `notrade=True`:
    brak obrotu potwierdzony w TV — konwencja wymuszona `tradingview_manual_notrade`
    (inna jawna `convention` -> `ValueError`), wejście musi mieć O=H=L=C > 0 i
    volume == 0 (inaczej "ohlc"), zapisany wiersz musi być wypełniaczem (jak w
    B-76), zapis O/H/L/C wg wejścia i volume_split_adj 0, `ingest_errors`
    `manual_correction` z prefiksem "B-78:". Detektor wypełniaczy takich wierszy
    nie zgłasza, a importer ich nie nadpisuje (reguła B-84)."""
    if notrade:
        if convention is not None and convention != NOTRADE_CONVENTION:
            raise ValueError(f"notrade wymusza konwencje {NOTRADE_CONVENTION}, podano {convention}")
        convention = NOTRADE_CONVENTION
        tag = "B-78"
    else:
        convention = convention or "tradingview_manual_b76"
        tag = "B-76"
    run_started_at = now if now is not None else datetime.now(timezone.utc)
    summary = ManualCorrectionSummary(rows_input=len(rows))
    planned: list[tuple[int, str, date, tuple[Any, ...], tuple[Any, ...]]] = []
    seen: set[tuple[str, date]] = set()
    try:
        with conn.cursor() as cur:
            _ensure_ingest_errors_table(cur)
            for symbol, price_date, o_, h_, l_, c_, v_ in rows:
                if (symbol, price_date) in seen:
                    summary.rejected.append((symbol, price_date, "duplikat"))
                    continue
                seen.add((symbol, price_date))
                cur.execute("SELECT id FROM instruments WHERE yahoo_symbol = %s", (symbol,))
                found = cur.fetchall()
                if len(found) != 1:
                    summary.rejected.append((symbol, price_date, "symbol"))
                    continue
                instrument_id = found[0][0]
                cur.execute(
                    """
                    SELECT open_split_adj, high_split_adj, low_split_adj, close_split_adj, volume_split_adj
                    FROM prices_daily
                    WHERE instrument_id = %s AND price_date = %s AND source = 'yahoo'
                    """,
                    (instrument_id, price_date),
                )
                stored = cur.fetchone()
                if stored is None:
                    summary.rejected.append((symbol, price_date, "brak wiersza"))
                    continue
                if not is_provider_filler(*stored):
                    summary.rejected.append((symbol, price_date, "nie filler"))
                    continue
                vals = (o_, h_, l_, c_, v_)
                if (
                    any(x is None for x in vals)
                    or any(x <= 0 for x in (vals[:4] if notrade else vals))
                    or l_ > min(o_, c_)
                    or h_ < max(o_, c_)
                    or (notrade and not (o_ == h_ == l_ == c_ and v_ == 0))
                ):
                    summary.rejected.append((symbol, price_date, "ohlc"))
                    continue
                planned.append((instrument_id, symbol, price_date, vals, tuple(stored)))
            summary.rows_to_write = len(planned)

            if not dry_run and not summary.rejected:
                for instrument_id, symbol, price_date, (o_, h_, l_, c_, v_), stored in planned:
                    events = _instrument_split_events(cur, instrument_id)
                    cur.execute(
                        """
                        UPDATE prices_daily SET
                            open_split_adj = %s, high_split_adj = %s, low_split_adj = %s,
                            close_split_adj = %s, volume_split_adj = %s,
                            open_raw = %s, high_raw = %s, low_raw = %s, close_raw = %s,
                            adjustment_convention = %s, fetched_at = now()
                        WHERE instrument_id = %s AND price_date = %s AND source = 'yahoo'
                        """,
                        (
                            o_,
                            h_,
                            l_,
                            c_,
                            v_,
                            reconstruct_raw(o_, price_date, events),
                            reconstruct_raw(h_, price_date, events),
                            reconstruct_raw(l_, price_date, events),
                            reconstruct_raw(c_, price_date, events),
                            convention,
                            instrument_id,
                            price_date,
                        ),
                    )
                    _log_ingest_error(
                        cur,
                        source="tradingview",
                        instrument_id=instrument_id,
                        price_date=price_date,
                        error_type="manual_correction",
                        detail=(
                            f"{tag}: old O/H/L/C/V="
                            + "/".join(str(x) for x in stored)
                            + " new="
                            + "/".join(str(x) for x in (o_, h_, l_, c_, v_))
                        ),
                        run_started_at=run_started_at,
                    )
                    summary.rows_written += 1
        for _, symbol, price_date, _, _ in planned:
            summary.per_symbol[symbol] = summary.per_symbol.get(symbol, 0) + 1
            lo, hi = summary.dates_per_symbol.get(symbol, (price_date, price_date))
            summary.dates_per_symbol[symbol] = (min(lo, price_date), max(hi, price_date))
        if not dry_run and not summary.rejected:
            conn.commit()
        else:
            conn.rollback()
    except BaseException:
        conn.rollback()
        raise
    if summary.rejected and not dry_run:
        raise ManualCorrectionRejectedError(summary)
    return summary

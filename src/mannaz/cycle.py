"""Cykl tygodniowy (brief CC-C, C2–C7) — spina import historii, FIFO, bramkę
rejestracji, ceny/FX, ryzyko i raport w jedno polecenie
`python -m mannaz.run_p3 cycle`.

Zasada projektu (bez wyjątków w tym module): nigdzie (stdout, raport) ilości,
kosztów ani kwot PER POZYCJA właściciela. Agregaty kont (kapitał C, wartość
rachunku KONTRAKTOWY K, heat w %) są dozwolone — patrz `risk.RiskSummary`.

Kolejność etapów (brief CC-C): import -> FIFO -> bramka rejestracji -> ceny/FX
-> ryzyko -> raport. Każdy etap może zakończyć cykl DATA FAILURE (stop przed
kolejnym etapem) — raport powstaje ZAWSZE, także przy stopie i nieoczekiwanym
wyjątku (złapanym per etap i zamienionym w DATA FAILURE, brief C8).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import psycopg

from mannaz.calendar_check import EXCHANGE_TO_CALENDAR_CODE
from mannaz.fifo import run_fifo
from mannaz.fx import NBP_CURRENCIES, FxSummary, run_fx_fetch
from mannaz.import_history import (
    _normalize_title_pattern,
    find_source_files,
    import_parsed_files,
)
from mannaz.parse_history import ParsedFile, parse_source_file
from mannaz.prices import PricesSummary, run_prices_fetch
from mannaz.risk import (
    IncompleteRiskDateError,
    IncompleteRiskItem,
    RiskSummary,
    _default_calendar_facts,
    is_risk_budget_eligible,
    resolve_default_risk_date,
    run_risk,
)

# ---------------------------------------------------------------------------
# Q3 (poprawka po przeglądzie, po STOP przebiegu #2) — wydruk odporny na
# kodowanie konsoli. Diagnoza: konsola Windows w cp1250 nie potrafi zakodować
# emoji użytych w `render_report` (🔴/🟢) -> `UnicodeEncodeError`, EXIT=1.
# ---------------------------------------------------------------------------


def safe_print(text: str, stream: Any = None) -> None:
    """Drukuje `text` na `stream` (domyślnie `sys.stdout`) w sposób odporny na
    kodowanie konsoli. Kolejność prób:
      1. `stream.reconfigure(errors="replace")` (Python 3.7+, TextIOWrapper) —
         znaki spoza strony kodowej konsoli zamieniane na '?', bez wyjątku.
      2. Gdy `stream` nie wspiera `reconfigure` (obronnie — np. bufor bez
         tekstowego wrappera): zapis bajtów UTF-8 z `errors="replace"`
         bezpośrednio do `stream.buffer`.
      3. Ostateczny fallback: transliteracja do ASCII (`errors="replace"`).
    Używane w `cmd_cycle` (run_p3.py) i wszędzie, gdzie `cycle.py` drukuje —
    NIGDY nie rzuca `UnicodeEncodeError`."""
    if stream is None:
        stream = sys.stdout
    try:
        stream.reconfigure(errors="replace")
        print(text, file=stream)
        return
    except (AttributeError, ValueError, OSError):
        pass
    try:
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.write(text.encode("utf-8", errors="replace") + b"\n")
            buffer.flush()
            return
    except (AttributeError, ValueError, OSError, UnicodeEncodeError):
        pass
    print(text.encode("ascii", errors="replace").decode("ascii"), file=stream)


# ---------------------------------------------------------------------------
# C2 — ciągłość historii (czysta funkcja)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContinuityGap:
    rachunek: str
    file_start_date: date
    last_known_date: date

    def as_data_failure(self) -> str:
        return (
            f"dziura w historii: rachunek {self.rachunek}, plik zaczyna się "
            f"{self.file_start_date.isoformat()}, ostatnia zaimportowana "
            f"{self.last_known_date.isoformat()}"
        )


def check_file_continuity(
    file_min_dates_by_rachunek: dict[str, date],
    known_max_dates_by_rachunek: dict[str, date],
) -> list[ContinuityGap]:
    """Brief CC-C, C2: per rachunek W PLIKU, `min(data w pliku) <= max(data w
    bazie)` jest warunkiem OK (brak dziury) — naruszenie to
    `min(data w pliku) > max(data w bazie)`. Rachunek bez historii w bazie
    (nieobecny w `known_max_dates_by_rachunek`) -> OK (brak wpisu).
    `known_max_dates_by_rachunek` reprezentuje stan bazy PRZED importem TEGO
    pliku (baza + pliki już zaimportowane wcześniej w tym samym przebiegu —
    odpowiedzialność wywołującego, `run_cycle` aktualizuje ten słownik po
    każdym udanym imporcie pliku)."""
    gaps: list[ContinuityGap] = []
    for rachunek, file_min in sorted(file_min_dates_by_rachunek.items()):
        last_known = known_max_dates_by_rachunek.get(rachunek)
        if last_known is None:
            continue
        if file_min > last_known:
            gaps.append(ContinuityGap(rachunek, file_min, last_known))
    return gaps


def _min_dates_by_rachunek(pf: ParsedFile) -> dict[str, date]:
    out: dict[str, date] = {}
    for row in pf.rows:
        cur = out.get(row.rachunek)
        out[row.rachunek] = row.transaction_date if cur is None else min(cur, row.transaction_date)
    return out


# ---------------------------------------------------------------------------
# C4 — bramka rejestracji (czysta funkcja)
# ---------------------------------------------------------------------------

WHERE_SYMBOL = (
    "instruments.yahoo_symbol/exchange — ręcznie po weryfikacji (reguły P3.1 w instruments_map.py)"
)
WHERE_THEME = "instruments.theme — wzór sql/seed_themes.sql"
WHERE_MULTIPLIER = (
    "instruments.multiplier + multiplier_source — migracja wg specyfikacji GPW, "
    "wzór sql/007_futures_multipliers.sql"
)
WHERE_BASE = "instruments.base_symbol + instrument bazowy z yahoo_symbol"
WHERE_UNSUPPORTED_TYPE = (
    "brak — typ instrumentu (certyfikat/nieznany) nie ma zdefiniowanej ścieżki uzupełnienia w P3.1/ryzyku"
)


@dataclass(frozen=True)
class Gap:
    ticker: str
    rachunek: str
    brak: str
    gdzie_uzupelnic: str


@dataclass
class RegistrationRow:
    """Wejście `registration_gaps` — JEDEN wiersz per pozycja otwarta po FIFO,
    JOIN `instruments` (i, dla kontraktów, instrument bazowy jeśli istnieje).
    Czysta reprezentacja — bez kursora/bazy, wypełniana przez `run_cycle`."""

    rachunek: str
    broker_ticker: str
    instrument_type: str
    is_core: bool
    currency: str | None = None
    exchange: str | None = None
    theme: str | None = None
    yahoo_symbol: str | None = None
    base_symbol: str | None = None
    base_instrument_found: bool = False
    base_currency: str | None = None
    base_exchange: str | None = None
    multiplier: Decimal | None = None
    multiplier_source: str | None = None


def is_satellite(instrument_type: str, is_core: bool) -> bool:
    """Brief CC-C, C4: satelita = `is_risk_budget_eligible` PLUS pozycje
    nie-core z `instrument_type IN ('certificate', 'unknown')` (zawsze braki:
    typ nieobsługiwany przez ryzyko). JEDYNA definicja satelity w tym module —
    poprawka po przeglądzie nadzorcy (P4): „Do rejestracji (§12): brak
    archetypu” używa TEJ SAMEJ funkcji, żeby core nigdy nie trafiał na listę."""
    if is_risk_budget_eligible(instrument_type, is_core):
        return True
    return (not is_core) and instrument_type in ("certificate", "unknown")


def _is_satellite_for_registration(row: RegistrationRow) -> bool:
    return is_satellite(row.instrument_type, row.is_core)


def filter_satellite_tickers(rows: list[tuple[str, str, bool]]) -> list[str]:
    """Poprawka po przeglądzie nadzorcy (P4): „Do rejestracji (§12): brak
    archetypu” tylko dla satelity (bez `is_core`) — ta sama definicja co
    bramka rejestracji (`is_satellite`). Czysta funkcja — testowalna bez bazy;
    `_archetype_missing_tickers` deleguje do niej po pobraniu wierszy z bazy.
    rows: [(broker_ticker, instrument_type, is_core), ...]."""
    return [ticker for ticker, instrument_type, is_core in rows if is_satellite(instrument_type, is_core)]


def registration_gaps(rows: list[RegistrationRow]) -> list[Gap]:
    """Brief CC-C, C4: minimum rejestracji per typ instrumentu satelity. Czysta
    funkcja — testowalna bez bazy (`tests/test_cycle.py`)."""
    gaps: list[Gap] = []
    for row in rows:
        if not _is_satellite_for_registration(row):
            continue

        if row.instrument_type in ("certificate", "unknown"):
            gaps.append(
                Gap(row.broker_ticker, row.rachunek, "typ instrumentu nieobsługiwany przez ryzyko", WHERE_UNSUPPORTED_TYPE)
            )
            continue

        if row.instrument_type in ("equity", "etf"):
            if not row.yahoo_symbol:
                gaps.append(Gap(row.broker_ticker, row.rachunek, "brak instruments.yahoo_symbol", WHERE_SYMBOL))
            if not row.currency:
                gaps.append(Gap(row.broker_ticker, row.rachunek, "brak instruments.currency", WHERE_SYMBOL))
            if not row.exchange or row.exchange not in EXCHANGE_TO_CALENDAR_CODE:
                gaps.append(
                    Gap(row.broker_ticker, row.rachunek, "brak instruments.exchange z kluczem w EXCHANGE_TO_CALENDAR_CODE", WHERE_SYMBOL)
                )
            if not row.theme:
                gaps.append(Gap(row.broker_ticker, row.rachunek, "brak instruments.theme", WHERE_THEME))
            continue

        if row.instrument_type == "future":
            if not row.base_symbol or not row.base_instrument_found:
                gaps.append(
                    Gap(row.broker_ticker, row.rachunek, "brak instruments.base_symbol / instrumentu bazowego", WHERE_BASE)
                )
            else:
                if not row.base_currency:
                    gaps.append(Gap(row.broker_ticker, row.rachunek, "brak currency instrumentu bazowego", WHERE_SYMBOL))
                if not row.base_exchange or row.base_exchange not in EXCHANGE_TO_CALENDAR_CODE:
                    gaps.append(
                        Gap(row.broker_ticker, row.rachunek, "brak exchange instrumentu bazowego z kluczem w EXCHANGE_TO_CALENDAR_CODE", WHERE_SYMBOL)
                    )
            if not row.theme:
                gaps.append(Gap(row.broker_ticker, row.rachunek, "brak instruments.theme (na wierszu kontraktu)", WHERE_THEME))
            if row.multiplier is None or row.multiplier_source is None:
                gaps.append(
                    Gap(row.broker_ticker, row.rachunek, "brak instruments.multiplier / multiplier_source", WHERE_MULTIPLIER)
                )

    return gaps


# ---------------------------------------------------------------------------
# C3 — diff pozycji (czysta funkcja)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionSnapshotRow:
    rachunek: str
    broker_ticker: str
    instrument_id: int
    currency: str
    qty: Decimal


@dataclass(frozen=True)
class PositionChange:
    broker_ticker: str
    rachunek: str
    rodzaj: str  # 'nowa' | 'zamknięta' | 'zmiana ilości ↑' | 'zmiana ilości ↓'


def diff_positions(
    before: list[PositionSnapshotRow], after: list[PositionSnapshotRow]
) -> list[PositionChange]:
    """Brief CC-C, C3: różnica snapshotów `positions_fifo` (przed/po
    `run_fifo`) po kluczu (rachunek, instrument_id, currency) — WYŁĄCZNIE
    kierunek zmiany (↑/↓), nigdy liczby (zasada projektu)."""

    def key(r: PositionSnapshotRow) -> tuple[str, int, str]:
        return (r.rachunek, r.instrument_id, r.currency)

    before_map = {key(r): r for r in before}
    after_map = {key(r): r for r in after}

    changes: list[PositionChange] = []
    for k in sorted(after_map, key=lambda t: (t[0], t[2], t[1])):
        a = after_map[k]
        if k not in before_map:
            changes.append(PositionChange(a.broker_ticker, a.rachunek, "nowa"))
            continue
        b = before_map[k]
        if abs(a.qty) > abs(b.qty):
            changes.append(PositionChange(a.broker_ticker, a.rachunek, "zmiana ilości ↑"))
        elif abs(a.qty) < abs(b.qty):
            changes.append(PositionChange(a.broker_ticker, a.rachunek, "zmiana ilości ↓"))

    for k in sorted(before_map, key=lambda t: (t[0], t[2], t[1])):
        if k not in after_map:
            b = before_map[k]
            changes.append(PositionChange(b.broker_ticker, b.rachunek, "zamknięta"))

    return changes


# ---------------------------------------------------------------------------
# C7 §2 — zdarzenie przecięcia stopu (M69 EVENT; CC-U V3) — czysta funkcja
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StopEventRow:
    """Wejście `stop_events` — JEDEN wiersz per pozycja z `risk_daily` na D
    (nie tylko satelita — filtr satelity jest wewnątrz `stop_events`)."""

    broker_ticker: str
    rachunek: str
    instrument_id: int
    instrument_type: str
    is_core: bool
    below_stop: bool | None  # risk.PositionRiskRow.below_stop (is_breached, ostra nierówność)
    price_date_used: date | None
    close_d: Decimal | None
    stop_effective: Decimal | None
    stop_source: str | None
    price_source_symbol: str | None  # symbol Yahoo instrumentu CENOWEGO (dla kontraktu: baza)
    currency: str


@dataclass(frozen=True)
class StopEvent:
    broker_ticker: str
    rachunek: str
    price_date_used: date | None
    close_d: Decimal | None
    stop_effective: Decimal | None
    stop_source: str | None
    price_source_symbol: str | None
    currency: str


def stop_events(
    rows: list[StopEventRow],
    prev_risk_state_by_key: dict[tuple[str, int], str | None],
) -> list[StopEvent]:
    """Poprawka po przeglądzie sesji głównej (CC-C): §2 raportu = ZDARZENIE
    przecięcia stopu od poprzedniej oceny (M69 EVENT; CC-U V3) — NIE lista
    wszystkich pozycji aktualnie pod stopem. Tylko satelita
    (`is_risk_budget_eligible`); zdarzenie, gdy `below_stop is True` na D
    ORAZ (brak wiersza w `risk_daily` na `prev_date` dla (rachunek,
    instrument_id) LUB `risk_state` na `prev_date` różny od `'HIGH'`) —
    pozycja, która była HIGH już na poprzedniej ocenie, nie generuje nowego
    zdarzenia. `prev_risk_state_by_key`: (rachunek, instrument_id) ->
    risk_state na `prev_date` (klucz nieobecny = brak wiersza na prev_date =
    zawsze zdarzenie, gdy below_stop)."""
    events: list[StopEvent] = []
    for row in rows:
        if not is_risk_budget_eligible(row.instrument_type, row.is_core):
            continue
        if row.below_stop is not True:
            continue
        key = (row.rachunek, row.instrument_id)
        if prev_risk_state_by_key.get(key) == "HIGH":
            continue
        events.append(
            StopEvent(
                broker_ticker=row.broker_ticker,
                rachunek=row.rachunek,
                price_date_used=row.price_date_used,
                close_d=row.close_d,
                stop_effective=row.stop_effective,
                stop_source=row.stop_source,
                price_source_symbol=row.price_source_symbol,
                currency=row.currency,
            )
        )
    return events


# ---------------------------------------------------------------------------
# C7 §3 — zapadka zlecenia stop u brokera (czysta funkcja)
# ---------------------------------------------------------------------------

# T38 [S], przegląd po F6 (shadow mode): próg minimalnej zmiany stopu, poniżej
# którego zapadka NIE każe zmieniać zlecenia u brokera, mimo ruchu w dobrą
# stronę — 0,25 * ATR22 na D (waluta jak stop; dla kontraktu ATR instrumentu
# BAZOWEGO, bo `row.atr22` jest liczone na bazie — patrz `risk.py`, P4.2).
STOP_ORDER_MIN_CHANGE_ATR = Decimal("0.25")

STOP_ORDER_HIGH_MESSAGE = "HIGH — zlecenie stop na tym poziomie wykonałoby się natychmiast"


def stop_order_decision(
    position_kind: str,
    stop_d: Decimal | None,
    stop_prev: Decimal | None,
    atr22_d: Decimal | None = None,
    below_stop: bool | None = None,
) -> str:
    """Brief CC-C, C7 §3 (M62), poprawka po przeglądzie nadzorcy (P1/P3):
      - `below_stop is True` (stan HIGH) -> `STOP_ORDER_HIGH_MESSAGE`, bez
        względu na resztę — pozycja już jest pod stopem, pytanie o zapadkę
        jest bezprzedmiotowe;
      - brak `stop_prev` -> 'tak (nowa pozycja)';
      - ruch w DOBRĄ stronę (long: `stop_d > stop_prev`; short: `stop_d <
        stop_prev`) I `|stop_d - stop_prev| >= 0,25 * atr22_d` -> 'tak';
      - ruch w dobrą stronę, ale poniżej progu -> 'nie (zmiana < 0,25×ATR22)';
      - ruch w dobrą stronę, brak `atr22_d` (za krótka historia) -> 'tak (brak
        ATR22 — bez progu)' (nie blokujemy aktualizacji brakiem progu);
      - bez zmiany (`stop_d == stop_prev`) -> 'nie';
      - ruch w złą stronę (zapadka trzyma poprzedni poziom) ->
        'nie (zapadka: zostaw poprzedni poziom)'."""
    if below_stop is True:
        return STOP_ORDER_HIGH_MESSAGE
    if stop_prev is None:
        return "tak (nowa pozycja)"
    if stop_d is None:
        return "nie"  # obronnie — nie powinno wystąpić dla pozycji policzonej na D
    is_short = position_kind == "short"
    good_direction = (stop_d < stop_prev) if is_short else (stop_d > stop_prev)
    if not good_direction:
        if stop_d == stop_prev:
            return "nie"
        return "nie (zapadka: zostaw poprzedni poziom)"
    if atr22_d is None:
        return "tak (brak ATR22 — bez progu)"
    threshold = STOP_ORDER_MIN_CHANGE_ATR * atr22_d
    if abs(stop_d - stop_prev) >= threshold:
        return "tak"
    return "nie (zmiana < 0,25×ATR22)"


@dataclass(frozen=True)
class StopOrderRow:
    broker_ticker: str
    rachunek: str
    position_kind: str  # 'long' | 'short'
    state: str  # 'HIGH' | 'NORMAL' (poprawka po przeglądzie, P1)
    stop_d: Decimal | None
    stop_d_currency: str
    stop_prev: Decimal | None
    atr22_threshold: Decimal | None  # 0,25*ATR22 na D, w stop_d_currency; None gdy brak ATR22
    base_symbol_label: str | None  # kontrakty: symbol bazy ("poziom na bazie"); inaczej None
    decision: str


def stop_change_arrow(stop_d: Decimal | None, stop_prev: Decimal | None) -> str:
    """Poprawka po przeglądzie: kolumna „zmiana” w M62 pokazuje kierunek
    (↑/↓/bez zmiany), nie słowny opis. `stop_prev is None` (nowa pozycja) ->
    'brak' (nie ma punktu odniesienia)."""
    if stop_prev is None or stop_d is None:
        return "brak"
    if stop_d > stop_prev:
        return "↑"
    if stop_d < stop_prev:
        return "↓"
    return "bez zmiany"


def build_stop_order_rows(
    items: list[
        tuple[str, str, str, Decimal | None, str, Decimal | None, str | None, Decimal | None, bool | None]
    ]
) -> list[StopOrderRow]:
    """items: (broker_ticker, rachunek, position_kind, stop_d, stop_d_currency,
    stop_prev, base_symbol_label, atr22_d, below_stop), jeden per pozycja
    satelity otwarta na D w `risk_daily`. Zwraca wiersze posortowane:
    najpierw stan HIGH, potem decyzje zaczynające się od „tak”."""
    rows: list[StopOrderRow] = []
    for ticker, rachunek, kind, stop_d, ccy, stop_prev, base_label, atr22_d, below_stop in items:
        state = "HIGH" if below_stop is True else "NORMAL"
        threshold = STOP_ORDER_MIN_CHANGE_ATR * atr22_d if atr22_d is not None else None
        decision = stop_order_decision(kind, stop_d, stop_prev, atr22_d, below_stop)
        rows.append(
            StopOrderRow(ticker, rachunek, kind, state, stop_d, ccy, stop_prev, threshold, base_label, decision)
        )
    rows.sort(
        key=lambda r: (
            0 if r.state == "HIGH" else (1 if r.decision.startswith("tak") else 2),
            r.rachunek,
            r.broker_ticker,
        )
    )
    return rows


# ---------------------------------------------------------------------------
# T28 — świeżość cen rynków otwartych pozycji satelity (czysta funkcja)
# ---------------------------------------------------------------------------

SessionsFn = Callable[[str, date, date], list[date]]


@dataclass(frozen=True)
class MarketFreshnessResult:
    calendar_code: str
    freshest_price_date: date
    age_sessions: int
    is_data_failure: bool


def _default_sessions_fn(calendar_code: str, start: date, end: date) -> list[date]:
    if start > end:
        return []
    import exchange_calendars as xcals

    cal = xcals.get_calendar(calendar_code)
    return [ts.date() for ts in cal.sessions_in_range(start.isoformat(), end.isoformat())]


# Poprawka po przeglądzie: kolumna "rynek" w T28 pokazuje nazwę giełdy
# (GPW/NASDAQ/…), nie kod kalendarza — odwrócenie EXCHANGE_TO_CALENDAR_CODE.
CALENDAR_CODE_TO_EXCHANGE: dict[str, str] = {v: k for k, v in EXCHANGE_TO_CALENDAR_CODE.items()}

MAX_STALE_SESSIONS = 2


def market_freshness(
    price_dates_by_calendar: dict[str, list[date]],
    sessions_fn: SessionsFn,
    today: date,
) -> list[MarketFreshnessResult]:
    """Brief CC-C, C7 (T28): rynki = kalendarze instrumentów cenowych otwartych
    pozycji satelity (dla kontraktu — baza). `price_dates_by_calendar`:
    calendar_code -> WSZYSTKIE daty cen w `prices_daily` dla instrumentów tego
    rynku (wywołujący liczy `max()` per rynek jest tu). Odniesienie = ostatnia
    sesja kalendarza ściśle przed `today`. Wiek = liczba sesji w (najświeższa
    cena, odniesienie] — dokładnie sesje w przedziale otwartym
    (najświeższa, today) (odniesienie jest z definicji ostatnią sesją < today,
    więc ten przedział jest identyczny). > 2 sesje -> DATA FAILURE."""
    results: list[MarketFreshnessResult] = []
    for calendar_code in sorted(price_dates_by_calendar):
        dates = price_dates_by_calendar[calendar_code]
        if not dates:
            continue
        freshest = max(dates)
        sessions_between = sessions_fn(calendar_code, freshest + timedelta(days=1), today - timedelta(days=1))
        age = len(sessions_between)
        results.append(MarketFreshnessResult(calendar_code, freshest, age, age > MAX_STALE_SESSIONS))
    return results


# ---------------------------------------------------------------------------
# Formatowanie liczb (brief CC-C, C7): przecinek dziesiętny, spacja tysięcy.
# ---------------------------------------------------------------------------


def format_number(value: Decimal | None, decimals: int = 2) -> str:
    if value is None:
        return "brak"
    quant = Decimal(1).scaleb(-decimals)
    q = value.quantize(quant)
    s = f"{q:,.{decimals}f}"
    s = s.replace(",", "\x00").replace(".", ",").replace("\x00", " ")
    return s


def format_pct(value: Decimal | None) -> str:
    if value is None:
        return "brak"
    return f"{format_number(value)}%"


def format_money(value: Decimal | None, currency: str) -> str:
    if value is None:
        return "brak"
    return f"{format_number(value)} {currency}"


# ---------------------------------------------------------------------------
# C7 — raport (czysta funkcja render_report + stan wejściowy)
# ---------------------------------------------------------------------------

STAGE_IMPORT = "import"
STAGE_FIFO = "fifo"
STAGE_REGISTRATION = "rejestracja"
STAGE_PRICES_FX = "ceny_fx"
STAGE_RISK = "ryzyko"

STAGE_LABELS = {
    STAGE_IMPORT: "import",
    STAGE_FIFO: "FIFO",
    STAGE_REGISTRATION: "bramka rejestracji",
    STAGE_PRICES_FX: "ceny/FX",
    STAGE_RISK: "ryzyko",
}


@dataclass
class ProcessedFileReportRow:
    original_name: str
    sha8: str
    status: str  # 'przetworzony' | 'pominięty (sha256 już przetworzony)'
    per_rachunek: dict[str, tuple[int, int]] = field(default_factory=dict)  # rachunek -> (new, duplicate)
    date_range: tuple[date, date] | None = None


def _satellite_capital_eligible(instrument_type: str, is_core: bool) -> bool:
    """Poprawka po przeglądzie nadzorcy (P1): DOKŁADNIE ta sama definicja,
    której `risk.run_risk` używa, by policzyć wartość pozycji wchodzącej do
    kapitału satelity C i do `zagraniczny_satellite_value_pct_below_stop`
    (risk.py: `is_satellite_capital_eligible = row.instrument_type in
    ("equity", "etf") and not row.is_core`) — equity/etf spoza core;
    kontrakty NIE (ich wartość wchodzi do C przez K = wartość rachunku
    KONTRAKTOWY, nie przez nominał pozycji)."""
    return instrument_type in ("equity", "etf") and not is_core


@dataclass
class RiskReportAggregates:
    """Podzbiór `risk.RiskSummary` faktycznie używany w sekcji „Ryzyko” —
    WYŁĄCZNIE agregaty kont/procenty (zasada projektu)."""

    heat_pct_total: Decimal | None
    heat_pct_zagraniczny: Decimal | None
    level1_max_ticker: str | None
    level1_max_pct: Decimal | None
    level1_breach_tickers: list[str]
    theme_ranking: list[tuple[str, Decimal | None, bool | None]]  # (theme, pct, breach)
    level3_breach: bool | None
    capital_satelite_positions_pln: Decimal
    kontraktowy_account_value_pln: Decimal
    capital_satelite_pln: Decimal
    stale_tickers: list[tuple[str, date]]  # (ticker, price_date_used)
    high_risk_tickers: list[str] = field(default_factory=list)  # satelita, below_stop=True na D (RISK=HIGH)
    # Poprawka po przeglądzie nadzorcy (P1): udział wartości pozycji HIGH w C —
    # None gdy C==0 albo brak pozycji equity/etf HIGH z policzalną wartością.
    high_risk_capital_pct: Decimal | None = None
    # Kontrakty HIGH — poza `high_risk_capital_pct` (ich nominał nie wchodzi
    # do C, patrz `_satellite_capital_eligible`), wymienione osobno w raporcie.
    high_risk_contract_tickers: list[str] = field(default_factory=list)

    @classmethod
    def from_risk_summary(cls, summary: RiskSummary) -> "RiskReportAggregates":
        eligible_rows = [r for r in summary.rows if r.risk_pct_satellite_capital is not None]
        if eligible_rows:
            top = max(eligible_rows, key=lambda r: r.risk_pct_satellite_capital)
            level1_max_ticker, level1_max_pct = top.broker_ticker, top.risk_pct_satellite_capital
        else:
            level1_max_ticker, level1_max_pct = None, None
        theme_ranking = sorted(
            ((t, r.risk_pct, r.breach) for t, r in summary.theme_budgets.items()),
            key=lambda t: (t[1] is None, -(t[1] or Decimal(0))),
        )
        stale = [
            (r.broker_ticker, r.price_date_used)
            for r in summary.rows
            if r.price_is_stale and r.price_date_used is not None
        ]
        high_risk_rows = [
            r for r in summary.rows if r.below_stop is True and is_risk_budget_eligible(r.instrument_type, r.is_core)
        ]
        high_risk = [r.broker_ticker for r in high_risk_rows]
        high_risk_contracts = [r.broker_ticker for r in high_risk_rows if r.instrument_type == "future"]
        high_risk_capital_value_pln = Decimal(0)
        for r in high_risk_rows:
            if (
                _satellite_capital_eligible(r.instrument_type, r.is_core)
                and r.close_d is not None
                and r.fx_rate is not None
            ):
                # risk.py, run_risk (poziom pozycji ZAGRANICZNY/below_stop):
                # value_pln = row.close_d * row.qty * row.fx_rate — IDENTYCZNA
                # formuła, żeby udział HIGH w C liczyć tą samą metodą co
                # `zagraniczny_satellite_value_pct_below_stop`.
                high_risk_capital_value_pln += r.close_d * r.qty * r.fx_rate
        high_risk_capital_pct = (
            high_risk_capital_value_pln / summary.capital_satelite_pln * Decimal(100)
            if summary.capital_satelite_pln
            else None
        )
        return cls(
            heat_pct_total=summary.total_risk_pct_satellite_capital,
            heat_pct_zagraniczny=summary.total_risk_pct_zagraniczny_satellite_capital,
            level1_max_ticker=level1_max_ticker,
            level1_max_pct=level1_max_pct,
            level1_breach_tickers=list(summary.level1_breach_tickers),
            theme_ranking=theme_ranking,
            level3_breach=summary.level3_breach,
            capital_satelite_positions_pln=summary.capital_satelite_positions_pln,
            kontraktowy_account_value_pln=summary.kontraktowy_account_value_pln,
            capital_satelite_pln=summary.capital_satelite_pln,
            stale_tickers=stale,
            high_risk_tickers=high_risk,
            high_risk_capital_pct=high_risk_capital_pct,
            high_risk_contract_tickers=high_risk_contracts,
        )


@dataclass
class ReportState:
    """Stan wejściowy `render_report` — czysty (bez bazy), budowany przez
    `run_cycle`. Wszystkie listy failure puste => sekcja "brak" => kod 0."""

    d: date | None
    run_started_at: datetime
    command: str = "python -m mannaz.run_p3 cycle"
    exit_code: int = 0
    runtime_seconds: float = 0.0

    # etapy niewykonane (stage_key -> True), do komunikatu "nie wykonano"
    stage_not_executed: set[str] = field(default_factory=set)

    # DATA FAILURE — podsekcje
    import_failures: list[str] = field(default_factory=list)
    registration_queue: list[Gap] = field(default_factory=list)
    price_ingest_errors: list[str] = field(default_factory=list)
    fx_failures: list[str] = field(default_factory=list)
    date_incomplete_items: list[IncompleteRiskItem] = field(default_factory=list)
    d_resolution_failure: str | None = None
    freshness_results: list[MarketFreshnessResult] = field(default_factory=list)

    # STOP (§2) — ZDARZENIE przecięcia stopu (M69 EVENT; CC-U V3), nie lista
    # wszystkich pozycji pod stopem; patrz `stop_events`.
    stop_events: list[StopEvent] = field(default_factory=list)
    prev_risk_date: date | None = None

    # Zlecenia stop (M62)
    stop_order_rows: list[StopOrderRow] = field(default_factory=list)

    # Ryzyko
    risk_aggregates: RiskReportAggregates | None = None

    # Zmiany pozycji
    position_changes: list[PositionChange] = field(default_factory=list)
    archetype_missing: list[str] = field(default_factory=list)

    # HEARTBEAT
    heartbeat_covered: int = 0
    heartbeat_total: int = 0
    files_processed: list[ProcessedFileReportRow] = field(default_factory=list)
    files_skipped: list[ProcessedFileReportRow] = field(default_factory=list)


def has_data_failures(state: ReportState) -> bool:
    return bool(
        state.import_failures
        or state.registration_queue
        or state.price_ingest_errors
        or state.fx_failures
        or state.date_incomplete_items
        or state.d_resolution_failure is not None
        or any(f.is_data_failure for f in state.freshness_results)
    )


def _fmt_date(d: date | None) -> str:
    return d.isoformat() if d is not None else "brak"


def _render_data_failure_section(state: ReportState) -> list[str]:
    lines = ["## 🔴 DATA FAILURE"]

    def stage_note(stage: str) -> str | None:
        if stage in state.stage_not_executed:
            return f"nie wykonano (cykl zatrzymany na etapie {STAGE_LABELS.get(stage, stage)})"
        return None

    lines.append("### Błędy importu (ciągłość, unknown, check-ignore, wyjątki etapów)")
    note = stage_note(STAGE_IMPORT)
    if note:
        lines.append(note)
    elif state.import_failures:
        for f in state.import_failures:
            lines.append(f"- {f}")
    else:
        lines.append("brak")

    lines.append("")
    lines.append("### Kolejka rejestracji (bramka)")
    note = stage_note(STAGE_REGISTRATION)
    if note:
        lines.append(note)
    elif state.registration_queue:
        lines.append("| ticker | rachunek | czego brakuje | gdzie uzupełnić |")
        lines.append("|---|---|---|---|")
        for g in state.registration_queue:
            lines.append(f"| {g.ticker} | {g.rachunek} | {g.brak} | {g.gdzie_uzupelnic} |")
    else:
        lines.append("brak")

    lines.append("")
    lines.append("### ingest_errors (ten przebieg)")
    note = stage_note(STAGE_PRICES_FX)
    if note:
        lines.append(note)
    elif state.price_ingest_errors:
        for e in state.price_ingest_errors:
            lines.append(f"- {e}")
    else:
        lines.append("brak")

    lines.append("")
    lines.append("### FX")
    if note:
        lines.append(note)
    elif state.fx_failures:
        for e in state.fx_failures:
            lines.append(f"- {e}")
    else:
        lines.append("brak")

    lines.append("")
    lines.append("### D niekompletne")
    note = stage_note(STAGE_RISK)
    if note:
        lines.append(note)
    elif state.date_incomplete_items or state.d_resolution_failure:
        if state.d_resolution_failure:
            lines.append(f"- {state.d_resolution_failure}")
        for it in state.date_incomplete_items:
            lines.append(
                f"- {it.broker_ticker} · {it.price_instrument or 'brak'} · {it.exchange or 'brak'} · {it.reason}"
            )
    else:
        lines.append("brak")

    lines.append("")
    lines.append("### Świeżość cen (T28)")
    if note:
        lines.append(note)
    elif state.freshness_results:
        # Poprawka po przeglądzie: tabela pokazuje WSZYSTKIE rynki (OK i DATA
        # FAILURE), nie tylko failujące — plus podsumowująca linia.
        lines.append("| rynek | kalendarz | najświeższa cena | wiek w sesjach | status |")
        lines.append("|---|---|---|---|---|")
        failing_codes: list[str] = []
        for f in sorted(state.freshness_results, key=lambda r: r.calendar_code):
            status = "DATA FAILURE" if f.is_data_failure else "OK"
            market_name = CALENDAR_CODE_TO_EXCHANGE.get(f.calendar_code, f.calendar_code)
            if f.is_data_failure:
                failing_codes.append(f.calendar_code)
            lines.append(
                f"| {market_name} | {f.calendar_code} | {_fmt_date(f.freshest_price_date)} | {f.age_sessions} | {status} |"
            )
        lines.append(f"DATA FAILURE: {', '.join(failing_codes) if failing_codes else 'brak'}")
    else:
        lines.append("brak")

    return lines


def _render_stop_section(state: ReportState) -> list[str]:
    lines = ["## 🔴 STOP"]
    if STAGE_RISK in state.stage_not_executed:
        lines.append(f"nie wykonano (cykl zatrzymany na etapie {STAGE_LABELS[STAGE_RISK]})")
        return lines
    lines.append(
        f"Zdarzenia przecięcia stopu od poprzedniej oceny (M69 EVENT; CC-U V3): "
        f"D={_fmt_date(state.d)}, poprzednia ocena={_fmt_date(state.prev_risk_date)}"
    )
    if not state.stop_events:
        lines.append("brak")
        return lines
    lines.append("| ticker | rachunek | data ceny | close | stop | źródło stopu | źródło ceny |")
    lines.append("|---|---|---|---|---|---|---|")
    for e in state.stop_events:
        lines.append(
            f"| {e.broker_ticker} | {e.rachunek} | {_fmt_date(e.price_date_used)} | "
            f"{format_money(e.close_d, e.currency)} | {format_money(e.stop_effective, e.currency)} | "
            f"{e.stop_source} | {e.price_source_symbol or 'brak'} |"
        )
    return lines


def _render_stop_orders_section(state: ReportState) -> list[str]:
    lines = ["## Zlecenia stop do aktualizacji u brokera (M62)"]
    if STAGE_RISK in state.stage_not_executed:
        lines.append(f"nie wykonano (cykl zatrzymany na etapie {STAGE_LABELS[STAGE_RISK]})")
        return lines
    prev_label = _fmt_date(state.prev_risk_date)
    lines.append(f"D={_fmt_date(state.d)}, poprzednia ocena={prev_label}")
    if not state.stop_order_rows:
        lines.append("brak")
        return lines
    lines.append(
        f"| ticker | rachunek | stan | kierunek | stop D | stop z poprzedniej oceny ({prev_label}) | "
        "zmiana | 0,25×ATR22 | poziom na bazie | zmień zlecenie |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in state.stop_order_rows:
        zmiana = stop_change_arrow(r.stop_d, r.stop_prev)
        lines.append(
            f"| {r.broker_ticker} | {r.rachunek} | {r.state} | {r.position_kind} | "
            f"{format_money(r.stop_d, r.stop_d_currency)} | {format_money(r.stop_prev, r.stop_d_currency)} | "
            f"{zmiana} | {format_money(r.atr22_threshold, r.stop_d_currency)} | "
            f"{r.base_symbol_label or '-'} | {r.decision} |"
        )
    return lines


def _render_risk_section(state: ReportState) -> list[str]:
    lines = ["## Ryzyko"]
    if STAGE_RISK in state.stage_not_executed or state.risk_aggregates is None:
        lines.append(f"nie wykonano (cykl zatrzymany na etapie {STAGE_LABELS[STAGE_RISK]})")
        return lines
    a = state.risk_aggregates
    lines.append(f"- heat ogółem (% kapitału satelity): {format_pct(a.heat_pct_total)}")
    lines.append(f"- heat ZAGRANICZNY (% kapitału satelity): {format_pct(a.heat_pct_zagraniczny)}")
    lines.append(
        f"- poziom 1: max {a.level1_max_ticker or 'brak'} ({format_pct(a.level1_max_pct)}); "
        f"przekroczenia > 1%: {', '.join(a.level1_breach_tickers) if a.level1_breach_tickers else 'brak'}"
    )
    lines.append("- poziom 2 (tematy, próg > 3%):")
    if a.theme_ranking:
        for theme, pct, breach in a.theme_ranking:
            lines.append(f"    - {theme}: {format_pct(pct)} breach={'tak' if breach else 'nie'}")
    else:
        lines.append("    brak")
    lines.append(f"- poziom 3 (> 15%): {'tak' if a.level3_breach else 'nie'}")
    lines.append(f"- C — kapitał satelity (PLN): {format_money(a.capital_satelite_pln, 'PLN')}")
    lines.append(f"    - w tym pozycje equity/etf: {format_money(a.capital_satelite_positions_pln, 'PLN')}")
    lines.append(f"- K — wartość rachunku KONTRAKTOWY (PLN): {format_money(a.kontraktowy_account_value_pln, 'PLN')}")
    lines.append(
        f"- pozycje HIGH: {len(a.high_risk_tickers)}, udział w kapitale satelity {format_pct(a.high_risk_capital_pct)}"
        f" — {', '.join(a.high_risk_tickers) if a.high_risk_tickers else 'brak'}"
    )
    if a.high_risk_contract_tickers:
        lines.append(
            f"    - kontrakty: poza udziałem (C liczy rachunek KONTRAKTOWY jako K): "
            f"{', '.join(a.high_risk_contract_tickers)}"
        )
    if a.stale_tickers:
        lines.append("- pozycje stale (T27):")
        for ticker, d in a.stale_tickers:
            lines.append(f"    - {ticker}: cena z {_fmt_date(d)}")
    else:
        lines.append("- pozycje stale (T27): brak")
    return lines


def _render_position_changes_section(state: ReportState) -> list[str]:
    lines = ["## Zmiany pozycji"]
    if STAGE_FIFO in state.stage_not_executed:
        lines.append(f"nie wykonano (cykl zatrzymany na etapie {STAGE_LABELS[STAGE_FIFO]})")
    elif not state.position_changes:
        lines.append("brak")
    else:
        for c in state.position_changes:
            lines.append(f"- {c.broker_ticker} ({c.rachunek}): {c.rodzaj}")
    lines.append("")
    if state.archetype_missing:
        lines.append(f"Do rejestracji (§12): brak archetypu: {', '.join(state.archetype_missing)}")
    else:
        lines.append("Do rejestracji (§12): brak")
    return lines


def _render_heartbeat_section(state: ReportState) -> list[str]:
    lines = ["## 🟢 HEARTBEAT"]
    lines.append(f"- pokrycie ceną na D: {state.heartbeat_covered}/{state.heartbeat_total}")
    lines.append("- pliki przetworzone:")
    if state.files_processed:
        for f in state.files_processed:
            per_r = "; ".join(f"{r}: nowe={n} zdublowane={d}" for r, (n, d) in sorted(f.per_rachunek.items()))
            rng = (
                f"{f.date_range[0].isoformat()}..{f.date_range[1].isoformat()}"
                if f.date_range is not None
                else "brak"
            )
            lines.append(f"    - {f.original_name} (sha8={f.sha8}) [{rng}]: {per_r or 'brak wierszy'}")
    else:
        lines.append("    brak")
    lines.append("- pliki pominięte:")
    if state.files_skipped:
        for f in state.files_skipped:
            lines.append(f"    - {f.original_name} (sha8={f.sha8}): {f.status}")
    else:
        lines.append("    brak")
    lines.append(f"- czas przebiegu: {state.runtime_seconds:.1f} s")
    return lines


FOOTER = "Raport nie zawiera rekomendacji inwestycyjnych — tylko stany i poziomy wg §19."


def render_report(state: ReportState) -> str:
    """Brief CC-C, C7: renderer czysty (bez bazy) — 6 nagłówków w stałej
    kolejności, po polsku, bez ilości/kwot per pozycja."""
    lines: list[str] = []
    lines.append(f"# Cykl tygodniowy — D={_fmt_date(state.d)}")
    lines.append("")
    lines.append(f"Przebieg: {state.run_started_at.isoformat()}")
    lines.append(f"Polecenie: `{state.command}`")
    lines.append(f"Kod wyjścia: {state.exit_code}")
    lines.append("")

    lines.extend(_render_data_failure_section(state))
    lines.append("")
    lines.extend(_render_stop_section(state))
    lines.append("")
    lines.extend(_render_stop_orders_section(state))
    lines.append("")
    lines.extend(_render_risk_section(state))
    lines.append("")
    lines.extend(_render_position_changes_section(state))
    lines.append("")
    lines.extend(_render_heartbeat_section(state))
    lines.append("")
    lines.append(FOOTER)

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# C8 — orkiestracja (`run_cycle`) + polecenie CLI
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INCOMING_DIR = REPO_ROOT / "_incoming"
DEFAULT_RAW_ARCHIVE_DIR = REPO_ROOT / "data" / "raw" / "financeHistory"
DEFAULT_REPORTS_DIR = REPO_ROOT / "data" / "reports"


def _nearest_existing_ancestor(path: Path) -> Path:
    """Poprawka po przeglądzie (Q1, po STOP przebiegu #2): `git -C <ścieżka>`
    WYMAGA katalogu — `-C <plik>` kończy się kodem 128 ("cannot change to
    ..."), co poprzednia wersja fałszywie odczytywała jako `check-ignore`
    zwracające `False` (bo tylko `returncode == 0` liczy się jako "ignorowane").
    Diagnoza sesji głównej: gdy `path` sam JUŻ ISTNIEJE (np. „plik docelowy
    już istnieje” — kolizja archiwum/raportu), poprzednia pętla `(resolved,
    *resolved.parents)` zwracała `resolved` (plik!) jako "najbliższego
    istniejącego przodka". Start MUSI być zawsze katalogiem — pomijamy
    `resolved` i zaczynamy od `resolved.parent` w górę."""
    resolved = path if path.is_absolute() else path.resolve()
    for candidate in resolved.parents:
        if candidate.exists():
            return candidate
    # resolved.parents zawsze zawiera co najmniej root drive'u/systemu plików —
    # ta gałąź to czysto obronny fallback (root zawsze istnieje).
    return resolved


def _default_check_ignored(path: Path) -> bool:
    """Domyślna implementacja `check_ignored` (brief CC-C, C2/C7, poprawka po
    przeglądzie): `git -C <najbliższy istniejący przodek path> check-ignore -q
    <path>` — True gdy ścieżka jest objęta .gitignore repo, w którym faktycznie
    leży (nie zawsze repo kodu cyklu)."""
    path = Path(path)
    anchor = _nearest_existing_ancestor(path)
    try:
        result = subprocess.run(
            ["git", "-C", str(anchor), "check-ignore", "-q", str(path)],
            capture_output=True,
        )
    except OSError:
        return False
    return result.returncode == 0


def report_base_name(d: date | None, run_started_at: datetime) -> str:
    """Q2 (poprawka po przeglądzie, po STOP przebiegu #2): nazwa raportu BEZ
    rozszerzenia i BEZ sufiksu unikalności — `cykl-<D>_<HHMM>` (HHMM = czas
    LOKALNY startu przebiegu), albo `cykl-przebieg-<YYYY-MM-DD>_<HHMM>` gdy D
    nieustalone. `run_started_at` MOŻE być tz-aware (UTC, jak w `run_cycle`) —
    `.astimezone()` bez argumentu konwertuje do strefy lokalnej systemu."""
    local = run_started_at.astimezone()
    hhmm = local.strftime("%H%M")
    if d is not None:
        return f"cykl-{d.isoformat()}_{hhmm}"
    return f"cykl-przebieg-{local:%Y-%m-%d}_{hhmm}"


def write_report_unique(reports_dir: Path, base_name: str, report_text: str) -> Path:
    """Q2: NIGDY nie nadpisuje istniejącego raportu — pierwsza wolna nazwa
    `<base_name>.md`, `<base_name>_2.md`, `<base_name>_3.md`, ... Otwiera
    plik w trybie `'x'` (ekskluzywne utworzenie — `FileExistsError`, gdy plik
    już istnieje, zamiast cichego nadpisania), więc pierwszy zapisany raport
    zostaje bajtowo nietknięty niezależnie od kolejnych przebiegów o tej samej
    nazwie bazowej. `reports_dir` musi już istnieć (wywołujący robi `mkdir`
    przed wywołaniem, PO asercji check-ignore — brief C7/C8)."""
    suffix = 1
    while True:
        name = f"{base_name}.md" if suffix == 1 else f"{base_name}_{suffix}.md"
        candidate = reports_dir / name
        try:
            with candidate.open("x", encoding="utf-8") as fh:
                fh.write(report_text)
            return candidate
        except FileExistsError:
            suffix += 1


@dataclass
class CycleResult:
    exit_code: int
    report_path: Path | None
    state: ReportState
    # Q3 (poprawka po przeglądzie, po STOP przebiegu #2): komunikat ASCII do
    # wydruku, gdy raport NIE został zapisany (check-ignore odmówiło) —
    # `cmd_cycle` drukuje go zamiast pełnej treści raportu.
    error_message: str | None = None


def _sha8(sha256: str) -> str:
    return sha256[:8]


def _positions_fifo_snapshot(cur: psycopg.Cursor) -> list[PositionSnapshotRow]:
    cur.execute(
        """
        SELECT p.rachunek, i.broker_ticker, p.instrument_id, p.currency, p.qty
        FROM positions_fifo p JOIN instruments i ON i.id = p.instrument_id
        """
    )
    return [
        PositionSnapshotRow(rachunek, ticker, instrument_id, currency, qty)
        for rachunek, ticker, instrument_id, currency, qty in cur.fetchall()
    ]


def _known_max_dates_by_rachunek(cur: psycopg.Cursor) -> dict[str, date]:
    cur.execute("SELECT rachunek, max(transaction_date) FROM transactions GROUP BY rachunek")
    return {r: d for r, d in cur.fetchall() if d is not None}


def _lookup_base_instrument(cur: psycopg.Cursor, base_symbol: str | None) -> dict[str, Any] | None:
    if not base_symbol:
        return None
    cur.execute(
        "SELECT id, currency, yahoo_symbol, exchange FROM instruments WHERE yahoo_symbol = %s",
        (base_symbol,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "currency": row[1], "yahoo_symbol": row[2], "exchange": row[3]}


def _registration_rows(cur: psycopg.Cursor) -> list[RegistrationRow]:
    cur.execute(
        """
        SELECT p.rachunek, i.broker_ticker, i.instrument_type, i.is_core, i.currency,
               i.exchange, i.theme, i.yahoo_symbol, i.base_symbol, i.multiplier, i.multiplier_source
        FROM positions_fifo p JOIN instruments i ON i.id = p.instrument_id
        """
    )
    rows: list[RegistrationRow] = []
    for (
        rachunek, ticker, instrument_type, is_core, currency, exchange, theme,
        yahoo_symbol, base_symbol, multiplier, multiplier_source,
    ) in cur.fetchall():
        base = _lookup_base_instrument(cur, base_symbol) if instrument_type == "future" else None
        rows.append(
            RegistrationRow(
                rachunek=rachunek,
                broker_ticker=ticker,
                instrument_type=instrument_type,
                is_core=bool(is_core),
                currency=currency,
                exchange=exchange,
                theme=theme,
                yahoo_symbol=yahoo_symbol,
                base_symbol=base_symbol,
                base_instrument_found=base is not None,
                base_currency=base["currency"] if base else None,
                base_exchange=base["exchange"] if base else None,
                multiplier=multiplier,
                multiplier_source=multiplier_source,
            )
        )
    return rows


def _archetype_missing_tickers(cur: psycopg.Cursor) -> list[str]:
    cur.execute(
        """
        SELECT DISTINCT i.broker_ticker, i.instrument_type, i.is_core
        FROM positions_fifo p
        JOIN instruments i ON i.id = p.instrument_id
        WHERE NOT EXISTS (
            SELECT 1 FROM archetype_assignments a
            WHERE a.instrument_id = p.instrument_id AND a.valid_to IS NULL
        )
        ORDER BY 1
        """
    )
    rows = [(ticker, itype, bool(is_core)) for ticker, itype, is_core in cur.fetchall()]
    return filter_satellite_tickers(rows)


def _price_source_symbol(cur: psycopg.Cursor, instrument_id: int, instrument_type: str) -> str | None:
    """Poprawka po przeglądzie (STOP §2 + M62 „poziom na bazie”): symbol
    Yahoo instrumentu CENOWEGO — dla equity/etf to własny `yahoo_symbol`, dla
    kontraktu to `base_symbol` (który JEST yahoo_symbol instrumentu bazowego,
    patrz `instruments_map.futures_base_mapping`/`_lookup_base_instrument`)."""
    if instrument_type == "future":
        cur.execute("SELECT base_symbol FROM instruments WHERE id = %s", (instrument_id,))
    else:
        cur.execute("SELECT yahoo_symbol FROM instruments WHERE id = %s", (instrument_id,))
    row = cur.fetchone()
    return row[0] if row else None


def _satellite_price_instruments(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Instrumenty CENOWE (dla kontraktu — baza) otwartych pozycji satelity
    (equity/etf spoza core LUB future z multiplier znanym) — wejście dla STOP,
    zleceń stop i T28."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    cur.execute(
        """
        SELECT p.rachunek, i.id, i.broker_ticker, i.instrument_type, i.is_core, i.base_symbol
        FROM positions_fifo p JOIN instruments i ON i.id = p.instrument_id
        """
    )
    for rachunek, instrument_id, ticker, instrument_type, is_core, base_symbol in cur.fetchall():
        if not is_risk_budget_eligible(instrument_type, bool(is_core)):
            continue
        if instrument_type == "future":
            base = _lookup_base_instrument(cur, base_symbol)
            if base is None:
                continue
            price_instrument_id = base["id"]
            exchange = base["exchange"]
        else:
            price_instrument_id = instrument_id
            cur.execute("SELECT exchange FROM instruments WHERE id = %s", (instrument_id,))
            exchange = cur.fetchone()[0]
        calendar_code = EXCHANGE_TO_CALENDAR_CODE.get(exchange) if exchange else None
        if calendar_code is None:
            continue
        if price_instrument_id in seen:
            continue
        seen.add(price_instrument_id)
        out.append({"instrument_id": price_instrument_id, "calendar_code": calendar_code})
    return out


def _price_dates_by_calendar(cur: psycopg.Cursor, instruments: list[dict[str, Any]]) -> dict[str, list[date]]:
    out: dict[str, list[date]] = {}
    for inst in instruments:
        cur.execute(
            "SELECT price_date FROM prices_daily WHERE instrument_id = %s", (inst["instrument_id"],)
        )
        dates = [r[0] for r in cur.fetchall()]
        out.setdefault(inst["calendar_code"], []).extend(dates)
    return out


def run_cycle(
    conn: psycopg.Connection,
    *,
    incoming_dir: Path = DEFAULT_INCOMING_DIR,
    raw_archive_dir: Path = DEFAULT_RAW_ARCHIVE_DIR,
    reports_dir: Path = DEFAULT_REPORTS_DIR,
    today: date | None = None,
    commit: bool = True,
    fetch_prices: Callable[..., PricesSummary] = run_prices_fetch,
    fetch_fx: Callable[..., FxSummary] = run_fx_fetch,
    risk_fn: Callable[..., RiskSummary] = run_risk,
    resolve_date_fn: Callable[..., date | None] = resolve_default_risk_date,
    calendar_facts_fn: Callable[[str | None, date], Any] = _default_calendar_facts,
    sessions_fn: SessionsFn = _default_sessions_fn,
    check_ignored: Callable[[Path], bool] = _default_check_ignored,
) -> CycleResult:
    """Brief CC-C, C2–C8: orkiestracja jednego przebiegu cyklu tygodniowego.
    Etapy w kolejności: import -> FIFO -> bramka rejestracji -> ceny/FX ->
    ryzyko -> raport. Wyjątek złapany per etap zamienia się w DATA FAILURE
    (raport powstaje zawsze)."""
    run_started_at = datetime.now(timezone.utc)
    t_start = time.monotonic()
    if today is None:
        today = date.today()

    state = ReportState(d=None, run_started_at=run_started_at)

    try:
        # --- C2: import ---------------------------------------------------
        with conn.cursor() as cur:
            known_max_dates = _known_max_dates_by_rachunek(cur)
            cur.execute("SELECT sha256 FROM processed_history_files")
            already_processed = {r[0] for r in cur.fetchall()}

        source_files = find_source_files(incoming_dir)
        parsed = [parse_source_file(f) for f in source_files]
        parsed.sort(key=lambda pf: min((r.transaction_date for r in pf.rows), default=date.min))

        import_stopped = False
        for pf in parsed:
            if pf.sha256 in already_processed:
                state.files_skipped.append(
                    ProcessedFileReportRow(pf.path.name, _sha8(pf.sha256), "pominięty (sha256 już przetworzony)")
                )
                continue

            file_min_dates = _min_dates_by_rachunek(pf)
            gaps = check_file_continuity(file_min_dates, known_max_dates)
            if gaps:
                for g in gaps:
                    state.import_failures.append(g.as_data_failure())
                import_stopped = True
                break

            # Poprawka po przeglądzie: unknown/check-ignore/exists sprawdzane
            # PRZED importem (`import_parsed_files`) — jeśli plik nie może
            # zostać w pełni przetworzony i zarchiwizowany, NIC się nie
            # zapisuje (ani transakcje, ani rejestr).
            if pf.unknown_titles:
                pattern_counts: dict[str, int] = {}
                for title in pf.unknown_titles:
                    pattern = _normalize_title_pattern(title)
                    pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1
                patterns = ", ".join(f"{p!r}×{n}" for p, n in sorted(pattern_counts.items()))
                state.import_failures.append(
                    f"plik {pf.path.name}: nieznane wzorce tytułów: {patterns} — plik NIE zaimportowany"
                )
                import_stopped = True
                break

            all_dates: list[date] = [r.transaction_date for r in pf.rows]
            last_date = max(all_dates) if all_dates else None
            first_date = min(all_dates) if all_dates else None

            target = raw_archive_dir / f"{last_date:%Y-%m-%d}_{pf.sha256[:8]}.csv"
            if not check_ignored(target):
                state.import_failures.append(
                    f"plik {pf.path.name}: cel archiwizacji {target} nie jest objęty .gitignore (check-ignore) — "
                    "plik NIE zaimportowany/przeniesiony"
                )
                import_stopped = True
                break
            if target.exists():
                state.import_failures.append(
                    f"plik {pf.path.name}: cel archiwizacji {target} już istnieje — plik NIE zaimportowany/przeniesiony/nadpisany"
                )
                import_stopped = True
                break

            # Dopiero teraz (wszystkie asercje przeszły) faktyczny import.
            import_summary = import_parsed_files([pf], conn, commit=commit)
            file_result = import_summary.per_file[0] if import_summary.per_file else None

            raw_archive_dir.mkdir(parents=True, exist_ok=True)
            rows_new = sum(r.new for r in file_result.per_rachunek.values()) if file_result else 0
            rows_dup = sum(r.duplicate for r in file_result.per_rachunek.values()) if file_result else 0
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO processed_history_files
                        (sha256, original_name, stored_path, first_date, last_date, rows_total, rows_new, rows_duplicate)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (sha256) DO NOTHING
                    """,
                    (pf.sha256, pf.path.name, str(target), first_date, last_date, pf.row_count, rows_new, rows_dup),
                )
            if commit:
                conn.commit()

            shutil.move(str(pf.path), str(target))

            # Aktualizacja stanu "znanego" max(transaction_date) per rachunek —
            # kolejny plik tego przebiegu sprawdza ciągłość względem TEGO
            # zaktualizowanego stanu (baza + pliki już zaimportowane w tym
            # przebiegu), patrz `check_file_continuity`.
            if file_result is not None:
                for rachunek, fr in file_result.per_rachunek.items():
                    if fr.max_date is not None:
                        known_max_dates[rachunek] = max(known_max_dates.get(rachunek, fr.max_date), fr.max_date)

            per_rachunek_counts = {
                r: (fr.new, fr.duplicate) for r, fr in (file_result.per_rachunek.items() if file_result else [])
            }
            date_range = (first_date, last_date) if first_date and last_date else None
            state.files_processed.append(
                ProcessedFileReportRow(pf.path.name, _sha8(pf.sha256), "przetworzony", per_rachunek_counts, date_range)
            )

        if import_stopped:
            state.stage_not_executed.update({STAGE_FIFO, STAGE_REGISTRATION, STAGE_PRICES_FX, STAGE_RISK})
            raise _StageStop()

        # --- C3: FIFO -------------------------------------------------------
        with conn.cursor() as cur:
            before_snapshot = _positions_fifo_snapshot(cur)
        run_fifo(conn, as_of=today, commit=commit)
        with conn.cursor() as cur:
            after_snapshot = _positions_fifo_snapshot(cur)
        state.position_changes = diff_positions(before_snapshot, after_snapshot)
        with conn.cursor() as cur:
            state.archetype_missing = _archetype_missing_tickers(cur)

        # --- C4: bramka rejestracji ------------------------------------------
        with conn.cursor() as cur:
            reg_rows = _registration_rows(cur)
        gaps = registration_gaps(reg_rows)
        if gaps:
            state.registration_queue = gaps
            state.stage_not_executed.update({STAGE_PRICES_FX, STAGE_RISK})
            raise _StageStop()

        # --- C5: ceny i FX ----------------------------------------------------
        try:
            fetch_prices(conn)
        except Exception as exc:  # noqa: BLE001 — zamiana na DATA FAILURE (brief C5/C8)
            state.import_failures.append(f"błąd etapu ceny/FX: {type(exc).__name__}: {exc}")
            state.stage_not_executed.add(STAGE_RISK)
            raise _StageStop() from exc

        try:
            fx_summary = fetch_fx(conn, NBP_CURRENCIES)
        except Exception as exc:  # noqa: BLE001
            state.import_failures.append(f"błąd etapu ceny/FX: {type(exc).__name__}: {exc}")
            state.stage_not_executed.add(STAGE_RISK)
            raise _StageStop() from exc

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT ie.source, COALESCE(i.yahoo_symbol, i.broker_ticker) AS symbol,
                       ie.price_date, ie.error_type, ie.detail
                FROM ingest_errors ie
                LEFT JOIN instruments i ON i.id = ie.instrument_id
                WHERE ie.run_started_at >= %s
                ORDER BY ie.id
                """,
                (run_started_at,),
            )
            for source, symbol, price_date, error_type, detail in cur.fetchall():
                state.price_ingest_errors.append(
                    f"{source} {symbol or 'brak'} data={_fmt_date(price_date)} {error_type}: {detail}"
                )

        for r in fx_summary.results:
            if r.error:
                state.fx_failures.append(f"{r.currency}: {r.error}")

        # --- C6: ryzyko ---------------------------------------------------
        d = resolve_date_fn(conn, calendar_facts_fn=calendar_facts_fn)
        if d is None:
            state.d_resolution_failure = "brak kompletnej daty w oknie 30 dat cenowych"
            state.stage_not_executed.add(STAGE_RISK)
            raise _StageStop()
        state.d = d

        with conn.cursor() as cur:
            cur.execute("SELECT max(risk_date) FROM risk_daily WHERE risk_date < %s", (d,))
            prev_date = cur.fetchone()[0]
        state.prev_risk_date = prev_date

        try:
            risk_summary = risk_fn(conn, as_of=d, commit=commit)
        except IncompleteRiskDateError as exc:
            state.date_incomplete_items = list(exc.items)
            state.stage_not_executed.add(STAGE_RISK)
            raise _StageStop() from exc

        state.risk_aggregates = RiskReportAggregates.from_risk_summary(risk_summary)

        # Poprzednia ocena (D-1): stop_effective (M62) I risk_state (STOP §2,
        # M69 EVENT) na `prev_date` — jedno zapytanie, dwa słowniki.
        prev_stops: dict[tuple[str, int], Decimal] = {}
        prev_risk_state: dict[tuple[str, int], str | None] = {}
        if prev_date is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT rachunek, instrument_id, stop_effective, risk_state FROM risk_daily WHERE risk_date = %s",
                    (prev_date,),
                )
                for r, i, stop_eff, risk_state in cur.fetchall():
                    if stop_eff is not None:
                        prev_stops[(r, i)] = stop_eff
                    prev_risk_state[(r, i)] = risk_state

        # symbol Yahoo instrumentu CENOWEGO (dla kontraktu: baza) — jeden
        # lookup per pozycja, dzielony przez STOP (§2) i M62.
        with conn.cursor() as cur:
            price_source_symbols = {
                row.instrument_id: _price_source_symbol(cur, row.instrument_id, row.instrument_type)
                for row in risk_summary.rows
            }

        # STOP (§2, poprawka po przeglądzie): ZDARZENIE przecięcia stopu od
        # poprzedniej oceny (M69 EVENT; CC-U V3), nie lista wszystkich pozycji
        # pod stopem — filtr satelity i logika "nowe przecięcie" są w
        # `stop_events` (czysta funkcja, testowana bez bazy).
        stop_event_rows = [
            StopEventRow(
                broker_ticker=row.broker_ticker,
                rachunek=row.rachunek,
                instrument_id=row.instrument_id,
                instrument_type=row.instrument_type,
                is_core=row.is_core,
                below_stop=row.below_stop,
                price_date_used=row.price_date_used,
                close_d=row.close_d,
                stop_effective=row.stop_effective,
                stop_source=row.stop_source,
                price_source_symbol=price_source_symbols.get(row.instrument_id),
                currency=row.quote_currency or row.settlement_currency,
            )
            for row in risk_summary.rows
        ]
        state.stop_events = stop_events(stop_event_rows, prev_risk_state)

        # M62 (§3): WYŁĄCZNIE otwarte pozycje satelity (brief: "każda otwarta
        # pozycja satelity z risk_daily na D"). "poziom na bazie" (kontrakty)
        # = symbol bazy — dla equity/etf None (stop już jest na własnej cenie).
        # `row.atr22`/`row.below_stop` przekazane wprost (poprawka po
        # przeglądzie nadzorcy, P1/P3) — dla kontraktu ATR jest już liczone na
        # instrumencie BAZOWYM przez `risk.run_risk` (patrz docstring modułu
        # risk.py, P4.2), więc `row.atr22` jest już właściwą wartością.
        stop_order_items: list[
            tuple[str, str, str, Decimal | None, str, Decimal | None, str | None, Decimal | None, bool | None]
        ] = []
        for row in risk_summary.rows:
            if not is_risk_budget_eligible(row.instrument_type, row.is_core):
                continue
            stop_prev = prev_stops.get((row.rachunek, row.instrument_id))
            base_symbol_label = price_source_symbols.get(row.instrument_id) if row.instrument_type == "future" else None
            stop_order_items.append(
                (
                    row.broker_ticker,
                    row.rachunek,
                    row.position_kind,
                    row.stop_effective,
                    row.quote_currency or row.settlement_currency,
                    stop_prev,
                    base_symbol_label,
                    row.atr22,
                    row.below_stop,
                )
            )
        state.stop_order_rows = build_stop_order_rows(stop_order_items)

        # HEARTBEAT — pokrycie n/N (price_date_used == D, stale wyłączone)
        satellite_rows = [r for r in risk_summary.rows if is_risk_budget_eligible(r.instrument_type, r.is_core)]
        state.heartbeat_total = len(satellite_rows)
        state.heartbeat_covered = sum(
            1 for r in satellite_rows if (not r.price_is_stale) and r.price_date_used == d
        )

        # T28 — świeżość rynków otwartych pozycji satelity
        with conn.cursor() as cur:
            price_instruments = _satellite_price_instruments(cur)
            price_dates_by_cal = _price_dates_by_calendar(cur, price_instruments)
        state.freshness_results = market_freshness(price_dates_by_cal, sessions_fn, today)

    except _StageStop:
        pass
    except Exception as exc:  # noqa: BLE001 — brief C8: wyjątek etapu -> DATA FAILURE, raport zawsze powstaje
        # Poprawka po przeglądzie: wyjątek nieoczekiwany zostawia transakcję
        # Postgresa w stanie "aborted" (jeśli błąd pochodził z zapytania SQL) —
        # rollback PRZED dalszym użyciem `conn` (np. przez wywołującego po
        # powrocie z `run_cycle`), żeby nic po drodze nie padło na przerwanej
        # transakcji. Tylko gdy `commit=True` (produkcja) — w trybie testowym
        # (`commit=False`) transakcję kończy WYŁĄCZNIE `finally: conn.rollback()`
        # wywołującego testu (jedna transakcja na cały test, brief C9).
        if commit:
            conn.rollback()
        state.import_failures.append(f"nieoczekiwany wyjątek: {type(exc).__name__}: {exc}")

    state.runtime_seconds = time.monotonic() - t_start
    state.exit_code = 2 if has_data_failures(state) else 0

    base_name = report_base_name(state.d, run_started_at)
    probe_path = reports_dir / f"{base_name}.md"

    # Q3 (poprawka po przeglądzie, po STOP przebiegu #2): asercja check-ignore
    # PRZED zapisem raportu — False -> NIC nie zapisujemy, NIE drukujemy
    # treści raportu (zawiera emoji — na konsoli w kodowaniu bez ich pokrycia,
    # np. cp1250, to była przyczyna EXIT=1 z UnicodeEncodeError w poprzednim
    # przebiegu), tylko czytelny komunikat ASCII i kod != 0.
    if not check_ignored(probe_path):
        state.exit_code = 2
        message = f"ERROR: report path not git-ignored: {probe_path}; report not written"
        return CycleResult(exit_code=state.exit_code, report_path=None, state=state, error_message=message)

    # Q2: kod wyjścia w treści raportu MUSI odzwierciedlać wynik ostateczny —
    # `state.exit_code` jest już ustalone (wyżej), więc kolejność (najpierw
    # render, potem zapis, na końcu druk) jest tu bezpieczna.
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_text = render_report(state)
    report_path = write_report_unique(reports_dir, base_name, report_text)
    return CycleResult(exit_code=state.exit_code, report_path=report_path, state=state)


class _StageStop(Exception):
    """Sygnał wewnętrzny: etap zakończył cykl (DATA FAILURE) — raport i tak
    powstaje po wyjściu z bloku `try` w `run_cycle`."""

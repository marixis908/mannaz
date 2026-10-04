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

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

import psycopg
import yfinance as yf

DEFAULT_START = date(2023, 10, 1)
LOG_RETURN_THRESHOLD = Decimal(4)

CurrencyCheck = Literal["match", "gbp_pence_conversion_needed", "mismatch", "no_data"]
# I3 (brief CC-I): status agregatu wierszy jednego instrumentu w jednym przebiegu.
SymbolPricesStatus = Literal["ok", "rows_rejected", "empty_response"]
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
    hist = ticker.history(
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        auto_adjust=False,
        actions=True,
    )
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


# B-05 (Y3): kolumny OHLC split_adj wchodzące do porównania rewizji dostawcy
# (kolejność = kolejność w `detail`). volume_split_adj i total return NIE wchodzą.
_REVISION_COLUMNS = ("open_split_adj", "high_split_adj", "low_split_adj", "close_split_adj")


def _format_revision_detail(stored: dict[str, Decimal | None], row: "OhlcRow") -> str | None:
    """B-05 (Y3): rewizja dostawcy = zmiana ZAPISANEJ, kompletnej (nie NULL)
    wartości którejkolwiek z kolumn OHLC split_adj przy ponownym pobraniu.
    Porównanie dokładne (Decimal `!=`, bez kwantyzacji): kolumny NUMERIC w
    `prices_daily` nie mają skali (sql/001_schema.sql), więc wartość z bazy
    jest dokładnie tym Decimalem, który został zapisany; `Decimal('10.50') ==
    Decimal('10.5')`, więc ponowny zapis identycznych wartości daje 0 rewizji.
    Zwraca `detail` w stałym formacie `col=<kolumna> old=<x> new=<y>; ...`
    (tylko kolumny zmienione) albo None, gdy rewizji nie ma."""
    parts: list[str] = []
    for col in _REVISION_COLUMNS:
        old = stored.get(col)
        new = getattr(row, col)
        if old is None or new is None:
            continue
        if old != new:
            parts.append(f"col={col} old={old} new={new}")
    return "; ".join(parts) if parts else None


def _mapped_instruments(cur: psycopg.Cursor, instrument_ids: list[int] | None) -> list[dict[str, Any]]:
    if instrument_ids:
        cur.execute(
            """
            SELECT id, yahoo_symbol, currency FROM instruments
            WHERE id = ANY(%s) AND yahoo_symbol IS NOT NULL
            ORDER BY id
            """,
            (instrument_ids,),
        )
    else:
        cur.execute(
            """
            SELECT i.id, i.yahoo_symbol, i.currency FROM instruments i
            WHERE i.yahoo_symbol IS NOT NULL
              AND (i.is_core OR EXISTS (
                  SELECT 1 FROM positions_fifo p WHERE p.instrument_id = i.id
              ) OR i.yahoo_symbol IN (
                  -- underlyings of open futures (P4.2: stop/ATR on the base)
                  SELECT f.base_symbol FROM instruments f
                  JOIN positions_fifo p ON p.instrument_id = f.id
                  WHERE f.instrument_type = 'future'
              ))
            ORDER BY i.id
            """
        )
    cols = ("id", "yahoo_symbol", "currency")
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

    `commit=False` (jak `run_risk`, brief CC-S) — do testów DB z rollbackiem."""
    if end is None:
        end = date.today()

    run_started_at = datetime.now(timezone.utc)
    summary = PricesSummary()

    with conn.cursor() as cur:
        _ensure_ingest_errors_table(cur)  # I5 — przed jakimkolwiek zapisem cen

        instruments = _mapped_instruments(cur, instrument_ids)

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

            rows = fetch_ohlc(symbol, start, end)
            result.rows_fetched = len(rows)

            closes_for_t8 = [(r.price_date, r.close_split_adj) for r in rows]
            result.log_return_outliers = detect_log_return_outliers(closes_for_t8)

            # I2/I3 — bezpiecznik wiersza/odpowiedzi (brief CC-I, B-21).
            valid_rows, rejected_rows = split_valid_and_rejected(rows)
            result.rows_rejected = len(rejected_rows)

            if not rows:
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
            elif not valid_rows:
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
                    SELECT price_date, open_split_adj, high_split_adj, low_split_adj, close_split_adj
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
                for pd_, o_, h_, l_, c_ in cur.fetchall():
                    stored_by_date[pd_] = {
                        "open_split_adj": o_,
                        "high_split_adj": h_,
                        "low_split_adj": l_,
                        "close_split_adj": c_,
                    }

            inserted = 0
            for row in valid_rows:
                stored = stored_by_date.get(row.price_date)
                if stored is not None:
                    revision_detail = _format_revision_detail(stored, row)
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

            # source_runs.sha256_input jest zdefiniowany dla plików (P2.4); dla API
            # bez pliku wejściowego używamy sha256 deterministycznego opisu zapytania
            # (symbol + zakres dat) jako klucza deduplikacji tego samego uruchomienia.
            run_key = hashlib.sha256(
                f"prices_daily:{symbol}:{start.isoformat()}:{end.isoformat()}".encode("utf-8")
            ).hexdigest()
            cur.execute(
                """
                INSERT INTO source_runs (source, row_count, sha256_input)
                VALUES (%s, %s, %s)
                ON CONFLICT (source, sha256_input) DO NOTHING
                """,
                (f"yahoo:{symbol}", inserted, run_key),
            )

            summary.results.append(result)

        if commit:
            conn.commit()

    return summary

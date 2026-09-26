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
utworzyłaby drugi próg zwrotu)."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal

import psycopg
import yfinance as yf

DEFAULT_START = date(2023, 10, 1)
LOG_RETURN_THRESHOLD = Decimal(4)

CurrencyCheck = Literal["match", "gbp_pence_conversion_needed", "mismatch", "no_data"]


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
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
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
    currency_check: CurrencyCheck = "no_data"
    log_return_outliers: list[tuple[date, Decimal]] = field(default_factory=list)
    adjustment_convention: str = "yahoo_split_adjusted"
    note: str = ""


@dataclass
class PricesSummary:
    results: list[SymbolPricesResult] = field(default_factory=list)
    t7_pass: int = 0
    t7_total: int = 0


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


def run_prices_fetch(
    conn: psycopg.Connection,
    instrument_ids: list[int] | None = None,
    start: date = DEFAULT_START,
    end: date | None = None,
) -> PricesSummary:
    """Pobiera i zapisuje `prices_daily` dla instrumentów z wypełnionym
    `yahoo_symbol` (P3.1). `instrument_ids=None` -> WSZYSTKIE zmapowane
    instrumenty (pełne pobranie — brief zastrzega, że wykonuje je inny agent);
    lista id -> próba na wybranym podzbiorze (brief P3.2: 2 symbole)."""
    if end is None:
        end = date.today()

    summary = PricesSummary()

    with conn.cursor() as cur:
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

            inserted = 0
            for row in rows:
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

        conn.commit()

    return summary

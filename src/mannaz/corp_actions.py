"""Detektor zdarzeń korporacyjnych (splitów/scaleń) z danych cenowych.
Brief CC-P, poprawka P2.3/P2.5.

Kontekst (ustalenia sesji głównej 2026-09-26): historia gotówki NIE zawiera
splitów (nie przesuwają gotówki), więc wymóg "parser sam wykrywa split z
tytułów transakcji" jest niespełnialny na tym źródle. Ten moduł dokłada dwa
NIEZALEŻNE mechanizmy wykrywania, oba działające na dowolnym instrumencie
posiadanym (bez hardkodowania konkretnych tickerów):

  1. `import_yfinance_splits()` — bezpośredni import znanych splitów z
     `yfinance.Ticker(symbol).splits` (API ma rekord ze znaną datą/ratio;
     np. APH 2:1 z 2026-09-03).
  2. `run_price_ratio_detector()` — dla instrumentów, których yfinance NIE
     rejestruje jako splitu (np. SPYI — Yahoo retroaktywnie skorygował cenę
     Xetra bez śladu w `splits`), licz medianę ilorazu
     cena_brokera / close_yahoo(auto_adjust=False) z dnia transakcji (albo
     najbliższej wcześniejszej sesji) per instrument. Gdy mediana odbiega od 1
     o >30% I jest bliska liczbie całkowitej N (lub 1/N) w tolerancji 5% ->
     zdarzenie split/reverse_split z ratio = zaokrąglony iloraz.

Obie ścieżki piszą do `corporate_events` z kolumną `source`
('yfinance_splits' | 'price_ratio_detector'); przed zapisem #2 sprawdzamy, czy
to samo zdarzenie nie zostało już zarejestrowane przez #1 dla tego samego
instrumentu i tej samej daty — żeby nie liczyć jednego realnego splitu dwa
razy pod dwoma źródłami.

Mapowanie broker_ticker -> symbol Yahoo jest CELOWO minimalne (pełne
mapowanie: P3.1) — tylko trzy jednoznaczne wzorce (GPW `.WA`, US bez sufiksu,
core ETF Xetra `.DE`); wszystko inne jest pomijane i zliczane jako
"pominięte bez symbolu", zamiast zgadywane.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from statistics import median
from typing import Any

import psycopg
import yfinance as yf

# ---------------------------------------------------------------------------
# Progi detekcji (brief CC-P, poprawka P2.3/P2.5)
# ---------------------------------------------------------------------------

RATIO_DEVIATION_THRESHOLD = Decimal("0.30")  # mediana musi odbiegać od 1 o więcej niż 30%
INTEGER_CLOSENESS_TOLERANCE = Decimal("0.05")  # i być bliska N / 1/N w tolerancji 5%
YFINANCE_SPLITS_IMPORT_START = date(2023, 10, 1)
RATIO_MATCH_TOLERANCE = Decimal("0.05")  # dopasowanie wykrytego ratio do konkretnego splitu z yfinance

# Yahoo bywa notowany w groszach (GBp) zamiast funtach (GBP) dla LSE — bez
# tego rozróżnienia detektor pomyliłby jednostkę z 100:1 splitem. Nie dotyczy
# obecnie posiadanych instrumentów (brak GBP/GBp w danych), ale reguła musi
# istnieć ogólnie (brief). Jeśli symbol kończy się na '.L' i waluta transakcji
# to GBP, close z Yahoo (zwykle w GBp) dzielimy przez 100.
_LSE_PENCE_SUFFIX = ".L"


def _pence_adjustment_factor(symbol: str, txn_currency: str) -> Decimal:
    if symbol.endswith(_LSE_PENCE_SUFFIX) and txn_currency == "GBP":
        return Decimal("0.01")
    return Decimal(1)


# ---------------------------------------------------------------------------
# Mapowanie minimalne broker_ticker -> symbol Yahoo (pełne mapowanie: P3.1)
# ---------------------------------------------------------------------------


def yahoo_symbol_for(*, broker_ticker: str, currency: str, rachunek: str, is_core: bool) -> str | None:
    """Zwraca symbol Yahoo albo None, gdy nie da się jednoznacznie zgadnąć
    (np. instrumenty EUR spoza rdzenia mogą być ADR-em na Xetra ALBO spółką
    natywnie europejską — nierozstrzygalne bez pełnego mapowania z P3.1)."""
    ticker = (broker_ticker or "").strip().upper()
    if not re.match(r"^[A-Z0-9]+$", ticker):
        return None

    rachunek_u = (rachunek or "").upper()

    if currency == "PLN" and "AKCYJNY" in rachunek_u:
        return f"{ticker}.WA"

    if currency == "USD" and "ZAGRANICZNY" in rachunek_u:
        return ticker

    if currency == "EUR" and is_core:
        # Core ETF-y (SPYI/V80A/V60A) notowane w EUR na Xetra — zweryfikowane
        # empirycznie dla SPYI 2026-09-26 (close ~6.8 EUR vs zapis brokera
        # ~170 EUR w 2023-10 => detektor #2 poniżej znajduje ratio ~25 sam).
        return f"{ticker}.DE"

    return None


# ---------------------------------------------------------------------------
# Pobieranie danych z yfinance
# ---------------------------------------------------------------------------


def fetch_close_series(symbol: str, start: date, end: date) -> dict[date, Decimal]:
    """Historia close (auto_adjust=False) dla symbolu, w oknie [start, end+bufor]."""
    ticker = yf.Ticker(symbol)
    hist = ticker.history(
        start=start.isoformat(),
        end=(end + timedelta(days=7)).isoformat(),
        auto_adjust=False,
    )
    out: dict[date, Decimal] = {}
    if hist is None or hist.empty:
        return out
    for idx, row in hist.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        close = row.get("Close")
        if close is not None:
            out[d] = Decimal(str(close))
    return out


def fetch_yfinance_splits(symbol: str) -> list[tuple[date, Decimal]]:
    """Pełna historia splitów danego symbolu z yfinance (Ticker.splits),
    jako lista (data, ratio) posortowana rosnąco po dacie."""
    ticker = yf.Ticker(symbol)
    splits = ticker.splits
    out: list[tuple[date, Decimal]] = []
    if splits is None or len(splits) == 0:
        return out
    for idx, value in splits.items():
        d = idx.date() if hasattr(idx, "date") else idx
        out.append((d, Decimal(str(value))))
    out.sort(key=lambda t: t[0])
    return out


def closest_prior_close(close_by_date: dict[date, Decimal], target: date) -> Decimal | None:
    candidates = [d for d in close_by_date if d <= target]
    if not candidates:
        return None
    return close_by_date[max(candidates)]


# ---------------------------------------------------------------------------
# Klasyfikacja mediany ilorazu
# ---------------------------------------------------------------------------


def classify_ratio(value: Decimal) -> tuple[str, Decimal] | None:
    """value = mediana(cena_brokera / close_yahoo). Zwraca (event_type,
    ratio_final) jeśli mediana odbiega od 1 o >30% ORAZ jest bliska liczbie
    całkowitej N>=2 (split) lub 1/N (reverse_split) w tolerancji 5%. Inaczej
    None (brak wystarczających dowodów na split)."""
    if value is None or value <= 0:
        return None

    deviation = abs(value - 1)
    if deviation <= RATIO_DEVIATION_THRESHOLD:
        return None

    n_up = int(value.to_integral_value(rounding=ROUND_HALF_UP))
    if n_up >= 2:
        tol = abs(value - n_up) / n_up
        if tol <= INTEGER_CLOSENESS_TOLERANCE:
            return ("split", Decimal(n_up))

    inv = Decimal(1) / value
    n_down = int(inv.to_integral_value(rounding=ROUND_HALF_UP))
    if n_down >= 2:
        tol = abs(inv - n_down) / n_down
        if tol <= INTEGER_CLOSENESS_TOLERANCE:
            return ("reverse_split", Decimal(1) / Decimal(n_down))

    return None


def _ratios_match(a: Decimal, b: Decimal) -> bool:
    if b == 0:
        return False
    return abs(a - b) / abs(b) <= RATIO_MATCH_TOLERANCE or abs((Decimal(1) / a) - (Decimal(1) / b)) / abs(
        Decimal(1) / b
    ) <= RATIO_MATCH_TOLERANCE


@dataclass
class RatioSample:
    transaction_date: date
    ratio: Decimal


def resolve_event_date(
    samples: list[RatioSample],
    ratio_final: Decimal,
    yf_splits: list[tuple[date, Decimal]],
) -> tuple[date, str]:
    """Data zdarzenia + jej źródło (date_source), gdy sam detektor cenowy nie
    zna dat z tytułów:
      1. dopasowanie do pełnej historii splitów yfinance (jeśli istnieje) ->
         'yfinance_splits'. Instrument mógł split-ować wielokrotnie z tym
         samym ratio (np. APH: 2:1 sześć razy w historii) — dopasowujemy
         WYŁĄCZNIE splity datowane PO ostatniej naszej transakcji, bo tylko
         taki split mógł spowodować obserwowaną rozbieżność (split sprzed
         transakcji byłby już "wliczony" identycznie po obu stronach ilorazu,
         zero rozbieżności). Spośród kandydatów bierzemy najwcześniejszy.
      2. inaczej: pierwsza transakcja, w której iloraz wraca do ~1 (granica
         przed/po realnym zdarzeniu) -> 'inferred_boundary',
      3. inaczej (iloraz != 1 we WSZYSTKICH próbkach): zastosuj do wszystkich
         transakcji -> data tuż po ostatniej transakcji, 'inferred_all'."""
    ordered = sorted(samples, key=lambda s: s.transaction_date)
    max_sample_date = ordered[-1].transaction_date

    candidates = sorted(
        (d, r) for d, r in yf_splits if d > max_sample_date and _ratios_match(r, ratio_final)
    )
    if candidates:
        return candidates[0][0], "yfinance_splits"
    for s in ordered:
        if abs(s.ratio - 1) <= RATIO_DEVIATION_THRESHOLD:
            return s.transaction_date, "inferred_boundary"

    last_date = max(s.transaction_date for s in ordered)
    return last_date + timedelta(days=1), "inferred_all"


# ---------------------------------------------------------------------------
# Orkiestracja / DB
# ---------------------------------------------------------------------------


@dataclass
class DetectedEvent:
    broker_ticker: str
    instrument_id: int
    event_type: str
    ratio: Decimal
    event_date: date
    date_source: str
    source: str
    n_samples: int
    median_ratio: Decimal


@dataclass
class CorpActionsSummary:
    yfinance_splits_imported: list[DetectedEvent] = field(default_factory=list)
    price_ratio_detected: list[DetectedEvent] = field(default_factory=list)
    price_ratio_duplicates_skipped: int = 0
    instruments_checked: int = 0  # symbol Yahoo rozwiązany
    instruments_skipped_no_symbol: int = 0
    instruments_no_data: int = 0  # symbol rozwiązany, ale brak danych cenowych/próbek
    # Kontrolka ujemna/dodatnia detektora #2 (price_ratio_detector), liczona na
    # WSZYSTKICH instrumentach z policzalną medianą (niezależnie od tego, czy
    # trafienie zostało finalnie zapisane, czy zdedupowane względem źródła 1):
    ratio_detector_evaluated: int = 0
    ratio_detector_hit_tickers: list[str] = field(default_factory=list)
    ratio_detector_clean_tickers: list[str] = field(default_factory=list)


def _owned_instruments(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    """Instrumenty equity/etf z co najmniej jedną transakcją kupna/sprzedaży,
    z dominującym (rachunek, currency) użytym do rozwiązania symbolu Yahoo."""
    cur.execute(
        """
        SELECT i.id, i.broker_ticker, i.is_core,
               (SELECT t.rachunek FROM transactions t
                WHERE t.instrument_id = i.id AND t.row_type IN ('kupno', 'sprzedaz')
                GROUP BY t.rachunek ORDER BY count(*) DESC LIMIT 1) AS rachunek,
               (SELECT t.currency FROM transactions t
                WHERE t.instrument_id = i.id AND t.row_type IN ('kupno', 'sprzedaz')
                GROUP BY t.currency ORDER BY count(*) DESC LIMIT 1) AS currency
        FROM instruments i
        WHERE i.instrument_type IN ('equity', 'etf')
          AND EXISTS (
              SELECT 1 FROM transactions t
              WHERE t.instrument_id = i.id AND t.row_type IN ('kupno', 'sprzedaz')
          )
        ORDER BY i.id
        """
    )
    cols = ("id", "broker_ticker", "is_core", "rachunek", "currency")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _instrument_transactions(cur: psycopg.Cursor, instrument_id: int, currency: str) -> list[tuple[date, Decimal]]:
    cur.execute(
        """
        SELECT transaction_date, price FROM transactions
        WHERE instrument_id = %s AND currency = %s
          AND row_type IN ('kupno', 'sprzedaz') AND price IS NOT NULL
        ORDER BY transaction_date
        """,
        (instrument_id, currency),
    )
    return [(d, p) for d, p in cur.fetchall()]


def _insert_event(
    cur: psycopg.Cursor,
    *,
    instrument_id: int,
    broker_ticker: str,
    event_date: date,
    event_type: str,
    ratio: Decimal,
    source: str,
    date_source: str | None,
    title_raw: str,
) -> bool:
    cur.execute(
        """
        INSERT INTO corporate_events (
            instrument_id, broker_ticker, event_date, event_type,
            ratio, title_raw, source, date_source
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (broker_ticker, event_date, event_type, title_raw) DO NOTHING
        RETURNING id
        """,
        (instrument_id, broker_ticker, event_date, event_type, ratio, title_raw, source, date_source),
    )
    return cur.fetchone() is not None


def _existing_yfinance_event_dates(cur: psycopg.Cursor, instrument_id: int) -> set[date]:
    cur.execute(
        """
        SELECT event_date FROM corporate_events
        WHERE instrument_id = %s AND source = 'yfinance_splits'
        """,
        (instrument_id,),
    )
    return {row[0] for row in cur.fetchall()}


def run_corp_actions_detector(conn: psycopg.Connection) -> CorpActionsSummary:
    summary = CorpActionsSummary()

    with conn.cursor() as cur:
        instruments = _owned_instruments(cur)

        for inst in instruments:
            broker_ticker = inst["broker_ticker"]
            rachunek = inst["rachunek"]
            currency = inst["currency"]
            if rachunek is None or currency is None:
                continue

            symbol = yahoo_symbol_for(
                broker_ticker=broker_ticker,
                currency=currency,
                rachunek=rachunek,
                is_core=bool(inst["is_core"]),
            )
            if symbol is None:
                summary.instruments_skipped_no_symbol += 1
                continue

            summary.instruments_checked += 1

            txns = _instrument_transactions(cur, inst["id"], currency)
            if not txns:
                summary.instruments_no_data += 1
                continue

            min_date = min(d for d, _ in txns)
            max_date = max(d for d, _ in txns)

            # --- Źródło 1: import bezpośredni z yfinance Ticker.splits ---
            yf_splits_full = fetch_yfinance_splits(symbol)
            yf_splits_recent = [
                (d, r) for d, r in yf_splits_full if d >= YFINANCE_SPLITS_IMPORT_START
            ]
            for d, ratio in yf_splits_recent:
                event_type = "split" if ratio >= 1 else "reverse_split"
                title_raw = (
                    f"[auto] yfinance_splits: {broker_ticker} ({symbol}) "
                    f"{event_type} ratio={ratio} date={d.isoformat()}"
                )
                inserted = _insert_event(
                    cur,
                    instrument_id=inst["id"],
                    broker_ticker=broker_ticker,
                    event_date=d,
                    event_type=event_type,
                    ratio=ratio,
                    source="yfinance_splits",
                    date_source="yfinance_splits",
                    title_raw=title_raw,
                )
                if inserted:
                    summary.yfinance_splits_imported.append(
                        DetectedEvent(
                            broker_ticker=broker_ticker,
                            instrument_id=inst["id"],
                            event_type=event_type,
                            ratio=ratio,
                            event_date=d,
                            date_source="yfinance_splits",
                            source="yfinance_splits",
                            n_samples=0,
                            median_ratio=ratio,
                        )
                    )

            # --- Źródło 2: detektor z ilorazu ceny (kontrolka dodatnia/ujemna) ---
            close_series = fetch_close_series(symbol, min_date, max_date)
            if not close_series:
                summary.instruments_no_data += 1
                continue

            factor = _pence_adjustment_factor(symbol, currency)
            samples: list[RatioSample] = []
            for tdate, price in txns:
                close = closest_prior_close(close_series, tdate)
                if close is None or close == 0:
                    continue
                close_adj = close * factor
                if close_adj == 0:
                    continue
                samples.append(RatioSample(transaction_date=tdate, ratio=price / close_adj))

            if not samples:
                summary.instruments_no_data += 1
                continue

            med_ratio = median(s.ratio for s in samples)
            decision = classify_ratio(med_ratio)
            summary.ratio_detector_evaluated += 1

            if decision is None:
                summary.ratio_detector_clean_tickers.append(broker_ticker)
                continue

            summary.ratio_detector_hit_tickers.append(broker_ticker)
            event_type, ratio_final = decision
            event_date, date_source = resolve_event_date(samples, ratio_final, yf_splits_full)

            if date_source == "yfinance_splits" and event_date in _existing_yfinance_event_dates(cur, inst["id"]):
                # to samo zdarzenie już zapisane pod source='yfinance_splits' — nie duplikuj
                summary.price_ratio_duplicates_skipped += 1
                continue

            title_raw = (
                f"[auto] price_ratio_detector: {broker_ticker} ({symbol}) {event_type} "
                f"median_ratio={med_ratio:.4f} n={len(samples)} date_source={date_source}"
            )
            inserted = _insert_event(
                cur,
                instrument_id=inst["id"],
                broker_ticker=broker_ticker,
                event_date=event_date,
                event_type=event_type,
                ratio=ratio_final,
                source="price_ratio_detector",
                date_source=date_source,
                title_raw=title_raw,
            )
            if inserted:
                summary.price_ratio_detected.append(
                    DetectedEvent(
                        broker_ticker=broker_ticker,
                        instrument_id=inst["id"],
                        event_type=event_type,
                        ratio=ratio_final,
                        event_date=event_date,
                        date_source=date_source,
                        source="price_ratio_detector",
                        n_samples=len(samples),
                        median_ratio=med_ratio,
                    )
                )

        conn.commit()

    return summary

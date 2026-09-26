"""Import plików financeHistory*.csv do Postgresa (brief CC-P, P2.4).

Dwuprzebiegowy import:
  1) rejestracja instrumentów ze wszystkich wierszy kupna/sprzedaży ze WSZYSTKICH
     plików (mają ISIN — to najbardziej wiarygodne źródło tożsamości instrumentu),
  2) insert transakcji + dowiązanie instrumentu również dla wierszy bez ISIN
     (dywidendy, depozyty, wygaśnięcia, zdarzenia korporacyjne) — po samym
     tickerze, z rejestru zbudowanego w przebiegu 1. Zapobiega to sytuacji, w
     której dywidenda przetworzona przed odpowiadającym jej kupnem tworzy
     osobny, zduplikowany rekord instrumentu.

Dedup: UNIQUE w transactions łapie identyczne krotki MIĘDZY plikami (ON CONFLICT
DO NOTHING). W OBRĘBIE jednego pliku nic nie jest deduplikowane — occurrence_no
odróżnia identyczne wiersze, bo mogą to być prawdziwe osobne transakcje (brief
P2.2). Liczba takich przypadków jest raportowana osobno, nie rozstrzygana.
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from mannaz.parse_history import (
    CORE_TICKERS,
    ParsedFile,
    classify_instrument_type,
    parse_source_file,
)


@dataclass
class ImportSummary:
    rows_in: int = 0
    rows_unique: int = 0
    rejected_cross_file_duplicates: int = 0
    in_file_duplicate_occurrences: int = 0
    per_rachunek: dict[str, dict[str, int]] = field(default_factory=dict)
    unknown_title_patterns: dict[str, int] = field(default_factory=dict)
    unknown_title_count: int = 0
    corporate_events_detected: int = 0


class InstrumentRegistry:
    def __init__(self, cur: psycopg.Cursor):
        self.cur = cur
        self.by_isin: dict[str, int] = {}
        self.by_ticker: dict[str, int] = {}
        self._preload()

    def _preload(self) -> None:
        self.cur.execute("SELECT id, isin, broker_ticker FROM instruments")
        rows = self.cur.fetchall()
        for id_, isin, ticker in rows:
            if ticker:
                self.by_ticker.setdefault(ticker, id_)
        for id_, isin, ticker in rows:
            if isin:
                self.by_isin[isin] = id_
                if ticker:
                    self.by_ticker[ticker] = id_  # wersja z ISIN ma pierwszeństwo

    def get_or_create(
        self,
        *,
        ticker: str,
        name: str | None,
        isin: str | None,
        currency: str,
        instrument_type: str,
        is_core: bool,
    ) -> int:
        if isin and isin in self.by_isin:
            return self.by_isin[isin]
        if not isin and ticker in self.by_ticker:
            return self.by_ticker[ticker]

        if isin:
            self.cur.execute(
                """
                INSERT INTO instruments (broker_ticker, name, isin, currency, instrument_type, is_core)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (isin) WHERE isin IS NOT NULL DO NOTHING
                RETURNING id
                """,
                (ticker, name, isin, currency, instrument_type, is_core),
            )
            row = self.cur.fetchone()
            if row is None:
                self.cur.execute("SELECT id FROM instruments WHERE isin = %s", (isin,))
                row = self.cur.fetchone()
            instrument_id = row[0]
            self.by_isin[isin] = instrument_id
            self.by_ticker[ticker] = instrument_id
            return instrument_id

        self.cur.execute(
            """
            INSERT INTO instruments (broker_ticker, name, isin, currency, instrument_type, is_core)
            VALUES (%s, %s, NULL, %s, %s, %s)
            ON CONFLICT (broker_ticker) WHERE isin IS NULL DO NOTHING
            RETURNING id
            """,
            (ticker, name, currency, instrument_type, is_core),
        )
        row = self.cur.fetchone()
        if row is None:
            self.cur.execute(
                "SELECT id FROM instruments WHERE broker_ticker = %s AND isin IS NULL", (ticker,)
            )
            row = self.cur.fetchone()
        instrument_id = row[0]
        self.by_ticker[ticker] = instrument_id
        return instrument_id

    def resolve_ticker_only(self, ticker: str, currency: str) -> int:
        if ticker in self.by_ticker:
            return self.by_ticker[ticker]
        instrument_type = classify_instrument_type(None, ticker, None)
        return self.get_or_create(
            ticker=ticker,
            name=None,
            isin=None,
            currency=currency,
            instrument_type=instrument_type,
            is_core=ticker in CORE_TICKERS,
        )


def find_source_files(data_dir: Path) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(data_dir / "financeHistory*.csv")))


def _normalize_title_pattern(title: str) -> str:
    """Anonimizuje tytuł do wzorca: cyfry -> '#'. Używane WYŁĄCZNIE do
    raportowania (liczba wystąpień wzorca), nigdy do zapisu w src/tests."""
    return re.sub(r"\d+", "#", title)


def run_import(data_dir: Path, conn: psycopg.Connection) -> ImportSummary:
    files = find_source_files(data_dir)
    parsed_files: list[ParsedFile] = [parse_source_file(f) for f in files]

    summary = ImportSummary()

    with conn.cursor() as cur:
        registry = InstrumentRegistry(cur)

        # Przebieg 1: instrumenty z transakcji kupna/sprzedaży (mają ISIN)
        for pf in parsed_files:
            for row in pf.rows:
                p = row.parsed
                if p.row_type in ("kupno", "sprzedaz"):
                    registry.get_or_create(
                        ticker=p.ticker,
                        name=p.name,
                        isin=p.isin,
                        currency=row.currency,
                        instrument_type=classify_instrument_type(p.name, p.ticker, p.isin),
                        is_core=p.ticker in CORE_TICKERS,
                    )

        # Przebieg 2: insert transakcji + zdarzenia korporacyjne + source_runs
        for pf in parsed_files:
            summary.rows_in += pf.row_count
            summary.in_file_duplicate_occurrences += sum(
                1 for r in pf.rows if r.occurrence_no > 1
            )

            for row in pf.rows:
                p = row.parsed
                instrument_id = None
                if p.ticker:
                    if p.row_type in ("kupno", "sprzedaz"):
                        instrument_id = registry.get_or_create(
                            ticker=p.ticker,
                            name=p.name,
                            isin=p.isin,
                            currency=row.currency,
                            instrument_type=classify_instrument_type(p.name, p.ticker, p.isin),
                            is_core=p.ticker in CORE_TICKERS,
                        )
                    else:
                        instrument_id = registry.resolve_ticker_only(p.ticker, row.currency)

                cur.execute(
                    """
                    INSERT INTO transactions (
                        transaction_date, rachunek, currency, title_raw, amount,
                        occurrence_no, row_type, instrument_id, qty, price,
                        broker_order_no, source_file, source_sha256
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (transaction_date, rachunek, currency, title_raw, amount, occurrence_no)
                    DO NOTHING
                    RETURNING id
                    """,
                    (
                        row.transaction_date,
                        row.rachunek,
                        row.currency,
                        row.title_raw,
                        row.amount,
                        row.occurrence_no,
                        p.row_type,
                        instrument_id,
                        p.qty,
                        p.price,
                        p.order_no,
                        row.source_file,
                        row.source_sha256,
                    ),
                )
                inserted = cur.fetchone()
                if inserted is not None:
                    summary.rows_unique += 1
                    transaction_id = inserted[0]
                else:
                    summary.rejected_cross_file_duplicates += 1
                    transaction_id = None

                if p.row_type == "unknown":
                    pattern = _normalize_title_pattern(row.title_raw)
                    summary.unknown_title_patterns[pattern] = (
                        summary.unknown_title_patterns.get(pattern, 0) + 1
                    )
                    summary.unknown_title_count += 1

                if p.corporate_event is not None:
                    ce = p.corporate_event
                    cur.execute(
                        """
                        INSERT INTO corporate_events (
                            instrument_id, broker_ticker, event_date, event_type,
                            ratio, source_transaction_id, title_raw, source
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (broker_ticker, event_date, event_type, title_raw)
                        DO NOTHING
                        RETURNING id
                        """,
                        (
                            instrument_id,
                            ce["ticker"],
                            row.transaction_date,
                            ce["event_type"],
                            ce["ratio"],
                            transaction_id,
                            row.title_raw,
                            "title_parser",
                        ),
                    )
                    if cur.fetchone() is not None:
                        summary.corporate_events_detected += 1

            cur.execute(
                """
                INSERT INTO source_runs (source, row_count, sha256_input)
                VALUES (%s, %s, %s)
                ON CONFLICT (source, sha256_input) DO NOTHING
                """,
                (pf.path.name, pf.row_count, pf.sha256),
            )

        conn.commit()

        cur.execute(
            """
            SELECT rachunek,
                   count(*) FILTER (WHERE row_type = 'kupno') AS n_kupno,
                   count(*) FILTER (WHERE row_type = 'sprzedaz') AS n_sprzedaz,
                   count(DISTINCT instrument_id) FILTER (WHERE row_type IN ('kupno', 'sprzedaz')) AS n_tickers
            FROM transactions
            GROUP BY rachunek
            ORDER BY rachunek
            """
        )
        for rachunek, n_kupno, n_sprzedaz, n_tickers in cur.fetchall():
            summary.per_rachunek[rachunek] = {
                "kupno": n_kupno,
                "sprzedaz": n_sprzedaz,
                "distinct_tickers": n_tickers,
            }

    return summary

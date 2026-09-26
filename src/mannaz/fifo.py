"""FIFO pozycji otwartych per (rachunek, instrument, waluta rozliczenia).
Brief CC-P, P2.5 + poprawka P2.3/P2.5 (kontrakty ze znakiem i wygasaniem).

Zdarzenia korporacyjne (tabela corporate_events) są stosowane PRZED FIFO, w
kolejności chronologicznej razem z transakcjami: gdy `ratio` zdarzenia jest
znane, wszystkie loty otwarte przed `event_date` są przeskalowane
(qty *= ratio, koszt_jednostkowy /= ratio) w momencie napotkania zdarzenia w
skanie chronologicznym. Zdarzenia korporacyjne z `ratio IS NULL` (np.
`certificate_redemption` / `share_exchange` bez wykrytego realnego
współczynnika) są pomijane w tym kroku — nie ma czym skalować.

Sprzedaż/kupno większe niż suma przeciwstawnych lotów FIFO jest netowane
najpierw względem istniejących lotów o przeciwnym znaku (pokrycie krótkiej
pozycji kupnem, domknięcie długiej pozycji sprzedażą), a dopiero nadwyżka
tworzy nowy lot:
  - nadwyżka KUPNA -> zawsze nowy lot długi (kupowanie nigdy nie jest
    anomalią),
  - nadwyżka SPRZEDAŻY, gdy `allow_short=True` (rachunek KONTRAKTOWY —
    pozycje ze znakiem, sprzedaż bez pozycji = świadome otwarcie krótkiej) ->
    nowy lot krótki z realnym kosztem jednostkowym (znamy cenę sprzedaży),
  - nadwyżka SPRZEDAŻY, gdy `allow_short=False` (rachunki akcyjne — brak
    wcześniejszego kupna to prawdopodobnie luka w historii, nie świadomy
    short) -> nowy lot ujemny z umownym kosztem zerowym (nie znamy kosztu
    historycznego), liczony osobno jako `oversell_events`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import psycopg

from mannaz.parse_history import compute_futures_expiry


@dataclass
class PositionResult:
    qty: Decimal
    residual_cost: Decimal
    first_entry_date: date | None
    last_entry_date: date | None
    oversell_events: int


def compute_position(
    rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    allow_short: bool = False,
) -> PositionResult:
    """rows: [{'date': date, 'type': 'kupno'|'sprzedaz', 'qty': Decimal, 'amount': Decimal(>=0)}], nieposortowane.
    events: [{'date': date, 'ratio': Decimal}] (tylko zdarzenia ze znanym ratio), nieposortowane.
    allow_short: True dla rachunków, na których świadome krótkie pozycje są
    normalne (KONTRAKTOWY) — wtedy nadwyżka sprzedaży NIE jest liczona jako
    `oversell_events` i dostaje realny koszt jednostkowy zamiast zera."""

    rows_sorted = sorted(rows, key=lambda r: r["date"])
    events_sorted = sorted(events, key=lambda e: e["date"])

    lots: deque[list] = deque()  # [qty, unit_cost, entry_date] — listy, bo qty/unit_cost mutowalne
    first_entry_date: date | None = None
    last_entry_date: date | None = None
    oversell_events = 0

    i, j = 0, 0
    while i < len(events_sorted) or j < len(rows_sorted):
        take_event = i < len(events_sorted) and (
            j >= len(rows_sorted) or events_sorted[i]["date"] <= rows_sorted[j]["date"]
        )
        if take_event:
            ratio = events_sorted[i]["ratio"]
            for lot in lots:
                lot[0] = lot[0] * ratio
                lot[1] = lot[1] / ratio
            i += 1
            continue

        r = rows_sorted[j]
        unit_price = (r["amount"] / r["qty"]) if r["qty"] else Decimal(0)
        signed_qty = r["qty"] if r["type"] == "kupno" else -r["qty"]

        if r["type"] == "kupno":
            first_entry_date = r["date"] if first_entry_date is None else min(first_entry_date, r["date"])
            last_entry_date = r["date"] if last_entry_date is None else max(last_entry_date, r["date"])

        remaining = signed_qty
        # Najpierw domykamy/pokrywamy istniejące loty o PRZECIWNYM znaku, od najstarszego (FIFO).
        while remaining != 0 and lots and (lots[0][0] > 0) != (remaining > 0):
            lot = lots[0]
            if abs(lot[0]) <= abs(remaining):
                remaining += lot[0]
                lots.popleft()
            else:
                lot[0] += remaining
                remaining = Decimal(0)

        if remaining > 0:
            lots.append([remaining, unit_price, r["date"]])
        elif remaining < 0:
            if allow_short:
                # świadome otwarcie/powiększenie krótkiej pozycji — znamy cenę sprzedaży
                lots.append([remaining, unit_price, r["date"]])
            else:
                oversell_events += 1
                # brak wcześniejszego lota na pełną ilość -> pozycja ujemna, koszt umowny 0
                # (nie znamy kosztu historycznego sprzedanych "znikąd" jednostek)
                lots.append([remaining, Decimal(0), r["date"]])
        j += 1

    qty_total = sum((lot[0] for lot in lots), Decimal(0))
    cost_total = sum((lot[0] * lot[1] for lot in lots if lot[0] > 0), Decimal(0))

    return PositionResult(
        qty=qty_total,
        residual_cost=cost_total,
        first_entry_date=first_entry_date,
        last_entry_date=last_entry_date,
        oversell_events=oversell_events,
    )


def resolve_effective_qty_after_expiry(
    qty: Decimal,
    instrument_type: str,
    contract_expiry: date | None,
    as_of: date,
) -> tuple[Decimal, bool]:
    """Jeśli instrument to wygasły kontrakt terminowy (instrument_type ==
    'future', contract_expiry <= as_of) i po FIFO wciąż ma niezerową ilość
    (brak transakcji zamykającej w danych) — traktuj jako zamknięty przez
    wygaśnięcie na datę wygaśnięcia, niezależnie od znaku pozycji.
    Zwraca (efektywna_ilość, czy_zamknięto_przez_wygaśnięcie)."""
    if (
        instrument_type == "future"
        and contract_expiry is not None
        and contract_expiry <= as_of
        and qty != 0
    ):
        return Decimal(0), True
    return qty, False


@dataclass
class FifoSummary:
    open_positions_per_rachunek: dict[str, int] = field(default_factory=dict)
    negative_qty_tickers: dict[str, list[str]] = field(default_factory=dict)  # rachunek -> [broker_ticker,...]
    oversell_tickers: dict[str, list[str]] = field(default_factory=dict)  # rachunek -> [broker_ticker,...]
    date_range_per_rachunek: dict[str, tuple[date, date]] = field(default_factory=dict)
    expired_closures_per_rachunek: dict[str, int] = field(default_factory=dict)


# Rachunek KONTRAKTOWY = pozycje ze znakiem (short dozwolony świadomie, brief P2.5 poprawka).
KONTRAKTOWY_PREFIX = "KONTRAKTOWY"


def ensure_contract_expiry_populated(conn: psycopg.Connection) -> int:
    """Uzupełnia instruments.contract_expiry dla instrumentów typu 'future',
    których kod serii da się rozpoznać (cykl kwartalny H/M/U/Z), a kolumna
    jest jeszcze pusta. Zwraca liczbę zaktualizowanych wierszy. Idempotentne —
    bezpieczne do wywołania na starcie każdego run_fifo."""
    updated = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, broker_ticker FROM instruments "
            "WHERE instrument_type = 'future' AND contract_expiry IS NULL"
        )
        rows = cur.fetchall()
        for instrument_id, broker_ticker in rows:
            expiry = compute_futures_expiry(broker_ticker)
            if expiry is None:
                continue
            cur.execute(
                "UPDATE instruments SET contract_expiry = %s WHERE id = %s",
                (expiry, instrument_id),
            )
            updated += 1
    conn.commit()
    return updated


def run_fifo(conn: psycopg.Connection, as_of: date | None = None) -> FifoSummary:
    if as_of is None:
        as_of = date.today()

    summary = FifoSummary()
    ensure_contract_expiry_populated(conn)

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT t.rachunek, t.instrument_id, t.currency
            FROM transactions t
            WHERE t.row_type IN ('kupno', 'sprzedaz') AND t.instrument_id IS NOT NULL
            """
        )
        groups = cur.fetchall()

        open_counts: dict[str, int] = {}
        negative_map: dict[str, list[str]] = {}
        oversell_map: dict[str, list[str]] = {}
        expired_counts: dict[str, int] = {}

        for rachunek, instrument_id, currency in groups:
            allow_short = rachunek.upper().startswith(KONTRAKTOWY_PREFIX)

            cur.execute(
                """
                SELECT transaction_date, row_type, qty, amount
                FROM transactions
                WHERE rachunek = %s AND instrument_id = %s AND currency = %s
                  AND row_type IN ('kupno', 'sprzedaz')
                ORDER BY transaction_date, id
                """,
                (rachunek, instrument_id, currency),
            )
            rows = [
                {"date": d, "type": rt, "qty": qty, "amount": abs(amount)}
                for d, rt, qty, amount in cur.fetchall()
            ]

            cur.execute(
                """
                SELECT event_date, ratio FROM corporate_events
                WHERE instrument_id = %s AND ratio IS NOT NULL
                ORDER BY event_date
                """,
                (instrument_id,),
            )
            events = [{"date": d, "ratio": ratio} for d, ratio in cur.fetchall()]

            pos = compute_position(rows, events, allow_short=allow_short)

            cur.execute(
                "SELECT broker_ticker, instrument_type, contract_expiry FROM instruments WHERE id = %s",
                (instrument_id,),
            )
            broker_ticker, instrument_type, contract_expiry = cur.fetchone()

            if not allow_short and pos.oversell_events > 0:
                oversell_map.setdefault(rachunek, []).append(broker_ticker)

            effective_qty, expired_closed = resolve_effective_qty_after_expiry(
                pos.qty, instrument_type, contract_expiry, as_of
            )

            if expired_closed:
                expired_counts[rachunek] = expired_counts.get(rachunek, 0) + 1
                cur.execute(
                    "DELETE FROM positions_fifo WHERE rachunek = %s AND instrument_id = %s AND currency = %s",
                    (rachunek, instrument_id, currency),
                )
                # pozycja domknięta przez wygaśnięcie — nie liczona jako otwarta ani ujemna
            elif effective_qty != 0:
                open_counts[rachunek] = open_counts.get(rachunek, 0) + 1
                if effective_qty < 0 and not allow_short:
                    negative_map.setdefault(rachunek, []).append(broker_ticker)

                cur.execute(
                    """
                    INSERT INTO positions_fifo (
                        rachunek, instrument_id, currency, qty, residual_cost,
                        first_entry_date, last_entry_date, entry_max_high
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)
                    ON CONFLICT (rachunek, instrument_id, currency) DO UPDATE SET
                        qty = EXCLUDED.qty,
                        residual_cost = EXCLUDED.residual_cost,
                        first_entry_date = EXCLUDED.first_entry_date,
                        last_entry_date = EXCLUDED.last_entry_date,
                        computed_at = now()
                    """,
                    (
                        rachunek,
                        instrument_id,
                        currency,
                        pos.qty,
                        pos.residual_cost,
                        pos.first_entry_date,
                        pos.last_entry_date,
                    ),
                )
            else:
                # pozycja domknięta (qty == 0) — usuń ewentualny stary wpis, jeśli istniał
                cur.execute(
                    "DELETE FROM positions_fifo WHERE rachunek = %s AND instrument_id = %s AND currency = %s",
                    (rachunek, instrument_id, currency),
                )

        conn.commit()

        cur.execute(
            "SELECT rachunek, min(transaction_date), max(transaction_date) FROM transactions GROUP BY rachunek"
        )
        for rachunek, dmin, dmax in cur.fetchall():
            summary.date_range_per_rachunek[rachunek] = (dmin, dmax)

    summary.open_positions_per_rachunek = open_counts
    summary.negative_qty_tickers = negative_map
    summary.oversell_tickers = oversell_map
    summary.expired_closures_per_rachunek = expired_counts
    return summary

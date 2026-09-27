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

Brief CC-S, S2 (naprawa F1/F2 — look-ahead w `run_risk`): `positions_as_of`
liczy pozycje PUNKTOWO na dowolny dzień `as_of` (nie tylko "dziś") wprost z
`transactions`/`corporate_events` — bez czytania `positions_fifo` (ten stan
jest zawsze "na dziś", niezależnie od tego, jaki `as_of` interesuje
wywołującego). `run_fifo` i `positions_as_of` dzielą JEDEN rdzeń
(`_compute_positions`/`_resolve_position`/`resolve_position_as_of`) — `run_fifo`
dodatkowo zapisuje wynik do `positions_fifo` (stan "na dziś", jak dotychczas).
F2: wykup certyfikatu domyka pozycję tylko, gdy zdarzenie jest NIE WCZEŚNIEJSZE
niż OSTATNIA transakcja kupna/sprzedaży ZNANA NA `as_of` (podzapytanie
max(transaction_date) filtrowane `<= as_of` — wcześniej filtr ten brakował,
więc late-arriving repurchase (transakcja po `as_of`) mogła fałszywie maskować
wykup, który z perspektywy `as_of` już domykał pozycję).
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


# Mapowanie row_type -> typ FIFO ('kupno'/'sprzedaz') — bilans otwarcia i
# wymiana instrumentu (zamiana_wydanie/zamiana_przyjecie) są dla FIFO
# równoważne kupnu/sprzedaży (brief CC-P P2.5, przekrój B/R/K).
_ROW_TYPE_TO_FIFO_TYPE = {
    "bilans_otwarcia": "kupno",
    "zamiana_przyjecie": "kupno",
    "zamiana_wydanie": "sprzedaz",
}
_FIFO_ROW_TYPES = ("kupno", "sprzedaz", "bilans_otwarcia", "zamiana_przyjecie", "zamiana_wydanie")


def resolve_position_as_of(
    rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    as_of: date,
    instrument_type: str,
    contract_expiry: date | None,
    certificate_redemption_dates: list[date],
    allow_short: bool = False,
) -> tuple[PositionResult, Decimal, bool]:
    """Czysty rdzeń pozycji NA DZIEŃ `as_of` (brief CC-S, S2) — bez bazy,
    testowalny wprost (żadna zależność od kursora SQL). Sam filtruje
    `rows`/`events` do `date <= as_of` (dzięki temu wywołujący może przekazać
    całą historię instrumentu i dostać poprawny wynik dla dowolnego `as_of`).

    rows: [{'date','row_type','qty','amount'}], row_type NIEZMAPOWANY (jak w
    `transactions.row_type` — mapowanie na 'kupno'/'sprzedaz' odbywa się tu, tak
    samo jak w `run_fifo`/`_entry_transactions`). Wiersze o innym row_type są
    ignorowane (jak w oryginalnym filtrze SQL `row_type IN (...)`).
    events: [{'date','ratio'}] — zdarzenia korporacyjne ze znanym ratio (jak w
    `run_fifo`, WSZYSTKIE typy, nie tylko split/reverse_split — tu chodzi o
    skalowanie lotów FIFO, nie o warstwę cen, patrz `risk.py` S3).
    certificate_redemption_dates: daty zdarzeń `certificate_redemption` dla
    tego instrumentu (dowolna liczba, nieposortowane).

    Zwraca (PositionResult, effective_qty, expired_closed) — `effective_qty`
    już uwzględnia wygaśnięcie kontraktu terminowego i wykup certyfikatu
    (F2 fix: oba sprawdzenia ograniczone do `<= as_of`, więc zdarzenie/
    transakcja PO `as_of` nigdy nie wpływa na wynik na `as_of` — brak
    look-ahead)."""
    rows_f = [r for r in rows if r["date"] <= as_of]
    events_f = [e for e in events if e["date"] <= as_of]

    fifo_rows = [
        {
            "date": r["date"],
            "type": _ROW_TYPE_TO_FIFO_TYPE.get(r["row_type"], r["row_type"]),
            "qty": r["qty"],
            "amount": abs(r["amount"]),
        }
        for r in rows_f
        if r["row_type"] in _FIFO_ROW_TYPES
    ]
    pos = compute_position(fifo_rows, events_f, allow_short=allow_short)

    effective_qty, expired_closed = resolve_effective_qty_after_expiry(
        pos.qty, instrument_type, contract_expiry, as_of
    )

    if not expired_closed and effective_qty != 0:
        # F2: wykup certyfikatu domyka pozycję, gdy nie ma PO NIM (a do
        # `as_of` włącznie) żadnej transakcji kupna/sprzedaży — obie granice
        # (transakcje i zdarzenie) liczone WYŁĄCZNIE z perspektywy `as_of`.
        buy_sell_dates = [r["date"] for r in rows_f if r["row_type"] in ("kupno", "sprzedaz")]
        max_txn_date = max(buy_sell_dates) if buy_sell_dates else None
        if max_txn_date is not None and any(
            max_txn_date <= d <= as_of for d in certificate_redemption_dates
        ):
            effective_qty, expired_closed = Decimal(0), True

    return pos, effective_qty, expired_closed


def _resolve_position(
    cur: psycopg.Cursor, rachunek: str, instrument_id: int, currency: str, as_of: date
) -> dict[str, Any]:
    """Wrapper DB nad `resolve_position_as_of` dla jednej grupy (rachunek,
    instrument, waluta rozliczenia) — pobiera CAŁĄ historię do `as_of`
    (transakcje/zdarzenia/wykup) i deleguje liczenie do rdzenia czystego."""
    allow_short = rachunek.upper().startswith(KONTRAKTOWY_PREFIX)

    cur.execute(
        """
        SELECT transaction_date, row_type, qty, amount
        FROM transactions
        WHERE rachunek = %s AND instrument_id = %s AND currency = %s
          AND row_type IN ('kupno', 'sprzedaz', 'bilans_otwarcia', 'zamiana_przyjecie', 'zamiana_wydanie')
          AND transaction_date <= %s
        ORDER BY transaction_date, id
        """,
        (rachunek, instrument_id, currency, as_of),
    )
    rows = [
        {"date": d, "row_type": rt, "qty": qty, "amount": amount}
        for d, rt, qty, amount in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT event_date, ratio FROM corporate_events
        WHERE instrument_id = %s AND ratio IS NOT NULL AND event_date <= %s
        ORDER BY event_date
        """,
        (instrument_id, as_of),
    )
    events = [{"date": d, "ratio": ratio} for d, ratio in cur.fetchall()]

    cur.execute(
        "SELECT broker_ticker, instrument_type, contract_expiry FROM instruments WHERE id = %s",
        (instrument_id,),
    )
    broker_ticker, instrument_type, contract_expiry = cur.fetchone()

    cur.execute(
        "SELECT event_date FROM corporate_events WHERE instrument_id = %s AND event_type = 'certificate_redemption'",
        (instrument_id,),
    )
    certificate_redemption_dates = [row[0] for row in cur.fetchall()]

    pos, effective_qty, expired_closed = resolve_position_as_of(
        rows, events, as_of, instrument_type, contract_expiry,
        certificate_redemption_dates, allow_short=allow_short,
    )

    return {
        "rachunek": rachunek,
        "instrument_id": instrument_id,
        "currency": currency,
        "pos": pos,
        "broker_ticker": broker_ticker,
        "instrument_type": instrument_type,
        "allow_short": allow_short,
        "effective_qty": effective_qty,
        "expired_closed": expired_closed,
    }


def _compute_positions(cur: psycopg.Cursor, as_of: date) -> list[dict[str, Any]]:
    """Rdzeń DB (brief CC-S, S2): jedna implementacja pętli FIFO dzielona przez
    `run_fifo` (as_of=dziś, zapis do `positions_fifo`) i `positions_as_of`
    (odczyt punktowy, bez zapisu). Zwraca surowe składniki per grupa (rachunek,
    instrument, waluta) — filtrowanie na "otwarte na as_of" robi wywołujący."""
    cur.execute(
        """
        SELECT DISTINCT t.rachunek, t.instrument_id, t.currency
        FROM transactions t
        WHERE t.row_type IN ('kupno', 'sprzedaz', 'bilans_otwarcia', 'zamiana_przyjecie', 'zamiana_wydanie')
          AND t.instrument_id IS NOT NULL
          AND t.transaction_date <= %s
        """,
        (as_of,),
    )
    groups = cur.fetchall()
    return [
        _resolve_position(cur, rachunek, instrument_id, currency, as_of)
        for rachunek, instrument_id, currency in groups
    ]


def positions_as_of(conn_or_cur: psycopg.Connection | psycopg.Cursor, as_of: date) -> list[dict[str, Any]]:
    """S2 (brief CC-S, F1 fix): pozycje otwarte na KONIEC DNIA `as_of`, liczone
    punktowo z `transactions`/`corporate_events` (nie z `positions_fifo`, który
    jest zawsze stanem "na dziś" — pozycja zamknięta po `as_of` by tam już nie
    istniała, split po `as_of` by już przeskalował ilość). Zwraca listę dictów
    z tymi samymi kluczami co wiersz `positions_fifo`: rachunek, instrument_id,
    currency, qty, residual_cost, first_entry_date, last_entry_date. Pomija
    pozycje domknięte na `as_of` (qty==0 albo domknięte przez wygaśnięcie
    kontraktu/wykup certyfikatu, patrz `resolve_position_as_of`).
    `conn_or_cur`: połączenie LUB istniejący kursor (przydatne w testach
    bazodanowych z jedną transakcją)."""

    def _run(cur: psycopg.Cursor) -> list[dict[str, Any]]:
        out = []
        for item in _compute_positions(cur, as_of):
            if item["expired_closed"] or item["effective_qty"] == 0:
                continue
            pos = item["pos"]
            out.append(
                {
                    "rachunek": item["rachunek"],
                    "instrument_id": item["instrument_id"],
                    "currency": item["currency"],
                    "qty": pos.qty,
                    "residual_cost": pos.residual_cost,
                    "first_entry_date": pos.first_entry_date,
                    "last_entry_date": pos.last_entry_date,
                }
            )
        return out

    if hasattr(conn_or_cur, "cursor"):
        with conn_or_cur.cursor() as cur:  # type: ignore[union-attr]
            return _run(cur)
    return _run(conn_or_cur)  # type: ignore[arg-type]


@dataclass
class FifoSummary:
    open_positions_per_rachunek: dict[str, int] = field(default_factory=dict)
    negative_qty_tickers: dict[str, list[str]] = field(default_factory=dict)  # rachunek -> [broker_ticker,...]
    oversell_tickers: dict[str, list[str]] = field(default_factory=dict)  # rachunek -> [broker_ticker,...]
    date_range_per_rachunek: dict[str, tuple[date, date]] = field(default_factory=dict)
    expired_closures_per_rachunek: dict[str, int] = field(default_factory=dict)


# Rachunek KONTRAKTOWY = pozycje ze znakiem (short dozwolony świadomie, brief P2.5 poprawka).
KONTRAKTOWY_PREFIX = "KONTRAKTOWY"


def ensure_contract_expiry_populated(conn: psycopg.Connection, commit: bool = True) -> int:
    """Uzupełnia instruments.contract_expiry dla instrumentów typu 'future',
    których kod serii da się rozpoznać (cykl kwartalny H/M/U/Z), a kolumna
    jest jeszcze pusta. Zwraca liczbę zaktualizowanych wierszy. Idempotentne —
    bezpieczne do wywołania na starcie każdego run_fifo. `commit=False` (brief
    CC-C, C2) — do testów DB z rollbackiem, tak jak `prices.run_prices_fetch`/
    `risk.run_risk`."""
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
    if commit:
        conn.commit()
    return updated


def run_fifo(conn: psycopg.Connection, as_of: date | None = None, commit: bool = True) -> FifoSummary:
    """Brief CC-S, S2: pętla FIFO nie jest już zduplikowana tutaj — liczy przez
    ten sam rdzeń co `positions_as_of` (`_compute_positions`), z `as_of`
    domyślnie dzisiejszym (jak dotychczas). Zapis do `positions_fifo` (te same
    kolumny/DELETE dla zamkniętych) bez zmian względem poprzedniej wersji.
    `commit=False` (brief CC-C, C2) — do testów DB z rollbackiem."""
    if as_of is None:
        as_of = date.today()

    summary = FifoSummary()
    ensure_contract_expiry_populated(conn, commit=commit)

    with conn.cursor() as cur:
        open_counts: dict[str, int] = {}
        negative_map: dict[str, list[str]] = {}
        oversell_map: dict[str, list[str]] = {}
        expired_counts: dict[str, int] = {}

        for item in _compute_positions(cur, as_of):
            rachunek = item["rachunek"]
            instrument_id = item["instrument_id"]
            currency = item["currency"]
            pos = item["pos"]
            broker_ticker = item["broker_ticker"]
            allow_short = item["allow_short"]
            effective_qty = item["effective_qty"]
            expired_closed = item["expired_closed"]

            if not allow_short and pos.oversell_events > 0:
                oversell_map.setdefault(rachunek, []).append(broker_ticker)

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

        if commit:
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

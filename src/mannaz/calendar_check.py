"""Strażnik kalendarza sesyjnego — brief CC-P, P3.4 (patrz też brief C2 /
§T25 dokumentu projektowego, P-04).

Dla każdego instrumentu z cenami w `prices_daily` porównujemy zbiór dat, dla
których MAMY cenę, ze zbiorem sesji zwróconym przez `exchange_calendars` dla
odpowiedniego kalendarza (dobór po `instruments.exchange`, patrz
`EXCHANGE_TO_CALENDAR_CODE`), w zakresie [pierwsza cena, ostatnia cena] tego
konkretnego instrumentu (NIE całego okna P3, żeby świeżo dodany instrument nie
generował sztucznie tysięcy "brakujących" sesji sprzed jego pierwszej ceny).

Różnica symetryczna (`symmetric_difference`) per instrument — obie kierunki są
błędem:
  - sesja w kalendarzu, dla której NIE MAMY ceny -> dziura w danych (albo
    źle dobrany kalendarz),
  - cena na dzień, którego kalendarz NIE UZNAJE za sesję -> błąd kalendarza
    (`exchange_calendars` jest utrzymywany przez społeczność, bez gwarancji —
    §T25/P-04 dokumentu projektowego) ALBO błąd doboru kalendarza (zła giełda).

P-04 (dokument projektowy): to porównanie JEST tym pomiarem — wynik
`{0: n, >0: m}` + przykładowe daty rozbieżności per instrument."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import exchange_calendars as xcals
import psycopg

# Dobór kalendarza po znormalizowanym polu instruments.exchange (P3.1:
# instruments_map.EXCHANGE_FULLNAME_TO_CODE). US ma DWA kalendarze zależnie od
# giełdy notowania (NASDAQ vs NYSE) — GPW/Xetra/Amsterdam/Toronto po jednym.
EXCHANGE_TO_CALENDAR_CODE: dict[str, str] = {
    "GPW": "XWAR",
    "NASDAQ": "XNAS",
    "NYSE": "XNYS",
    "XETRA": "XETR",
    "AMSTERDAM": "XAMS",
    "TSX": "XTSE",
}

# NASDAQ i NYSE mają identyczny kalendarz sesji w praktyce (obie US equity),
# ale rozróżniamy explicit zamiast zakładać — jeśli kiedyś się rozjadą (np.
# dni żałoby narodowej ogłoszone różnie), guard tu ma to złapać, nie ukryć.


@dataclass
class InstrumentCalendarResult:
    instrument_id: int
    broker_ticker: str
    exchange: str | None
    calendar_code: str | None
    n_price_dates: int
    n_calendar_sessions: int
    symmetric_diff_count: int
    missing_prices_for_sessions: list[date] = field(default_factory=list)  # sesja bez ceny
    prices_on_non_sessions: list[date] = field(default_factory=list)  # cena poza sesją
    note: str = ""


@dataclass
class CalendarCheckSummary:
    per_instrument: list[InstrumentCalendarResult] = field(default_factory=list)

    @property
    def distribution(self) -> dict[str, int]:
        """{'0': n, '>0': m} — brief P3.4."""
        zero = sum(1 for r in self.per_instrument if r.symmetric_diff_count == 0)
        nonzero = sum(1 for r in self.per_instrument if r.symmetric_diff_count > 0)
        return {"0": zero, ">0": nonzero}


def compare_dates_to_calendar(
    price_dates: list[date],
    calendar_code: str,
    example_limit: int = 5,
) -> tuple[int, list[date], list[date]]:
    """Różnica symetryczna między `price_dates` (posiadane ceny) a sesjami
    kalendarza w zakresie [min(price_dates), max(price_dates)]. Czysta funkcja
    poza samym wywołaniem `exchange_calendars` (testowalna przez wstrzyknięcie
    zbioru sesji — patrz tests/test_calendar_check.py)."""
    if not price_dates:
        return 0, [], []
    start, end = min(price_dates), max(price_dates)
    cal = xcals.get_calendar(calendar_code)
    sessions = {ts.date() for ts in cal.sessions_in_range(start.isoformat(), end.isoformat())}
    prices = set(price_dates)

    missing_prices_for_sessions = sorted(sessions - prices)
    prices_on_non_sessions = sorted(prices - sessions)
    total = len(missing_prices_for_sessions) + len(prices_on_non_sessions)
    return total, missing_prices_for_sessions[:example_limit], prices_on_non_sessions[:example_limit]


def _instruments_with_prices(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT i.id, i.broker_ticker, i.exchange
        FROM instruments i
        WHERE EXISTS (SELECT 1 FROM prices_daily p WHERE p.instrument_id = i.id)
        ORDER BY i.id
        """
    )
    cols = ("id", "broker_ticker", "exchange")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _price_dates(cur: psycopg.Cursor, instrument_id: int) -> list[date]:
    cur.execute(
        "SELECT price_date FROM prices_daily WHERE instrument_id = %s ORDER BY price_date",
        (instrument_id,),
    )
    return [row[0] for row in cur.fetchall()]


def run_calendar_check(conn: psycopg.Connection) -> CalendarCheckSummary:
    summary = CalendarCheckSummary()

    with conn.cursor() as cur:
        instruments = _instruments_with_prices(cur)

        for inst in instruments:
            instrument_id = inst["id"]
            exchange = inst["exchange"]
            calendar_code = EXCHANGE_TO_CALENDAR_CODE.get(exchange) if exchange else None

            price_dates = _price_dates(cur, instrument_id)

            if calendar_code is None:
                summary.per_instrument.append(
                    InstrumentCalendarResult(
                        instrument_id=instrument_id,
                        broker_ticker=inst["broker_ticker"],
                        exchange=exchange,
                        calendar_code=None,
                        n_price_dates=len(price_dates),
                        n_calendar_sessions=0,
                        symmetric_diff_count=0,
                        note=f"brak kalendarza dla exchange={exchange!r} — nieporownywalne",
                    )
                )
                continue

            diff_count, missing, extra = compare_dates_to_calendar(price_dates, calendar_code)
            cal = xcals.get_calendar(calendar_code)
            n_sessions = (
                len(cal.sessions_in_range(min(price_dates).isoformat(), max(price_dates).isoformat()))
                if price_dates
                else 0
            )

            summary.per_instrument.append(
                InstrumentCalendarResult(
                    instrument_id=instrument_id,
                    broker_ticker=inst["broker_ticker"],
                    exchange=exchange,
                    calendar_code=calendar_code,
                    n_price_dates=len(price_dates),
                    n_calendar_sessions=n_sessions,
                    symmetric_diff_count=diff_count,
                    missing_prices_for_sessions=missing,
                    prices_on_non_sessions=extra,
                )
            )

    return summary

"""Ryzyko i sizing (dokument projektowy §19) — brief CC-P, P4.1 (per-pozycja:
ATR/stopy/REGIME/OSTRZEŻENIE/ryzyko PLN) + P4.2 (kontrakty terminowe: stopy na
bazie, ekspozycja przez multiplier, znak pozycji).

Warstwa cen: WYŁĄCZNIE `*_split_adj` (patrz `prices.py` — konwencja kolumn).
Nigdy `*_raw` w tym module (spójność z brief P4.1).

Konwencje liczbowe (patrz też każda funkcja):
  - Decimal wszędzie poza `math.log`/`statistics.pstdev`, które wymagają
    float — konwersja tam i z powrotem, tak jak w `prices.py::detect_log_return_outliers`.
  - "sesja" = wiersz `prices_daily` (data z ceną), NIE dzień kalendarzowy —
    okna (22/60/200/252) liczone po INDEKSIE w posortowanej serii cen, nie po
    różnicy dat.
  - sigma (zmienność) = odchylenie standardowe POPULACYJNE (statistics.pstdev)
    dziennych zwrotów logarytmicznych — wybór jawny, dokument nie precyzuje
    populacyjne/próbkowe; testowalne, do rewizji jeśli owner zażąda innej
    konwencji.

Semantyka "pierwszy/ostatni pozostały lot" (brief P4.1) NIE jest tym samym, co
`positions_fifo.first_entry_date`/`last_entry_date` — te dwie kolumny (z
`fifo.compute_position`) śledzą PIERWSZĄ/OSTATNIĄ transakcję kupna w CAŁEJ
historii instrumentu, niezależnie od tego, czy ten konkretny lot przetrwał do
dziś. Brief P4.1 chce dat lotów, które są OTWARTE na dzień D — dlatego
`compute_weighted_entry_price` w tym module odtwarza własny, niezależny
przebieg FIFO (na bazie `transactions.price`, nie `amount`) i czyta
first/last date z KOŃCOWEGO stanu kolejki lotów, nie z bieżącego licznika.

POPRAWKA (sesja główna 2026-09-26, §19.3 "dzień zero na żywej książce"):
`stop_chandelier_D` (główny stop, wchodzi do `stop_effective`) NIE jest już
ratchetowany od pierwszego POZOSTAŁEGO LOTU (historyczne wejście) — zapadka
zaczyna się od INICJALIZACJI SYSTEMU dla tej pozycji, czyli od najwcześniejszej
`risk_date` już zapisanej dla niej w `risk_daily` sprzed D. Brak takiego wiersza
(pierwszy dzień, w którym ta pozycja jest w ogóle liczona) -> RATCHET_START = D,
czyli wartość BEZ zapadki (`max(high,22) - 3*ATR22_D`, jeden dzień). Ratchet
rośnie o jeden dzień z każdym kolejnym uruchomieniem `risk` (RATCHET_START =
najwcześniejszy dotychczasowy dzień pomiaru, nie data wejścia w pozycję).
Stary wariant ("zapadka od pierwszego pozostałego lotu") jest ZACHOWANY jako
kolumna informacyjna `chandelier_from_entry` (+ `below_chandelier_from_entry`)
— NIE wchodzi do `stop_effective`. Patrz `_earliest_prior_risk_date`.

P4.2 (kontrakty terminowe): ATR/stopy/REGIME liczone na instrumencie BAZOWYM
(`instruments.base_symbol` -> `instruments.yahoo_symbol` drugiego wiersza),
NIE na samym kontrakcie — kontrakt sam nie ma serii cenowej w Yahoo (patrz
`instruments_map.py`). `entry_price`/`qty`/pierwszy-ostatni-lot liczone
NATOMIAST na transakcjach KONTRAKTU (jednostki kontraktu GPW dla akcji są w tej
samej skali co akcja bazowa — `cena kontraktu` z tytułu transakcji brokera to
przybliżenie ceny bazy, nie osobna waluta/skala). `multiplier IS NULL`
(FMDVZ26 na dziś) -> ryzyko NULL, `multiplier_missing=True`, wyłączone z sum."""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from mannaz.fifo import KONTRAKTOWY_PREFIX

# ---------------------------------------------------------------------------
# Stałe (§19 dokumentu projektowego + brief P4.1/P4.2)
# ---------------------------------------------------------------------------

ATR20_PERIOD = 20
ATR22_PERIOD = 22
CHANDELIER_WINDOW = 22
SMA200_PERIOD = 200
REGIME_LOOKBACK_SESSIONS = 21
SIGMA_SHORT_PERIOD = 20
SIGMA_LONG_PERIOD = 60
CHANNEL_LOOKBACK_SESSIONS = 252
WARNING_CHANNEL_RATIO = Decimal("0.75")
CHANDELIER_ATR_MULT = Decimal(3)
TWO_N_ATR_MULT = Decimal(2)

LEVEL1_PCT = Decimal(1)
LEVEL2_PCT = Decimal(3)
LEVEL3_PCT = Decimal(15)

# Splity GPW z jednostką w pensach (GBp) zamiast funtów — patrz corp_actions.py
# `_pence_adjustment_factor` dla identycznej heurystyki (symbol '.L' + waluta
# 'GBP'). Nie dotyczy obecnie posiadanych instrumentów (brak GBP w danych),
# reguła ogólna z briefu P4.1.
_LSE_PENCE_SUFFIX = ".L"


# ---------------------------------------------------------------------------
# Wilder ATR
# ---------------------------------------------------------------------------


def true_range(high: Decimal, low: Decimal, prev_close: Decimal | None) -> Decimal:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def wilder_atr_series(
    highs: list[Decimal], lows: list[Decimal], closes: list[Decimal], period: int
) -> list[Decimal | None]:
    """Seria ATR metodą Wildera, wyrównana indeksem do wejścia. `None` dopóki
    okno rozgrzewki (`period` sesji) się nie wypełni. Seed = średnia
    arytmetyczna TR pierwszych `period` sesji; dalej wygładzanie Wildera
    `(ATR[i-1]*(period-1) + TR[i]) / period`."""
    n = len(closes)
    if n == 0 or n < period:
        return [None] * n
    tr: list[Decimal] = [highs[0] - lows[0]]
    for i in range(1, n):
        tr.append(true_range(highs[i], lows[i], closes[i - 1]))

    atr: list[Decimal | None] = [None] * n
    atr[period - 1] = sum(tr[0:period], Decimal(0)) / Decimal(period)
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / Decimal(period)
    return atr


# ---------------------------------------------------------------------------
# Rolling max/min/SMA — okno pełne wymagane (None dopóki się nie wypełni)
# ---------------------------------------------------------------------------


def rolling_max(values: list[Decimal], window: int) -> list[Decimal | None]:
    n = len(values)
    out: list[Decimal | None] = [None] * n
    for i in range(n):
        if i + 1 < window:
            continue
        out[i] = max(values[i - window + 1 : i + 1])
    return out


def rolling_min(values: list[Decimal], window: int) -> list[Decimal | None]:
    n = len(values)
    out: list[Decimal | None] = [None] * n
    for i in range(n):
        if i + 1 < window:
            continue
        out[i] = min(values[i - window + 1 : i + 1])
    return out


def simple_moving_average(values: list[Decimal], period: int) -> list[Decimal | None]:
    n = len(values)
    out: list[Decimal | None] = [None] * n
    for i in range(n):
        if i + 1 < period:
            continue
        out[i] = sum(values[i - period + 1 : i + 1], Decimal(0)) / Decimal(period)
    return out


# ---------------------------------------------------------------------------
# GBp (pensy) -> GBP — brief P4.1: "GBp→/100 jeśli wystąpi"
# ---------------------------------------------------------------------------


def gbp_pence_factor(quote_currency: str | None, yahoo_symbol: str | None) -> Decimal:
    if quote_currency == "GBP" and yahoo_symbol and yahoo_symbol.endswith(_LSE_PENCE_SUFFIX):
        return Decimal("0.01")
    return Decimal(1)


# ---------------------------------------------------------------------------
# Chandelier (główny stop, zapadka) + stop 2N + stop_effective
# ---------------------------------------------------------------------------


def chandelier_series(
    highs: list[Decimal],
    lows: list[Decimal],
    atr22_series: list[Decimal | None],
    is_short: bool,
    window: int = CHANDELIER_WINDOW,
) -> list[Decimal | None]:
    """chandelier_t = max(high,22) - 3*ATR22 [long] / min(low,22) + 3*ATR22 [short].
    `window` parametryzowalne (domyślnie 22) wyłącznie dla testowalności na
    krótkich seriach syntetycznych — produkcyjne wywołania zawsze używają
    domyślnej wartości (brief P4.1)."""
    n = len(highs)
    if is_short:
        extreme = rolling_min(lows, window)
        return [
            (extreme[i] + CHANDELIER_ATR_MULT * atr22_series[i])
            if (extreme[i] is not None and atr22_series[i] is not None)
            else None
            for i in range(n)
        ]
    extreme = rolling_max(highs, window)
    return [
        (extreme[i] - CHANDELIER_ATR_MULT * atr22_series[i])
        if (extreme[i] is not None and atr22_series[i] is not None)
        else None
        for i in range(n)
    ]


def ratchet_extreme(values: list[Decimal | None], start_idx: int, end_idx: int, is_short: bool) -> Decimal | None:
    """max(long)/min(short) nieodrzucanych wartości `values[start_idx..end_idx]`
    włącznie — "zapadka" sprowadza się do jednorazowego max/min po całym oknie
    (matematycznie równoważne iteracyjnemu "nigdy w dół/górę")."""
    window = [v for v in values[start_idx : end_idx + 1] if v is not None]
    if not window:
        return None
    return min(window) if is_short else max(window)


def two_n_stop(entry_price: Decimal | None, atr20_at_last_entry: Decimal | None, is_short: bool) -> Decimal | None:
    if entry_price is None or atr20_at_last_entry is None:
        return None
    delta = TWO_N_ATR_MULT * atr20_at_last_entry
    return entry_price + delta if is_short else entry_price - delta


def stop_effective(
    stop_2n: Decimal | None, stop_chandelier: Decimal | None, is_short: bool
) -> tuple[Decimal | None, str | None]:
    """max(stop_2n, stop_chandelier) [long] / min(...) [short]; `stop_source`
    wskazuje, który wygrał ('two_n' wygrywa remisy, bo listowany pierwszy —
    Python max()/min() zwraca pierwszy napotkany ekstremum przy równości)."""
    candidates: list[tuple[Decimal, str]] = [
        (v, src) for v, src in ((stop_2n, "two_n"), (stop_chandelier, "chandelier")) if v is not None
    ]
    if not candidates:
        return None, None
    chosen = min(candidates, key=lambda t: t[0]) if is_short else max(candidates, key=lambda t: t[0])
    return chosen


# ---------------------------------------------------------------------------
# Wariant kontrolny B (do P4.4) — chandelier_hold: NIE jest zapadką, pojedyncza
# wartość na D, okno = cały czas posiadania (od pierwszego pozostałego lotu).
# ---------------------------------------------------------------------------


def is_breached(close_d: Decimal | None, level: Decimal | None, is_short: bool) -> bool | None:
    """Czy `close_d` jest po ZŁEJ stronie `level` — long: close_d < level;
    short: close_d > level. Wspólna dla below_stop / below_chandelier_hold /
    below_chandelier_from_entry (poprawka P4.1, sesja główna 2026-09-26)."""
    if close_d is None or level is None:
        return None
    return (close_d > level) if is_short else (close_d < level)


def chandelier_hold(
    highs: list[Decimal],
    lows: list[Decimal],
    first_entry_idx: int,
    d_idx: int,
    atr22_at_d: Decimal | None,
    is_short: bool,
) -> Decimal | None:
    if atr22_at_d is None or first_entry_idx > d_idx:
        return None
    if is_short:
        window_low = min(lows[first_entry_idx : d_idx + 1])
        return window_low + CHANDELIER_ATR_MULT * atr22_at_d
    window_high = max(highs[first_entry_idx : d_idx + 1])
    return window_high - CHANDELIER_ATR_MULT * atr22_at_d


# ---------------------------------------------------------------------------
# REGIME / OSTRZEŻENIE (§19.1 poz. 2 i 5)
# ---------------------------------------------------------------------------


def is_regime(close_d: Decimal | None, sma200_d: Decimal | None, sma200_prior: Decimal | None) -> bool:
    """REGIME = close_D < SMA200_D ∧ SMA200_D < SMA200_{D-21 sesji}."""
    if close_d is None or sma200_d is None or sma200_prior is None:
        return False
    return close_d < sma200_d and sma200_d < sma200_prior


def log_returns(values: list[Decimal | None]) -> list[Decimal | None]:
    out: list[Decimal | None] = []
    for a, b in zip(values, values[1:]):
        if a is None or b is None or a <= 0 or b <= 0:
            out.append(None)
        else:
            out.append(Decimal(str(math.log(float(b) / float(a)))))
    return out


def population_stdev(values: list[Decimal | None]) -> Decimal | None:
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 2:
        return None
    return Decimal(str(statistics.pstdev(clean)))


def is_warning(
    sigma20: Decimal | None,
    sigma60: Decimal | None,
    close_d: Decimal | None,
    max_close_252: Decimal | None,
) -> bool:
    """OSTRZEŻENIE = σ20 > 2×σ60 ∨ close_D / max(close,252) < 0,75."""
    cond_vol = sigma20 is not None and sigma60 is not None and sigma20 > Decimal(2) * sigma60
    cond_channel = (
        close_d is not None
        and max_close_252 is not None
        and max_close_252 != 0
        and (close_d / max_close_252) < WARNING_CHANNEL_RATIO
    )
    return bool(cond_vol or cond_channel)


# ---------------------------------------------------------------------------
# FX — kurs NBP A z D albo ostatni dostępny ≤ D (brief P4.1)
# ---------------------------------------------------------------------------


def resolve_fx_rate(
    rates: list[tuple[date, Decimal]], currency: str, target_date: date
) -> tuple[Decimal | None, date | None]:
    if currency == "PLN":
        return Decimal(1), target_date
    candidates = [(d, r) for d, r in rates if d <= target_date]
    if not candidates:
        return None, None
    d, r = max(candidates, key=lambda t: t[0])
    return r, d


# ---------------------------------------------------------------------------
# Ryzyko w walucie notowania + PLN + budżety §19.2
# ---------------------------------------------------------------------------


def compute_risk_native(
    close_d: Decimal | None,
    stop_eff: Decimal | None,
    qty_abs: Decimal,
    multiplier: Decimal | None,
    is_short: bool,
) -> tuple[Decimal | None, bool | None]:
    """Zwraca (ryzyko_native, below_stop). `below_stop=True` -> RISK=HIGH,
    ryzyko do sumy = 0 (brief: `max(close_D - stop_effective, 0)` już to daje
    automatycznie — po złej stronie różnica jest ujemna i `max(...,0)=0`)."""
    if close_d is None or stop_eff is None:
        return None, None
    raw_diff = (stop_eff - close_d) if is_short else (close_d - stop_eff)
    below_stop = raw_diff < 0
    mult = multiplier if multiplier is not None else Decimal(1)
    risk = max(raw_diff, Decimal(0)) * qty_abs * mult
    return risk, below_stop


def level1_check(
    risk_pln: Decimal | None, capital_satelite_pln: Decimal | None, threshold_pct: Decimal = LEVEL1_PCT
) -> tuple[Decimal | None, bool | None]:
    if risk_pln is None or not capital_satelite_pln:
        return None, None
    pct = risk_pln / capital_satelite_pln * Decimal(100)
    return pct, pct > threshold_pct


def level3_check(
    total_risk_pln: Decimal, capital_satelite_pln: Decimal | None, threshold_pct: Decimal = LEVEL3_PCT
) -> tuple[Decimal | None, bool | None]:
    if not capital_satelite_pln:
        return None, None
    pct = total_risk_pln / capital_satelite_pln * Decimal(100)
    return pct, pct > threshold_pct


@dataclass
class ThemeBudgetResult:
    theme: str
    risk_pct: Decimal | None
    breach: bool | None


def compute_theme_budgets(
    ticker_risk_pln: list[tuple[str, Decimal | None]],
    ticker_to_theme: dict[str, str],
    capital_satelite_pln: Decimal | None,
    threshold_pct: Decimal = LEVEL2_PCT,
) -> dict[str, ThemeBudgetResult]:
    """Poziom 2 (§19.2, do wdrożenia gdy tematy przyjdą osobno — brief P4.1):
    czysta funkcja, `ticker_to_theme` = mapa broker_ticker -> temat
    (`instruments.theme`); pozycje bez tematu (None) są pomijane."""
    sums: dict[str, Decimal] = {}
    for ticker, risk_pln in ticker_risk_pln:
        theme = ticker_to_theme.get(ticker)
        if theme is None or risk_pln is None:
            continue
        sums[theme] = sums.get(theme, Decimal(0)) + risk_pln

    out: dict[str, ThemeBudgetResult] = {}
    for theme, total in sums.items():
        pct, breach = level3_check(total, capital_satelite_pln, threshold_pct)
        out[theme] = ThemeBudgetResult(theme=theme, risk_pct=pct, breach=breach)
    return out


# ---------------------------------------------------------------------------
# Cena wejścia ważona pozostałymi lotami FIFO (brief P4.1) — NIEZALEŻNA od
# fifo.compute_position (patrz docstring modułu: potrzebujemy dat REMAINING
# lotów, nie "pierwszy/ostatni kupno w całej historii", i kosztu opartego na
# transactions.price zamiast amount/qty).
# ---------------------------------------------------------------------------


@dataclass
class WeightedEntryResult:
    entry_price: Decimal | None
    qty: Decimal
    first_remaining_date: date | None
    last_remaining_date: date | None


def compute_weighted_entry_price(
    rows: list[dict[str, Any]], events: list[dict[str, Any]], allow_short: bool
) -> WeightedEntryResult:
    """rows: [{'date','type':'kupno'|'sprzedaz','qty':Decimal,'price':Decimal|None}],
    nieposortowane. events: [{'date','ratio'}], tylko ratio IS NOT NULL (jak w
    fifo.run_fifo). Algorytm identyczny z `fifo.compute_position` (splity PRZED
    FIFO, netowanie najpierw względem przeciwnego znaku), ale:
      - koszt lotu = `price` transakcji (nie `amount/qty`),
      - agregacja kosztu obejmuje loty OBU znaków (potrzebne dla wejścia
        krótkiej pozycji kontraktu, KONTRAKTOWY/allow_short=True),
      - first/last data = z KOŃCOWEGO stanu kolejki lotów (pozostałe loty),
        nie z licznika aktualizowanego przy każdej transakcji kupna."""
    rows_sorted = sorted(rows, key=lambda r: r["date"])
    events_sorted = sorted(events, key=lambda e: e["date"])

    lots: deque[list] = deque()  # [qty, price, entry_date]
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
        price = r.get("price")
        signed_qty = r["qty"] if r["type"] == "kupno" else -r["qty"]

        remaining = signed_qty
        while remaining != 0 and lots and (lots[0][0] > 0) != (remaining > 0):
            lot = lots[0]
            if abs(lot[0]) <= abs(remaining):
                remaining += lot[0]
                lots.popleft()
            else:
                lot[0] += remaining
                remaining = Decimal(0)

        if remaining > 0:
            lots.append([remaining, price if price is not None else Decimal(0), r["date"]])
        elif remaining < 0:
            if allow_short:
                lots.append([remaining, price if price is not None else Decimal(0), r["date"]])
            else:
                lots.append([remaining, Decimal(0), r["date"]])
        j += 1

    if not lots:
        return WeightedEntryResult(entry_price=None, qty=Decimal(0), first_remaining_date=None, last_remaining_date=None)

    qty_total = sum((lot[0] for lot in lots), Decimal(0))
    cost_total = sum((lot[0] * lot[1] for lot in lots), Decimal(0))
    first_remaining = min(lot[2] for lot in lots)
    last_remaining = max(lot[2] for lot in lots)
    entry_price = (cost_total / qty_total) if qty_total != 0 else None

    return WeightedEntryResult(
        entry_price=entry_price,
        qty=qty_total,
        first_remaining_date=first_remaining,
        last_remaining_date=last_remaining,
    )


# ---------------------------------------------------------------------------
# Domyślne D — brief P4.1 [Z]: ostatnia sesja, dla której WSZYSTKIE instrumenty
# satelity Z CENAMI NA TĘ DATĘ mają niepusty close_split_adj. Instrument bez
# wiersza na daną datę (np. dane przestały napływać) NIE blokuje tej daty —
# blokuje tylko wiersz OBECNY z close_split_adj IS NULL (np. Yahoo zwrócił
# świecę bez zamknięcia). Patrz `resolve_default_risk_date` (DB) niżej.
# ---------------------------------------------------------------------------


def resolve_default_date(date_any_null_flags: list[tuple[date, bool]]) -> date | None:
    """date_any_null_flags: [(price_date, any_satellite_row_has_null_close)],
    nieposortowane, jeden wpis per data. Zwraca najpóźniejszą datę z flagą
    False, albo None gdy brak takiej daty."""
    for d, any_null in sorted(date_any_null_flags, key=lambda t: t[0], reverse=True):
        if not any_null:
            return d
    return None


# ---------------------------------------------------------------------------
# Orkiestracja / DB
# ---------------------------------------------------------------------------


@dataclass
class PositionRiskRow:
    rachunek: str
    instrument_id: int
    broker_ticker: str
    settlement_currency: str
    quote_currency: str | None
    instrument_type: str
    is_core: bool
    qty: Decimal
    position_kind: str  # 'long' | 'short'
    entry_price: Decimal | None
    close_d: Decimal | None
    atr20: Decimal | None
    atr22: Decimal | None
    sma200: Decimal | None
    stop_2n: Decimal | None
    stop_chandelier: Decimal | None
    stop_effective: Decimal | None
    stop_source: str | None
    chandelier_hold: Decimal | None
    below_chandelier_hold: bool | None
    chandelier_from_entry: Decimal | None
    below_chandelier_from_entry: bool | None
    regime: bool | None
    warning: bool | None
    risk_native: Decimal | None
    below_stop: bool | None
    fx_rate: Decimal | None
    fx_rate_date: date | None
    risk_pln: Decimal | None
    multiplier_missing: bool
    risk_pct_satellite_capital: Decimal | None = None
    level1_breach: bool | None = None
    note: str = ""


@dataclass
class RiskSummary:
    risk_date: date
    rows: list[PositionRiskRow] = field(default_factory=list)
    capital_satelite_positions_total: int = 0
    capital_satelite_positions_by_rachunek: dict[str, int] = field(default_factory=dict)
    below_stop_tickers: list[str] = field(default_factory=list)
    below_stop_zagraniczny_tickers: list[str] = field(default_factory=list)
    stop_source_counts: dict[str, int] = field(default_factory=dict)
    regime_tickers: list[str] = field(default_factory=list)
    warning_tickers: list[str] = field(default_factory=list)
    variant_b_tickers: list[str] = field(default_factory=list)
    variant_b_zagraniczny_satellite_value_pct: Decimal | None = None
    below_chandelier_from_entry_tickers: list[str] = field(default_factory=list)
    zagraniczny_satellite_value_pct_below_stop: Decimal | None = None
    total_risk_pct_satellite_capital: Decimal | None = None
    total_risk_pct_zagraniczny_satellite_capital: Decimal | None = None
    level1_breach_tickers: list[str] = field(default_factory=list)
    level3_breach: bool | None = None
    multiplier_missing_tickers: list[str] = field(default_factory=list)
    futures_nominal_sanity: dict[str, bool] = field(default_factory=dict)
    excluded_no_price_tickers: list[str] = field(default_factory=list)
    theme_budgets: dict[str, ThemeBudgetResult] = field(default_factory=dict)


def resolve_default_risk_date(conn: psycopg.Connection) -> date | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.price_date, bool_or(p.close_split_adj IS NULL) AS any_null
            FROM prices_daily p
            JOIN instruments i ON i.id = p.instrument_id
            WHERE i.is_core = FALSE
            GROUP BY p.price_date
            """
        )
        rows = cur.fetchall()
    return resolve_default_date([(d, bool(any_null)) for d, any_null in rows])


def _open_positions(cur: psycopg.Cursor) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT pf.rachunek, pf.instrument_id, pf.currency AS settlement_currency, pf.qty,
               i.broker_ticker, i.instrument_type, i.is_core, i.base_symbol, i.multiplier,
               i.yahoo_symbol, i.currency AS quote_currency, i.theme
        FROM positions_fifo pf
        JOIN instruments i ON i.id = pf.instrument_id
        ORDER BY pf.rachunek, i.broker_ticker
        """
    )
    cols = (
        "rachunek", "instrument_id", "settlement_currency", "qty",
        "broker_ticker", "instrument_type", "is_core", "base_symbol", "multiplier",
        "yahoo_symbol", "quote_currency", "theme",
    )
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _base_instrument(cur: psycopg.Cursor, base_symbol: str | None) -> dict[str, Any] | None:
    if not base_symbol:
        return None
    cur.execute(
        "SELECT id, currency, yahoo_symbol FROM instruments WHERE yahoo_symbol = %s",
        (base_symbol,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "currency": row[1], "yahoo_symbol": row[2]}


def _price_series(cur: psycopg.Cursor, instrument_id: int, end_date: date) -> dict[str, list]:
    cur.execute(
        """
        SELECT price_date, high_split_adj, low_split_adj, close_split_adj
        FROM prices_daily
        WHERE instrument_id = %s AND price_date <= %s
          AND high_split_adj IS NOT NULL AND low_split_adj IS NOT NULL AND close_split_adj IS NOT NULL
        ORDER BY price_date
        """,
        (instrument_id, end_date),
    )
    rows = cur.fetchall()
    return {
        "dates": [r[0] for r in rows],
        "highs": [r[1] for r in rows],
        "lows": [r[2] for r in rows],
        "closes": [r[3] for r in rows],
    }


def _instrument_split_events(cur: psycopg.Cursor, instrument_id: int, as_of: date) -> list[dict[str, Any]]:
    # as_of filtr: pozycja jest liczona PUNKTOWO na dzien D — split zarejestrowany
    # z data pozniejsza niz D nie mial jeszcze miejsca "z perspektywy D" i nie
    # moze skalowac lotow otwartych na ten dzien (positions_fifo to jedyny,
    # biezacy snapshot — bez tego filtra przyszly split zostalby bledny
    # zaaplikowany do stanu sprzed jego wystapienia).
    cur.execute(
        """
        SELECT event_date, ratio FROM corporate_events
        WHERE instrument_id = %s AND ratio IS NOT NULL AND event_date <= %s
        ORDER BY event_date
        """,
        (instrument_id, as_of),
    )
    return [{"date": d, "ratio": r} for d, r in cur.fetchall()]


def _entry_transactions(
    cur: psycopg.Cursor, rachunek: str, instrument_id: int, currency: str, as_of: date
) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT transaction_date, row_type, qty, price
        FROM transactions
        WHERE rachunek = %s AND instrument_id = %s AND currency = %s
          AND row_type IN ('kupno', 'sprzedaz', 'bilans_otwarcia', 'zamiana_przyjecie', 'zamiana_wydanie')
          AND transaction_date <= %s
        ORDER BY transaction_date, id
        """,
        (rachunek, instrument_id, currency, as_of),
    )
    return [
        {"date": d, "type": {"bilans_otwarcia": "kupno", "zamiana_przyjecie": "kupno", "zamiana_wydanie": "sprzedaz"}.get(rt, rt), "qty": qty, "price": price}
        for d, rt, qty, price in cur.fetchall()
    ]


def _earliest_prior_risk_date(
    cur: psycopg.Cursor, rachunek: str, instrument_id: int, settlement_currency: str, before_date: date
) -> date | None:
    """RATCHET_START (poprawka §19.3): najwcześniejsza `risk_date` już
    zapisana dla tej pozycji w `risk_daily` PRZED `before_date`. `None` gdy
    brak takiego wiersza -> dzień zero dla tej pozycji, ratchet zaczyna się
    dopiero od `before_date` samego (brak zapadki na pierwszym pomiarze)."""
    cur.execute(
        """
        SELECT MIN(risk_date) FROM risk_daily
        WHERE rachunek = %s AND instrument_id = %s AND settlement_currency = %s AND risk_date < %s
        """,
        (rachunek, instrument_id, settlement_currency, before_date),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _fx_rate_on_or_before(cur: psycopg.Cursor, currency: str, target_date: date) -> tuple[Decimal | None, date | None]:
    if currency == "PLN":
        return Decimal(1), target_date
    cur.execute(
        """
        SELECT rate_date, rate FROM fx_nbp
        WHERE pair = %s AND rate_date <= %s
        ORDER BY rate_date DESC LIMIT 1
        """,
        (f"{currency}/PLN", target_date),
    )
    row = cur.fetchone()
    if row is None:
        return None, None
    return row[1], row[0]


def run_risk(conn: psycopg.Connection, as_of: date | None = None) -> RiskSummary:
    if as_of is None:
        as_of = resolve_default_risk_date(conn)
        if as_of is None:
            raise RuntimeError("brak sesji z kompletnymi close_split_adj dla instrumentow satelity — nie da sie ustalic D")

    summary = RiskSummary(risk_date=as_of)

    with conn.cursor() as cur:
        positions = _open_positions(cur)
        instrument_theme_by_id: dict[int, str] = {
            p["instrument_id"]: p["theme"] for p in positions if p["theme"] is not None
        }

        capital_by_rachunek: dict[str, Decimal] = {}
        capital_total = Decimal(0)
        positions_count_by_rachunek: dict[str, int] = {}

        computed: list[dict[str, Any]] = []

        for pos in positions:
            broker_ticker = pos["broker_ticker"]
            rachunek = pos["rachunek"]
            instrument_type = pos["instrument_type"]
            is_core = bool(pos["is_core"])
            qty = pos["qty"]
            is_short = qty < 0
            position_kind = "short" if is_short else "long"
            note = ""
            multiplier_missing = False
            multiplier: Decimal | None = Decimal(1)

            # --- Instrument dla ceny (equity/etf: samo siebie; future: baza) ---
            if instrument_type == "future":
                base = _base_instrument(cur, pos["base_symbol"])
                if base is None:
                    computed.append(
                        _empty_row(
                            rachunek, pos["instrument_id"], broker_ticker, pos["settlement_currency"],
                            None, instrument_type, is_core, qty, position_kind,
                            note="base_instrument_not_found", multiplier_missing=True,
                        )
                    )
                    summary.excluded_no_price_tickers.append(broker_ticker)
                    continue
                price_instrument_id = base["id"]
                quote_currency = base["currency"]
                yahoo_symbol = base["yahoo_symbol"]
                multiplier = pos["multiplier"]
                if multiplier is None:
                    multiplier_missing = True
            elif instrument_type in ("equity", "etf"):
                price_instrument_id = pos["instrument_id"]
                quote_currency = pos["quote_currency"]
                yahoo_symbol = pos["yahoo_symbol"]
            else:
                computed.append(
                    _empty_row(
                        rachunek, pos["instrument_id"], broker_ticker, pos["settlement_currency"],
                        pos["quote_currency"], instrument_type, is_core, qty, position_kind,
                        note="unsupported_instrument_type", multiplier_missing=False,
                    )
                )
                summary.excluded_no_price_tickers.append(broker_ticker)
                continue

            series = _price_series(cur, price_instrument_id, as_of)
            dates = series["dates"]
            if not dates or dates[-1] != as_of:
                computed.append(
                    _empty_row(
                        rachunek, pos["instrument_id"], broker_ticker, pos["settlement_currency"],
                        quote_currency, instrument_type, is_core, qty, position_kind,
                        note="no_price_on_d", multiplier_missing=multiplier_missing,
                    )
                )
                summary.excluded_no_price_tickers.append(broker_ticker)
                continue

            pence = gbp_pence_factor(quote_currency, yahoo_symbol)
            highs = [h * pence for h in series["highs"]]
            lows = [low * pence for low in series["lows"]]
            closes = [c * pence for c in series["closes"]]
            d_idx = len(dates) - 1

            atr20 = wilder_atr_series(highs, lows, closes, ATR20_PERIOD)
            atr22 = wilder_atr_series(highs, lows, closes, ATR22_PERIOD)
            sma200 = simple_moving_average(closes, SMA200_PERIOD)
            chand_series = chandelier_series(highs, lows, atr22, is_short)

            close_d = closes[d_idx]
            atr20_d = atr20[d_idx]
            atr22_d = atr22[d_idx]
            sma200_d = sma200[d_idx]
            sma200_prior = sma200[d_idx - REGIME_LOOKBACK_SESSIONS] if d_idx >= REGIME_LOOKBACK_SESSIONS else None

            lr = log_returns(closes)
            sigma20 = population_stdev(lr[-SIGMA_SHORT_PERIOD:]) if len(lr) >= SIGMA_SHORT_PERIOD else None
            sigma60 = population_stdev(lr[-SIGMA_LONG_PERIOD:]) if len(lr) >= SIGMA_LONG_PERIOD else None
            window_252 = closes[-CHANNEL_LOOKBACK_SESSIONS:]
            max_close_252 = max(window_252) if window_252 else None

            regime = is_regime(close_d, sma200_d, sma200_prior)
            warning = is_warning(sigma20, sigma60, close_d, max_close_252)

            # --- entry price / qty / pierwszy-ostatni pozostały lot ---
            events = _instrument_split_events(
                cur,
                price_instrument_id if instrument_type != "future" else pos["instrument_id"],
                as_of,
            )
            # Splity dotyczą samego instrumentu wycenianego (equity/etf: siebie;
            # future: kontrakt — GPW nie splituje kontraktów, ale zapytanie po
            # instrument_id kontraktu jest bezpieczne/nogatywne w praktyce).
            allow_short = rachunek.upper().startswith(KONTRAKTOWY_PREFIX)
            txn_rows = _entry_transactions(cur, rachunek, pos["instrument_id"], pos["settlement_currency"], as_of)
            entry_result = compute_weighted_entry_price(txn_rows, events, allow_short=allow_short)

            if entry_result.qty != qty:
                note = (note + ";" if note else "") + "entry_qty_mismatch_vs_positions_fifo"

            first_idx = None
            last_idx = None
            if entry_result.first_remaining_date is not None:
                first_idx = _index_on_or_after(dates, entry_result.first_remaining_date)
            if entry_result.last_remaining_date is not None:
                last_idx = _index_on_or_before(dates, entry_result.last_remaining_date)

            atr20_at_last_entry = atr20[last_idx] if last_idx is not None else None
            stop_2n = two_n_stop(entry_result.entry_price, atr20_at_last_entry, is_short)

            # --- stop_chandelier_D: zapadka od RATCHET_START = najwcześniejsza
            # risk_date już zapisana dla tej pozycji PRZED D (§19.3 poprawka,
            # sesja główna 2026-09-26). Brak wcześniejszego wiersza -> dzień
            # zero -> RATCHET_START = D (wartość BEZ zapadki, jeden dzień). ---
            ratchet_start_date = _earliest_prior_risk_date(
                cur, rachunek, pos["instrument_id"], pos["settlement_currency"], as_of
            )
            if ratchet_start_date is None:
                ratchet_start_date = as_of
            ratchet_start_idx = _index_on_or_after(dates, ratchet_start_date)
            if ratchet_start_idx is None:
                ratchet_start_idx = d_idx

            stop_chandelier = ratchet_extreme(chand_series, ratchet_start_idx, d_idx, is_short)
            stop_eff, stop_source = stop_effective(stop_2n, stop_chandelier, is_short)

            # --- chandelier_from_entry: STARY wariant (zapadka od pierwszego
            # pozostałego lotu) — zachowany jako kolumna informacyjna, NIE
            # wchodzi do stop_effective. ---
            chandelier_from_entry = None
            if first_idx is not None:
                chandelier_from_entry = ratchet_extreme(chand_series, first_idx, d_idx, is_short)
            below_chandelier_from_entry = is_breached(close_d, chandelier_from_entry, is_short)

            hold = None
            below_hold = None
            if first_idx is not None:
                hold = chandelier_hold(highs, lows, first_idx, d_idx, atr22_d, is_short)
                below_hold = is_breached(close_d, hold, is_short)

            risk_native, below_stop = compute_risk_native(
                close_d, stop_eff, abs(qty), multiplier if not multiplier_missing else None, is_short
            )
            if multiplier_missing:
                risk_native = None
                note = (note + ";" if note else "") + "multiplier_missing"

            fx_rate, fx_rate_date = _fx_rate_on_or_before(cur, quote_currency, as_of)
            risk_pln = risk_native * fx_rate if (risk_native is not None and fx_rate is not None) else None

            row = PositionRiskRow(
                rachunek=rachunek,
                instrument_id=pos["instrument_id"],
                broker_ticker=broker_ticker,
                settlement_currency=pos["settlement_currency"],
                quote_currency=quote_currency,
                instrument_type=instrument_type,
                is_core=is_core,
                qty=qty,
                position_kind=position_kind,
                entry_price=entry_result.entry_price,
                close_d=close_d,
                atr20=atr20_d,
                atr22=atr22_d,
                sma200=sma200_d,
                stop_2n=stop_2n,
                stop_chandelier=stop_chandelier,
                stop_effective=stop_eff,
                stop_source=stop_source,
                chandelier_hold=hold,
                below_chandelier_hold=below_hold,
                chandelier_from_entry=chandelier_from_entry,
                below_chandelier_from_entry=below_chandelier_from_entry,
                regime=regime,
                warning=warning,
                risk_native=risk_native,
                below_stop=below_stop,
                fx_rate=fx_rate,
                fx_rate_date=fx_rate_date,
                risk_pln=risk_pln,
                multiplier_missing=multiplier_missing,
                note=note,
            )
            computed.append(row)

            # --- kapitał satelity: equity/etf, is_core=false, BEZ futures ---
            if instrument_type in ("equity", "etf") and not is_core and close_d is not None and fx_rate is not None:
                value_pln = close_d * qty * fx_rate
                capital_total += value_pln
                capital_by_rachunek[rachunek] = capital_by_rachunek.get(rachunek, Decimal(0)) + value_pln
                positions_count_by_rachunek[rachunek] = positions_count_by_rachunek.get(rachunek, 0) + 1
                summary.capital_satelite_positions_total += 1

        # --- druga faza: budżety poziom 1/3, agregaty raportu (satellite only) ---
        satellite_risk_total = Decimal(0)
        satellite_risk_zagraniczny = Decimal(0)
        satellite_value_zagraniczny = Decimal(0)
        satellite_value_zagraniczny_below_hold = Decimal(0)
        satellite_value_zagraniczny_below_stop = Decimal(0)
        stop_source_counts: dict[str, int] = {}
        ticker_risk_pln_for_themes: list[tuple[str, Decimal | None]] = []
        ticker_to_theme: dict[str, str] = {}

        for row in computed:
            if isinstance(row, dict):
                continue  # empty rows created via _empty_row helper (dicts), skip aggregation
            if row.stop_source:
                stop_source_counts[row.stop_source] = stop_source_counts.get(row.stop_source, 0) + 1
            if row.below_stop:
                summary.below_stop_tickers.append(row.broker_ticker)
            if row.regime:
                summary.regime_tickers.append(row.broker_ticker)
            if row.warning:
                summary.warning_tickers.append(row.broker_ticker)
            if row.below_chandelier_hold:
                summary.variant_b_tickers.append(row.broker_ticker)
            if row.below_chandelier_from_entry:
                summary.below_chandelier_from_entry_tickers.append(row.broker_ticker)
            if row.multiplier_missing:
                summary.multiplier_missing_tickers.append(row.broker_ticker)

            is_zagraniczny = row.rachunek.upper().startswith("ZAGRANICZNY")
            is_satellite_capital_eligible = row.instrument_type in ("equity", "etf") and not row.is_core

            if is_zagraniczny and row.below_stop:
                summary.below_stop_zagraniczny_tickers.append(row.broker_ticker)

            if is_satellite_capital_eligible and row.risk_pln is not None:
                satellite_risk_total += row.risk_pln
                if is_zagraniczny:
                    satellite_risk_zagraniczny += row.risk_pln

            if is_satellite_capital_eligible:
                ticker_risk_pln_for_themes.append((row.broker_ticker, row.risk_pln))
                pos_theme = instrument_theme_by_id.get(row.instrument_id)
                if pos_theme is not None:
                    ticker_to_theme[row.broker_ticker] = pos_theme

            if (
                is_satellite_capital_eligible
                and is_zagraniczny
                and row.close_d is not None
                and row.fx_rate is not None
            ):
                value_pln = row.close_d * row.qty * row.fx_rate
                satellite_value_zagraniczny += value_pln
                if row.below_chandelier_hold:
                    satellite_value_zagraniczny_below_hold += value_pln
                if row.below_stop:
                    satellite_value_zagraniczny_below_stop += value_pln

        summary.capital_satelite_positions_by_rachunek = positions_count_by_rachunek
        summary.stop_source_counts = stop_source_counts

        pct_total, breach3 = level3_check(satellite_risk_total, capital_total)
        summary.total_risk_pct_satellite_capital = pct_total
        summary.level3_breach = breach3
        pct_zagr, _ = level3_check(satellite_risk_zagraniczny, _sum_zagraniczny_capital(capital_by_rachunek))
        summary.total_risk_pct_zagraniczny_satellite_capital = pct_zagr

        if satellite_value_zagraniczny:
            summary.variant_b_zagraniczny_satellite_value_pct = (
                satellite_value_zagraniczny_below_hold / satellite_value_zagraniczny * Decimal(100)
            )
            summary.zagraniczny_satellite_value_pct_below_stop = (
                satellite_value_zagraniczny_below_stop / satellite_value_zagraniczny * Decimal(100)
            )

        # --- Poziom 2 (§19.2): suma ryzyka per temat jako % kapitału satelity,
        # flaga > 3% (instruments.theme, sql/seed_themes.sql — 53 nazwy). ---
        summary.theme_budgets = compute_theme_budgets(ticker_risk_pln_for_themes, ticker_to_theme, capital_total)

        for row in computed:
            if isinstance(row, dict):
                continue
            is_satellite_capital_eligible = row.instrument_type in ("equity", "etf") and not row.is_core
            if is_satellite_capital_eligible and row.risk_pln is not None and capital_total:
                pct1, breach1 = level1_check(row.risk_pln, capital_total)
                row.risk_pct_satellite_capital = pct1
                row.level1_breach = breach1
                if breach1:
                    summary.level1_breach_tickers.append(row.broker_ticker)

        # --- kontrolka nominału P4.2: FPGEZ26/FCDRZ26 (multiplier znany) ---
        for row in computed:
            if isinstance(row, dict):
                continue
            if row.instrument_type == "future" and not row.multiplier_missing and row.close_d is not None:
                nominal = abs(row.qty) * _multiplier_for(cur, row.instrument_id) * row.close_d
                summary.futures_nominal_sanity[row.broker_ticker] = bool(
                    nominal > 0 and Decimal("1e4") <= nominal <= Decimal("1e6")
                )

        # --- zapis do risk_daily ---
        for row in computed:
            if isinstance(row, dict):
                _write_empty_row(cur, summary.risk_date, row)
                continue
            _write_row(cur, summary.risk_date, row)

        conn.commit()

    summary.rows = [r for r in computed if not isinstance(r, dict)]
    return summary


def _sum_zagraniczny_capital(capital_by_rachunek: dict[str, Decimal]) -> Decimal:
    return sum(
        (v for k, v in capital_by_rachunek.items() if k.upper().startswith("ZAGRANICZNY")), Decimal(0)
    )


def _multiplier_for(cur: psycopg.Cursor, instrument_id: int) -> Decimal:
    cur.execute("SELECT multiplier FROM instruments WHERE id = %s", (instrument_id,))
    row = cur.fetchone()
    return row[0] if row and row[0] is not None else Decimal(0)


def _index_on_or_after(dates: list[date], target: date) -> int | None:
    for idx, d in enumerate(dates):
        if d >= target:
            return idx
    return None


def _index_on_or_before(dates: list[date], target: date) -> int | None:
    idx_found = None
    for idx, d in enumerate(dates):
        if d <= target:
            idx_found = idx
        else:
            break
    return idx_found


def _empty_row(
    rachunek: str,
    instrument_id: int,
    broker_ticker: str,
    settlement_currency: str,
    quote_currency: str | None,
    instrument_type: str,
    is_core: bool,
    qty: Decimal,
    position_kind: str,
    note: str,
    multiplier_missing: bool,
) -> dict[str, Any]:
    """Wiersz dla pozycji bez policzalnych stopów (brak ceny na D / brak bazy /
    nieobsługiwany typ instrumentu) — zwracany jako dict (nie PositionRiskRow),
    żeby druga faza (agregaty) mogła je jednoznacznie pominąć przez `isinstance`."""
    return {
        "rachunek": rachunek,
        "instrument_id": instrument_id,
        "broker_ticker": broker_ticker,
        "settlement_currency": settlement_currency,
        "quote_currency": quote_currency,
        "instrument_type": instrument_type,
        "is_core": is_core,
        "qty": qty,
        "position_kind": position_kind,
        "note": note,
        "multiplier_missing": multiplier_missing,
    }


def _write_row(cur: psycopg.Cursor, risk_date: date, row: PositionRiskRow) -> None:
    cur.execute(
        """
        INSERT INTO risk_daily (
            rachunek, instrument_id, risk_date, atr20, atr22, sma200,
            chandelier_stop, two_n_stop, risk_state,
            settlement_currency, quote_currency, qty, entry_price, close_d,
            stop_effective, stop_source, chandelier_hold, below_chandelier_hold,
            chandelier_from_entry, below_chandelier_from_entry,
            position_kind, risk_native, fx_rate, fx_rate_date, risk_pln,
            risk_pct_satellite_capital, level1_breach,
            regime, warning, multiplier_missing, note
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s
        )
        ON CONFLICT (rachunek, instrument_id, settlement_currency, risk_date) DO UPDATE SET
            atr20 = EXCLUDED.atr20,
            atr22 = EXCLUDED.atr22,
            sma200 = EXCLUDED.sma200,
            chandelier_stop = EXCLUDED.chandelier_stop,
            two_n_stop = EXCLUDED.two_n_stop,
            risk_state = EXCLUDED.risk_state,
            quote_currency = EXCLUDED.quote_currency,
            qty = EXCLUDED.qty,
            entry_price = EXCLUDED.entry_price,
            close_d = EXCLUDED.close_d,
            stop_effective = EXCLUDED.stop_effective,
            stop_source = EXCLUDED.stop_source,
            chandelier_hold = EXCLUDED.chandelier_hold,
            below_chandelier_hold = EXCLUDED.below_chandelier_hold,
            chandelier_from_entry = EXCLUDED.chandelier_from_entry,
            below_chandelier_from_entry = EXCLUDED.below_chandelier_from_entry,
            position_kind = EXCLUDED.position_kind,
            risk_native = EXCLUDED.risk_native,
            fx_rate = EXCLUDED.fx_rate,
            fx_rate_date = EXCLUDED.fx_rate_date,
            risk_pln = EXCLUDED.risk_pln,
            risk_pct_satellite_capital = EXCLUDED.risk_pct_satellite_capital,
            level1_breach = EXCLUDED.level1_breach,
            regime = EXCLUDED.regime,
            warning = EXCLUDED.warning,
            multiplier_missing = EXCLUDED.multiplier_missing,
            note = EXCLUDED.note,
            computed_at = now()
        """,
        (
            row.rachunek, row.instrument_id, risk_date, row.atr20, row.atr22, row.sma200,
            row.stop_chandelier, row.stop_2n, ("HIGH" if row.below_stop else ("NORMAL" if row.below_stop is not None else None)),
            row.settlement_currency, row.quote_currency, row.qty, row.entry_price, row.close_d,
            row.stop_effective, row.stop_source, row.chandelier_hold, row.below_chandelier_hold,
            row.chandelier_from_entry, row.below_chandelier_from_entry,
            row.position_kind, row.risk_native, row.fx_rate, row.fx_rate_date, row.risk_pln,
            row.risk_pct_satellite_capital, row.level1_breach,
            row.regime, row.warning, row.multiplier_missing, row.note,
        ),
    )


def _write_empty_row(cur: psycopg.Cursor, risk_date: date, row: dict[str, Any]) -> None:
    cur.execute(
        """
        INSERT INTO risk_daily (
            rachunek, instrument_id, risk_date,
            settlement_currency, quote_currency, qty, position_kind,
            multiplier_missing, note
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (rachunek, instrument_id, settlement_currency, risk_date) DO UPDATE SET
            quote_currency = EXCLUDED.quote_currency,
            qty = EXCLUDED.qty,
            position_kind = EXCLUDED.position_kind,
            multiplier_missing = EXCLUDED.multiplier_missing,
            note = EXCLUDED.note,
            computed_at = now()
        """,
        (
            row["rachunek"], row["instrument_id"], risk_date,
            row["settlement_currency"], row["quote_currency"], row["qty"], row["position_kind"],
            row["multiplier_missing"], row["note"],
        ),
    )

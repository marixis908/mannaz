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
zaczyna się od INICJALIZACJI SYSTEMU dla tej pozycji.
POPRAWKA (brief CC-S, S5, naprawa F4 — niedeterminizm): pierwotna wersja tej
poprawki liczyła RATCHET_START jako najwcześniejszą `risk_date` już zapisaną
dla pozycji w `risk_daily` sprzed D (`_earliest_prior_risk_date`, USUNIĘTA) —
zależność od tabeli POCHODNEJ, więc wynik na D zależał od tego, które dni już
przeliczono (niedeterministyczne) i nie resetował się po zamknięciu i ponownym
otwarciu pozycji. Teraz RATCHET_START(pozycja, D) =
`max(RATCHET_INIT_DATE, holding_period_start(D))`, gdzie `holding_period_start`
= najpóźniejsza data ≤ D, w której ilość pozycji przeszła z 0 na ≠0 (z tych
samych wierszy transakcji ≤ D co FIFO/`compute_weighted_entry_price`, splity
nie zmieniają znaku/zera — patrz `fifo.compute_position`/`holding_period_start`),
oraz `RATCHET_INIT_DATE = 2026-09-24` (data inicjalizacji systemu — [Z] jedyna
i pierwsza data w `risk_daily` sprzed tej zmiany). D < RATCHET_INIT_DATE
(system jeszcze nie działał) -> zapadka zaczyna się w D (jeden dzień, brak
zapadki) — patrz `resolve_ratchet_start`. Stop na D zależy WYŁĄCZNIE od cen
≤ D i transakcji ≤ D, zero zależności od `risk_daily`.
Stary wariant ("zapadka od pierwszego pozostałego lotu") jest ZACHOWANY jako
kolumna informacyjna `chandelier_from_entry` (+ `below_chandelier_from_entry`)
— NIE wchodzi do `stop_effective`.

CC-S, S2/S3 (naprawa F1/F3 — look-ahead na dzień D): pozycje wejściowe do
`run_risk` pochodzą z `fifo.positions_as_of(D)` (FIFO liczone PUNKTOWO na D z
`transactions`/`corporate_events`), NIE z `positions_fifo` (stan zawsze "na
dziś" — pozycja zamknięta po D by tam już nie istniała, split po D by już
przeskalował ilość). Ilość/cena wejścia z `positions_as_of(D)` żyją w warstwie
cen "na D" (bez wiedzy o zdarzeniach po D); ATR/stopy/Chandelier/close liczone
są (jak zawsze, patrz wyżej) w warstwie `*_split_adj` z `prices.py` (seria
Yahoo "dziś", uwzględnia WSZYSTKIE splity, także te po D). Żeby oba nie
mieszały się w jednym wierszu, ilość/cenę wejścia przelicza się czynnikiem
`f = layer_factor_after(events, D)` (iloczyn `ratio` zdarzeń split/reverse_split
instrumentu WYCENIANEGO z `event_date > D` — te same typy zdarzeń, których
używa `prices.reconstruct_raw`): `qty_adj = qty_D * f`, `entry_adj = entry_D / f`.
Dla kontraktów terminowych (wyceniane na bazie instrumentu, ale `qty`/`entry`
liczone na transakcjach SAMEGO kontraktu) `f = 1` — kontrakt nie ma własnych
splitów, jego ilość jest w jednostkach kontraktu, nie serii cenowej bazy.
Ta konwersja to WYŁĄCZNIE normalizacja warstwy cen (`f` nie niesie żadnej
informacji o przyszłości do wyniku na D) — wartość pozycji w PLN na D jest
z definicji niezależna od `f`: `qty_D * close_raw(D) == qty_adj * close_split_adj(D)`.
`risk_daily` zapisuje `qty`/`entry_price` już w warstwie split_adj (`qty_adj`/
`entry_adj`), spójnie z resztą wiersza.

P4.2 (kontrakty terminowe): ATR/stopy/REGIME liczone na instrumencie BAZOWYM
(`instruments.base_symbol` -> `instruments.yahoo_symbol` drugiego wiersza),
NIE na samym kontrakcie — kontrakt sam nie ma serii cenowej w Yahoo (patrz
`instruments_map.py`). `entry_price`/`qty`/pierwszy-ostatni-lot liczone
NATOMIAST na transakcjach KONTRAKTU (jednostki kontraktu GPW dla akcji są w tej
samej skali co akcja bazowa — `cena kontraktu` z tytułu transakcji brokera to
przybliżenie ceny bazy, nie osobna waluta/skala). `multiplier IS NULL`
(kontrakty wygasłe) -> ryzyko NULL, `multiplier_missing=True`, wyłączone z sum.

CC-R, krok K4 (§19.4 dokumentu projektowego — kontrakty w budżetach ryzyka):
kontrakty terminowe WCHODZĄ do budżetów poziomów 1-3 (`is_risk_budget_eligible`
= equity/etf spoza core LUB future) ryzykiem (`risk_pln`), analogicznie do
akcji/ETF satelitarnych — ale ich NOMINAŁ nigdy nie wchodzi do kapitału
satelity (pozycje w kapitale to tylko equity/etf spoza core). Zamiast
nominału do kapitału satelity wchodzi WARTOŚĆ RACHUNKU KONTRAKTOWY (decyzja
nadzorcy K3): środki ogółem łącznie z depozytem zablokowanym, liczone przez
`kontraktowy_account_value` z historii `transactions` (rachunek KONTRAKTOWY) —
wynik zmienny NIE jest doliczany osobno, bo broker rozlicza go już codziennie
w środkach (wiersze `depozyt_doplata`/`depozyt_zwrot`). `capital_by_rachunek`
i metryki ZAGRANICZNY (wagi pozycji/below_stop/below_chandelier_hold w %
wartości) pozostają BEZ ZMIAN — kontrakty tam nie wchodzą, tylko w budżety
i w łączny kapitał satelity (`capital_satelite_pln`).

B-17 (decyzja ownera 2026-10-04, M78): kapitał satelity w budżetach §19 =
NAV satelity z §21.6 — pozycje equity/etf spoza core + gotówka rachunków
AKCYJNY i ZAGRANICZNY (bez `NON_CASH_ROW_TYPES`) + wartość rachunku
KONTRAKTOWY. Jedna definicja: `run_risk` woła `satellite.nav_on` (ta sama
funkcja NAV co ocena satelity); NAV niepełny na D -> `IncompleteRiskDateError`
bez zapisu (pozycje `nav_incomplete:*`).

Brief CC-U (B-19), U2-U5 (T27 dokumentu projektowego — forward-fill max 1-2
dni, wyłącznie rynek faktycznie zamknięty, zawsze z flagą stale): pozycja bez
policzalnej ceny na D NIGDY nie wypada po cichu z kapitału ani z ryzyka
(decyzja nadzorcy 2026-09-27). `run_risk` najpierw sprawdza WSZYSTKIE pozycje
z `positions_as_of(D)` (przez `_resolve_position_price_coverage`, rdzeń
dzielony z `resolve_default_risk_date`) — jeśli którakolwiek jest
niekompletna (brak instrumentu bazowego, nieobsługiwany typ, cena na D wg
reguły T27 się nie rozstrzyga, brak FX, brak mnożnika kontraktu, albo
`stop_effective` niepoliczalny z powodu za krótkiej historii), `run_risk`
robi `conn.rollback()` i rzuca `IncompleteRiskDateError` PRZED jakimkolwiek
zapisem do `risk_daily` — zero wierszy D zmienionych. Reguła ceny na D
(3 przypadki: cena dokładnie na D / forward-fill max 2 sesje przy rynku
zamkniętym z flagą `price_is_stale` / niekompletne) jest czystą funkcją
`resolve_price_on_d` — dostęp do kalendarza sesyjnego (mapowanie giełda ->
kod kalendarza z `calendar_check.EXCHANGE_TO_CALENDAR_CODE`) jest wydzielony
do `CalendarFacts`/`_default_calendar_facts`, żeby testy syntetyczne mogły
podmienić kalendarz bez `exchange_calendars`/bazy. Dotychczasowe stany
"cichego wypadnięcia" (`_empty_row`/`_write_empty_row`, `note='no_price_on_d'`
itp.) są USUNIĘTE — każda pozycja albo jest w pełni policzona (jeden wiersz
`risk_daily`), albo cały przebieg D kończy się wyjątkiem."""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Hashable, NamedTuple

import exchange_calendars as xcals
import psycopg

from mannaz.calendar_check import EXCHANGE_TO_CALENDAR_CODE
from mannaz.fifo import KONTRAKTOWY_PREFIX, positions_as_of

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

# S5 (brief CC-S, F4): dzień inicjalizacji systemu ryzyka — [Z] jedyna i
# pierwsza data w `risk_daily` PRZED tą zmianą. Zapadka Chandeliera
# (RATCHET_START, patrz `resolve_ratchet_start`) nigdy nie zaczyna się wcześniej
# niż ta data — system nie liczył ryzyka przed nią, więc nie ma czego
# ratchetować wstecz.
RATCHET_INIT_DATE = date(2026, 9, 24)

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
# Budżety §19.2/§19.4 (brief CC-R) — kontrakty terminowe WCHODZĄ do budżetów
# poziomów 1-3 ryzykiem (risk_pln), obok akcji/ETF satelitarnych spoza core;
# NIE wchodzą do wag pozycji/kapitału/metryk ZAGRANICZNY (bez zmian, patrz
# `run_risk`).
# ---------------------------------------------------------------------------


def is_risk_budget_eligible(instrument_type: str, is_core: bool) -> bool:
    """§19.4: (equity/etf spoza core) LUB future — jedyne dwa typy pozycji,
    które wchodzą do budżetów ryzyka poziomów 1-3. Certyfikaty/inne typy oraz
    pozycje core (SPYI/V80A/V60A) — nie."""
    return (instrument_type in ("equity", "etf") and not is_core) or instrument_type == "future"


@dataclass
class Level1Name:
    """Poziom 1 per NAZWA (B-32) — wspólna prezentacja dla raportu i dashboardu."""

    name_key: Hashable
    label: str
    pct: Decimal | None
    breach: bool | None
    incomplete: bool
    # (broker_ticker, settlement_currency, risk_pct_satellite_capital wiersza)
    members: list[tuple[str, str | None, Decimal | None]]


def level1_by_name(items, labels: dict | None = None) -> list[Level1Name]:
    """`items` = krotki `(name_key, eligible, broker_ticker, settlement_currency,
    row_pct, name_pct, level1_breach)`. Tylko wiersze eligible. `name_key` None →
    każdy wiersz własną nazwą. Nazwa niepełna (którykolwiek wiersz z
    `name_pct` None) → pct/breach None, incomplete=True (B-19: nigdy „brak
    przekroczenia”). Sortowanie: pełne malejąco po pct, potem niepełne."""
    groups: dict[Any, list[tuple]] = {}
    for idx, it in enumerate(items):
        name_key, eligible, ticker, cur, row_pct, name_pct, breach = it
        if not eligible:
            continue
        gk = name_key if name_key is not None else ("__row__", idx)
        groups.setdefault(gk, []).append((name_key, ticker, cur, row_pct, name_pct, breach))
    out: list[Level1Name] = []
    for gk, rows in groups.items():
        name_key = rows[0][0]
        label = (labels or {}).get(name_key) if name_key is not None else None
        if label is None:
            label = rows[0][1]
        members = [(t, c, rp) for _, t, c, rp, _, _ in rows]
        incomplete = any(r[4] is None for r in rows)
        if incomplete:
            pct, breach = None, None
        else:
            pct = rows[0][4]
            breach = any(r[5] is True for r in rows)
        out.append(
            Level1Name(
                name_key=name_key if name_key is not None else gk,
                label=label,
                pct=pct,
                breach=breach,
                incomplete=incomplete,
                members=members,
            )
        )
    out.sort(key=lambda n: (n.pct is None, -(n.pct or Decimal(0))))
    return out


@dataclass
class RiskBudgetAggregateResult:
    satellite_risk_total: Decimal
    total_risk_pct: Decimal | None
    level3_breach: bool | None
    theme_budgets: dict[str, ThemeBudgetResult]
    level1_results: list[tuple[Decimal | None, bool | None]]
    level1_breach_tickers: list[str]
    name_risk_pcts: list[Decimal | None] = field(default_factory=list)


def aggregate_risk_budgets(
    items: list[tuple[str, str, bool, Decimal | None, str | None]],
    capital: Decimal | None,
    name_keys: list[Hashable | None] | None = None,
) -> RiskBudgetAggregateResult:
    """Czysta funkcja (brief CC-R (e)) — agreguje budżety poziomów 1-3 z listy
    `items` = (broker_ticker, instrument_type, is_core, risk_pln, theme), JEDEN
    element per policzalna pozycja z `run_risk` (kolejność = kolejność
    `computed`). Eligibility per element liczona wewnątrz
    (`is_risk_budget_eligible`) — pozwala wywołać tę funkcję z pełnej listy
    pozycji (equity/etf/future/inne) bez wstępnego filtrowania przez wołającego.
    `level1_results` wyrównane indeksem do `items` (żeby dało się przypisać
    z powrotem `risk_pct_satellite_capital`/`level1_breach` do wierszy bez
    grupowania po tickerze — ten sam ticker może wystąpić w kilku wierszach).

    Poziom 1 per NAZWĘ (B-30, decyzja ownera 6.3: nazwa = emitent):
    `name_keys` (wyrównane indeksem do `items`) = klucz nazwy wiersza (w
    `run_risk`: id instrumentu wyceny — equity/etf własny, future = bazowy).
    Suma `risk_pln` po kluczu wśród wierszy uprawnionych trafia do
    `level1_check`; `None` → każdy wiersz jest własną nazwą. B-19: jeśli
    którykolwiek uprawniony wiersz nazwy ma `risk_pln=None` albo `capital`
    jest pusty, pct nazwy i breach = None dla całej nazwy (jawnie niepełne,
    nigdy "brak przekroczenia"). `level1_results[i]` = (pct WIERSZA, breach
    NAZWY); `name_risk_pcts[i]` = pct nazwy (None dla nieuprawnionych)."""
    satellite_risk_total = Decimal(0)
    ticker_risk_pln_for_themes: list[tuple[str, Decimal | None]] = []
    ticker_to_theme: dict[str, str] = {}
    level1_results: list[tuple[Decimal | None, bool | None]] = []
    level1_breach_tickers: list[str] = []
    name_risk_pcts: list[Decimal | None] = []

    def _key(idx: int) -> Hashable:
        k = name_keys[idx] if name_keys is not None else None
        return ("row", idx) if k is None else ("name", k)

    name_sum: dict[Hashable, Decimal] = {}
    name_incomplete: set[Hashable] = set()
    for idx, (_t, instrument_type, is_core, risk_pln, _th) in enumerate(items):
        if not is_risk_budget_eligible(instrument_type, is_core):
            continue
        k = _key(idx)
        if risk_pln is None:
            name_incomplete.add(k)
        else:
            name_sum[k] = name_sum.get(k, Decimal(0)) + risk_pln
    name_result: dict[Hashable, tuple[Decimal | None, bool | None]] = {}
    for k in set(name_sum) | name_incomplete:
        if k in name_incomplete:
            name_result[k] = (None, None)
        else:
            name_result[k] = level1_check(name_sum[k], capital)

    for idx, (ticker, instrument_type, is_core, risk_pln, theme) in enumerate(items):
        eligible = is_risk_budget_eligible(instrument_type, is_core)

        if eligible and risk_pln is not None:
            satellite_risk_total += risk_pln

        if eligible:
            ticker_risk_pln_for_themes.append((ticker, risk_pln))
            if theme is not None:
                ticker_to_theme[ticker] = theme

        if eligible:
            name_pct, name_breach = name_result[_key(idx)]
            row_pct, _ = level1_check(risk_pln, capital)
            level1_results.append((row_pct, name_breach))
            name_risk_pcts.append(name_pct)
            if name_breach:
                level1_breach_tickers.append(ticker)
        else:
            level1_results.append((None, None))
            name_risk_pcts.append(None)

    total_risk_pct, level3_breach = level3_check(satellite_risk_total, capital)
    theme_budgets = compute_theme_budgets(ticker_risk_pln_for_themes, ticker_to_theme, capital)

    return RiskBudgetAggregateResult(
        satellite_risk_total=satellite_risk_total,
        total_risk_pct=total_risk_pct,
        level3_breach=level3_breach,
        theme_budgets=theme_budgets,
        level1_results=level1_results,
        level1_breach_tickers=level1_breach_tickers,
        name_risk_pcts=name_risk_pcts,
    )


# ---------------------------------------------------------------------------
# Rachunek KONTRAKTOWY — wartość rachunku (brief CC-R, §19.4 + decyzja
# nadzorcy K3): środki ogółem łącznie z depozytem zablokowanym. Nominał
# kupna/sprzedaży NIE jest przepływem środków (broker rozlicza wynik zmienny
# codziennie w środkach przez wiersze depozyt_doplata/depozyt_zwrot) — z
# wiersza kupno/sprzedaz liczy się WYŁĄCZNIE prowizja, zawsze jako koszt.
# ---------------------------------------------------------------------------

# Wiersze ręczne (opening_balances.py) NIE są przepływem gotówki: `amount`
# niesie koszt lotu dla FIFO, nie ruch środków (nadzorca, K5a/K4c).
NON_CASH_ROW_TYPES = frozenset({"bilans_otwarcia", "zamiana_przyjecie", "zamiana_wydanie"})


def kontraktowy_account_value(rows: list[dict[str, Any]], as_of: date) -> Decimal:
    """rows: [{'transaction_date','currency','row_type','amount','qty','price',
    'multiplier','broker_ticker'}] z rachunku KONTRAKTOWY, nieposortowane.
    Suma dla transaction_date <= as_of wg reguł: kupno/sprzedaz -> tylko
    prowizja jako koszt (nominał pomijany; mnożnik wyłącznie z
    `instruments.multiplier` — brak -> ValueError z nazwą serii, K4b);
    NON_CASH_ROW_TYPES -> pomijane; pozostałe row_type -> amount bez zmian.
    Wszystkie wiersze muszą być w PLN -> ValueError w przeciwnym razie."""
    total = Decimal(0)
    for row in rows:
        if row["transaction_date"] > as_of or row["row_type"] in NON_CASH_ROW_TYPES:
            continue
        if row["currency"] != "PLN":
            raise ValueError(
                f"KONTRAKTOWY: oczekiwano waluty PLN, otrzymano {row['currency']} dla "
                f"{row.get('broker_ticker')} @ {row['transaction_date']}"
            )
        if row["row_type"] in ("kupno", "sprzedaz"):
            multiplier = row["multiplier"]
            if multiplier is None:
                raise ValueError(
                    f"KONTRAKTOWY: brak mnoznika (instruments.multiplier IS NULL) dla serii "
                    f"{row.get('broker_ticker')} @ {row['transaction_date']} — uzupelnij ze "
                    "specyfikacji GPW (sql/007_futures_multipliers.sql), bez zgadywania."
                )
            commission = abs(row["amount"]) - row["qty"] * row["price"] * multiplier
            total -= commission
        else:
            total += row["amount"]
    return total


def check_kontraktowy_coverage(n_rows: int, max_date: date | None, has_open_futures: bool, as_of: date) -> None:
    """Kontrola pokrycia historii KONTRAKTOWY do D (brief CC-R (b)) — nigdy
    cichego zera. `n_rows`/`max_date` liczone na wierszach z
    transaction_date <= as_of (patrz `_kontraktowy_rows`). RuntimeError gdy:
    brak historii KONTRAKTOWY do D w ogóle, albo (przy otwartych kontraktach)
    historia nie sięga D — broker księguje rozliczenie każdej sesji, więc brak
    wiersza na D przy otwartych pozycjach oznacza niekompletne dane."""
    if n_rows == 0:
        raise RuntimeError(
            f"KONTRAKTOWY: brak historii transakcji do D={as_of} — nie da sie ustalic "
            "wartosci rachunku kontraktowego (nigdy cicho 0)."
        )
    if has_open_futures and (max_date is None or max_date < as_of):
        raise RuntimeError(
            f"KONTRAKTOWY: historia nie obejmuje D={as_of} (ostatni wiersz: {max_date}) "
            "przy otwartych kontraktach terminowych — broker ksieguje rozliczenie kazdej "
            "sesji, brak wiersza na D oznacza niekompletne dane."
        )


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
    fx_missing: bool = False  # brief CC-W, B-41: pozostały lot bez kursu FX


def convert_entry_rows_to_quote(
    rows: list[dict[str, Any]],
    settlement_currency: str | None,
    quote_currency: str | None,
    fx_lookup: Callable[[str, date], Decimal | None],
) -> list[dict[str, Any]]:
    """Brief CC-W, B-41: transactions.price jest w walucie ROZLICZENIA, a ATR/close
    w walucie notowania. Dla settlement != quote cena każdego wiersza jest
    przeliczana na walutę notowania kursem NBP z dnia transakcji (fx_lookup =
    ostatni fixing <= data; PLN -> 1): price * fx(settlement, d) / fx(quote, d).
    Brak któregokolwiek kursu -> price None i `fx_missing: True` (nigdy kurs z
    innego dnia ani brak przeliczenia). Wiersze z price None bez zmian.
    settlement == quote -> zwraca `rows` bez zmian (ten sam obiekt)."""
    if settlement_currency == quote_currency:
        return rows
    out: list[dict[str, Any]] = []
    for r in rows:
        price = r.get("price")
        if price is None:
            out.append(r)
            continue
        fx_s = fx_lookup(settlement_currency, r["date"])
        fx_q = fx_lookup(quote_currency, r["date"])
        new = dict(r)
        if fx_s is None or fx_q is None or fx_q == 0:
            new["price"] = None
            new["fx_missing"] = True
        else:
            new["price"] = price * fx_s / fx_q
        out.append(new)
    return out


def compute_weighted_entry_price(
    rows: list[dict[str, Any]], events: list[dict[str, Any]], allow_short: bool
) -> WeightedEntryResult:
    """rows: [{'date','type':'kupno'|'sprzedaz','qty':Decimal,'price':Decimal|None}],
    nieposortowane. events: [{'date','ratio'}], tylko split/reverse_split z
    ratio IS NOT NULL (jak w fifo._resolve_position, brief CC-B24 C2). Algorytm identyczny z `fifo.compute_position` (splity PRZED
    FIFO, netowanie najpierw względem przeciwnego znaku), ale:
      - koszt lotu = `price` transakcji (nie `amount/qty`),
      - agregacja kosztu obejmuje loty OBU znaków (potrzebne dla wejścia
        krótkiej pozycji kontraktu, KONTRAKTOWY/allow_short=True),
      - first/last data = z KOŃCOWEGO stanu kolejki lotów (pozostałe loty),
        nie z licznika aktualizowanego przy każdej transakcji kupna.
    Brief CC-W, B-41: opcjonalny klucz wiersza `fx_missing` (True = brak kursu
    przeliczenia na walutę notowania, patrz `convert_entry_rows_to_quote`) jest
    przenoszony na utworzony lot; jeśli któryś POZOSTAŁY lot go ma ->
    entry_price=None i fx_missing=True (qty/daty liczone normalnie). Lot w pełni
    zamknięty późniejszą sprzedażą flagi nie wnosi. Brak klucza = zachowanie bez zmian."""
    rows_sorted = sorted(rows, key=lambda r: r["date"])
    events_sorted = sorted(events, key=lambda e: e["date"])

    lots: deque[list] = deque()  # [qty, price, entry_date, fx_missing]
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

        fxm = bool(r.get("fx_missing", False))
        if remaining > 0:
            lots.append([remaining, price if price is not None else Decimal(0), r["date"], fxm])
        elif remaining < 0:
            if allow_short:
                lots.append([remaining, price if price is not None else Decimal(0), r["date"], fxm])
            else:
                lots.append([remaining, Decimal(0), r["date"], False])
        j += 1

    if not lots:
        return WeightedEntryResult(entry_price=None, qty=Decimal(0), first_remaining_date=None, last_remaining_date=None)

    qty_total = sum((lot[0] for lot in lots), Decimal(0))
    cost_total = sum((lot[0] * lot[1] for lot in lots), Decimal(0))
    first_remaining = min(lot[2] for lot in lots)
    last_remaining = max(lot[2] for lot in lots)
    entry_price = (cost_total / qty_total) if qty_total != 0 else None
    fx_missing = any(lot[3] for lot in lots)
    if fx_missing:
        entry_price = None

    return WeightedEntryResult(
        entry_price=entry_price,
        qty=qty_total,
        first_remaining_date=first_remaining,
        last_remaining_date=last_remaining,
        fx_missing=fx_missing,
    )


# ---------------------------------------------------------------------------
# Warstwa cen "na D" -> `*_split_adj` (brief CC-S, S3, naprawa F3) — patrz
# akapit CC-S w docstringu modułu.
# ---------------------------------------------------------------------------


def layer_factor_after(events: list[dict[str, Any]], as_of: date) -> Decimal:
    """S3 (brief CC-S): czynnik przejścia ilości/ceny z warstwy "na D" (FIFO
    punktowy, `positions_as_of`/`compute_weighted_entry_price`) do warstwy
    `*_split_adj` (`prices.py`) — iloczyn `ratio` zdarzeń z `event_date > D`.
    events: [{'date','ratio'}] — DOKŁADNIE ten sam zestaw, którego używa
    `prices.reconstruct_raw`/`cumulative_ratio_after` (`corporate_events`,
    `ratio IS NOT NULL`, `event_type IN ('split', 'reverse_split')` — od
    brief CC-B24 C2 (B-46) ten sam zestaw co w FIFO i
    `_instrument_split_events`). Kierunek identyczny jak `cumulative_ratio_after`: Yahoo
    dzieli historyczne ceny przez `ratio` przy każdym kolejnym splicie, więc
    `qty_adj = qty_D * f`, `entry_adj = entry_D / f`."""
    factor = Decimal(1)
    for e in events:
        if e["date"] > as_of:
            factor *= e["ratio"]
    return factor


# ---------------------------------------------------------------------------
# Zapadka Chandeliera — RATCHET_START deterministyczny (brief CC-S, S5,
# naprawa F4). Patrz akapit POPRAWKA (S5) w docstringu modułu.
# ---------------------------------------------------------------------------


def holding_period_start(rows: list[dict[str, Any]], events: list[dict[str, Any]]) -> date | None:
    """Najpóźniejsza data, w której ILOŚĆ pozycji przeszła z 0 na ≠0 — tylko
    wśród `rows`/`events` PRZEKAZANYCH przez wywołującego (żadnego filtra
    `as_of` tutaj — ten sam wzorzec co `fifo.compute_position`: wywołujący
    przekazuje już przefiltrowane dane `<= D`). `None` gdy pozycja jest
    domknięta (qty końcowe == 0) — nie powinno się zdarzyć dla pozycji z
    `qty != 0` na D, ale funkcja jest obronna.

    rows: [{'date','type':'kupno'|'sprzedaz','qty'}] (te same dane co FIFO —
    `_entry_transactions`/`compute_weighted_entry_price`, pole 'amount'/'price'
    nieużywane tutaj). events: [{'date','ratio'}] (zdarzenia split/reverse_split
    ze znanym ratio, jak w FIFO — filtr typu w SQL, brief CC-B24 C2/B-46).

    Algorytm: chronologiczny przebieg zdarzeń+transakcji (identyczna kolejność
    scalania jak `compute_position`), śledzący TYLKO sumę ilości (nie
    poszczególne loty — dla sumy netowanie FIFO jest tożsamościowo równe
    zwykłej sumie podpisanej, splity mnożą całość przez `ratio` i NIGDY nie
    zmieniają znaku/zera — stąd nie trzeba replikować pełnej struktury lotów)."""
    rows_sorted = sorted(rows, key=lambda r: r["date"])
    events_sorted = sorted(events, key=lambda e: e["date"])

    qty = Decimal(0)
    last_zero_to_nonzero: date | None = None
    i, j = 0, 0
    while i < len(events_sorted) or j < len(rows_sorted):
        take_event = i < len(events_sorted) and (
            j >= len(rows_sorted) or events_sorted[i]["date"] <= rows_sorted[j]["date"]
        )
        if take_event:
            qty *= events_sorted[i]["ratio"]
            i += 1
            continue

        r = rows_sorted[j]
        prev_qty = qty
        signed = r["qty"] if r["type"] == "kupno" else -r["qty"]
        qty += signed
        if prev_qty == 0 and qty != 0:
            last_zero_to_nonzero = r["date"]
        j += 1

    if qty == 0:
        return None
    return last_zero_to_nonzero


def resolve_ratchet_start(rows: list[dict[str, Any]], events: list[dict[str, Any]], as_of: date) -> date:
    """RATCHET_START(pozycja, D) (brief CC-S, S5) = `max(RATCHET_INIT_DATE,
    holding_period_start)`. `rows`/`events` MUSZĄ już być przefiltrowane do
    `<= as_of` przez wywołującego (ten sam zestaw, co dla FIFO na D).
    `as_of < RATCHET_INIT_DATE` -> system jeszcze nie działał -> zapadka
    zaczyna się w `as_of` (jeden dzień, brak zapadki). Brak
    `holding_period_start` (obronnie, nie powinno wystąpić dla pozycji z
    qty != 0 na D) -> również `as_of`."""
    if as_of < RATCHET_INIT_DATE:
        return as_of
    start = holding_period_start(rows, events)
    if start is None:
        return as_of
    return max(RATCHET_INIT_DATE, start)


# ---------------------------------------------------------------------------
# T27 (dokument projektowy, §T27 — forward-fill max 1-2 dni, wyłącznie rynek
# faktycznie zamknięty, zawsze flaga stale) — reguła ceny na D, brief CC-U U2.
# Czysta funkcja: kalendarz już rozstrzygnięty przez wywołującego (patrz
# `CalendarFacts`/`_default_calendar_facts` niżej) — zero zależności od
# `exchange_calendars`/bazy, żeby testy syntetyczne (U5) mogły podmienić
# kalendarz przez wstrzyknięcie gotowych wartości `is_session_on_d`/
# `sessions_before`.
# ---------------------------------------------------------------------------


def resolve_price_on_d(
    price_dates: list[date],
    as_of: date,
    sessions_before: list[date],
    is_session_on_d: bool | None,
) -> tuple[str, date | None, str | None]:
    """U2 (brief CC-U): reguła ceny na D wg T27. Zwraca `(status,
    price_date_used, reason)`:

    - `status='ok'`     — cena dokładnie z D (`as_of in price_dates`);
      `price_date_used=as_of`, `reason=None`.
    - `status='stale'`  — brak ceny na D, rynek ZAMKNIĘTY w D
      (`is_session_on_d is False`), ostatnia cena `<= D` nie starsza niż
      2 sesje giełdy (patrz niżej) -> forward-fill, `price_date_used` =
      data tej ceny, `reason=None`. Nigdy cena z datą > D (parametr
      `price_dates` musi być już przefiltrowany do `<= as_of` przez
      wywołującego, tak jak `_price_series`).
    - `status='incomplete'` — każdy inny przypadek, `price_date_used=None`,
      `reason` in:
        'no_calendar'       — `is_session_on_d is None` (brak mapowania
                              giełdy, exchange NULL albo spoza
                              `EXCHANGE_TO_CALENDAR_CODE`).
        'market_open_no_price' — D jest sesją, ale brak ceny na D.
        'no_price_at_all'   — rynek zamknięty w D, ale brak JAKIEJKOLWIEK
                              ceny `<= D` (nie ma czego forward-fillować).
        'price_too_old'     — ostatnia cena `<= D` jest starsza niż
                              dozwolone.

    Definicja "nie starsza niż 2 sesje" [S, decyzja 2026-09-27]: niech
    `s1` = ostatnia sesja giełdy `< D` (`sessions_before[0]`), `s2` =
    przedostatnia (`sessions_before[1]`) — dozwolone, gdy
    `price_date_used >= s2` (brakuje najwyżej jednej sesji). `sessions_before`
    krótsze niż 2 elementy (np. początek historii kalendarza) -> nie da się
    potwierdzić warunku -> `incomplete`/`insufficient_calendar_history`
    (obronnie, nie powinno wystąpić w praktyce)."""
    if as_of in price_dates:
        return "ok", as_of, None

    if is_session_on_d is None:
        return "incomplete", None, "no_calendar"

    if is_session_on_d:
        return "incomplete", None, "market_open_no_price"

    candidates = [d for d in price_dates if d <= as_of]
    if not candidates:
        return "incomplete", None, "no_price_at_all"
    last_price_date = max(candidates)

    if len(sessions_before) < 2:
        return "incomplete", None, "insufficient_calendar_history"

    s2 = sorted(sessions_before, reverse=True)[1]
    if last_price_date >= s2:
        return "stale", last_price_date, None
    return "incomplete", None, "price_too_old"


@dataclass
class CalendarFacts:
    """Warstwa dostępu do kalendarza (brief CC-U, U2) — `resolve_price_on_d`
    jest czysta i przyjmuje te fakty już gotowe; testy syntetyczne wstrzykują
    własne `CalendarFacts` (przez podmianę `calendar_facts_fn` w
    `run_risk`/`resolve_default_risk_date`) bez dotykania
    `exchange_calendars`."""

    is_session_on_d: bool | None
    sessions_before: list[date]  # do 2 najbliższych sesji ŚCIŚLE przed D, malejąco: [s1, s2]


def _default_calendar_facts(calendar_code: str | None, as_of: date) -> CalendarFacts:
    """Domyślna implementacja `CalendarFacts` przez `exchange_calendars` —
    jedyne miejsce w tym module, które dotyka tej biblioteki. `calendar_code`
    None (exchange NULL albo spoza `EXCHANGE_TO_CALENDAR_CODE`) -> brak
    kalendarza, `is_session_on_d=None`."""
    if calendar_code is None:
        return CalendarFacts(is_session_on_d=None, sessions_before=[])
    cal = xcals.get_calendar(calendar_code)
    is_session_on_d = bool(cal.is_session(as_of.isoformat()))
    # okno 30 dni kalendarzowych wstecz wystarcza na najdluzsze przerwy
    # swiateczne (Boze Narodzenie/Nowy Rok) miedzy dwiema sesjami.
    window_start = as_of - timedelta(days=30)
    sessions = [ts.date() for ts in cal.sessions_in_range(window_start.isoformat(), as_of.isoformat())]
    sessions_before = sorted((s for s in sessions if s < as_of), reverse=True)[:2]
    return CalendarFacts(is_session_on_d=is_session_on_d, sessions_before=sessions_before)


# ---------------------------------------------------------------------------
# Domyślne D (brief CC-U, U3) — najpóźniejsza data <= dziś, dla której reguła
# T27 (U2) daje komplet dla WSZYSTKICH pozycji z `positions_as_of` tej daty
# (zero przypadków 'incomplete' — stopy NIE są sprawdzane, patrz
# `_resolve_position_price_coverage`). Zastępuje starą `resolve_default_date`
# (brief P4.1 — "any_null" per satelitę), USUNIĘTĄ: nowa reguła sprawdza
# WSZYSTKIE pozycje (nie tylko satelitę) przez ten sam rdzeń co `run_risk`.
# ---------------------------------------------------------------------------


def resolve_default_risk_date_pure(candidates_with_completeness: list[tuple[date, bool]]) -> date | None:
    """U3: czysta część `resolve_default_risk_date` — `candidates_with_completeness`:
    [(price_date, is_complete)], nieposortowane, jeden wpis per kandydat.
    Zwraca najpóźniejszą datę z `is_complete=True`, albo `None` gdy żadna."""
    for d, is_complete in sorted(candidates_with_completeness, key=lambda t: t[0], reverse=True):
        if is_complete:
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
    price_is_stale: bool
    price_date_used: date | None
    risk_pct_satellite_capital: Decimal | None = None
    level1_breach: bool | None = None
    name_key: int | None = None
    name_risk_pct: Decimal | None = None
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
    theme_budgets: dict[str, ThemeBudgetResult] = field(default_factory=dict)
    # B-17 (M78): kapitał satelity = NAV z §21.6 (nominał futures nie wchodzi,
    # §19.4; do 2026-10-04 bez gotówki, CC-R); poniżej rozkład informacyjny
    # (pozycje + gotówka AKCYJNY/ZAGRANICZNY + KONTRAKTOWY = capital_satelite_pln).
    capital_satelite_positions_pln: Decimal = Decimal(0)
    capital_satelite_cash_pln: Decimal = Decimal(0)
    kontraktowy_account_value_pln: Decimal = Decimal(0)
    capital_satelite_pln: Decimal = Decimal(0)
    # CC-U (U4, T27): pozycje z cena forward-filled (rynek zamkniety w D,
    # ostatnia cena <=D w granicach 2 sesji) — nigdy nie sa cicho pomijane,
    # ale sa oznaczone i zliczone osobno.
    stale_positions_count: int = 0
    stale_capital_pln: Decimal = Decimal(0)
    stale_capital_pct: Decimal | None = None
    stale_tickers: list[str] = field(default_factory=list)


def _open_positions_as_of(cur: psycopg.Cursor, as_of: date) -> list[dict[str, Any]]:
    """S2/S3 (brief CC-S, F1 fix): pozycje na dzień D przez `fifo.positions_as_of`
    (FIFO liczone punktowo na D, bez look-ahead) — zamiast czytania bieżącego
    stanu `positions_fifo`. Atrybuty instrumentu JOIN z `instruments`, te same
    klucze dict co dawne `_open_positions` (reszta `run_risk` bez zmian).
    `qty` tutaj to ilość w warstwie "na D" (jeszcze nie w `*_split_adj`) —
    patrz `layer_factor_after` w `run_risk`."""
    positions = positions_as_of(cur, as_of)
    out: list[dict[str, Any]] = []
    for p in positions:
        cur.execute(
            """
            SELECT broker_ticker, instrument_type, is_core, base_symbol, multiplier,
                   yahoo_symbol, currency, theme, exchange
            FROM instruments WHERE id = %s
            """,
            (p["instrument_id"],),
        )
        row = cur.fetchone()
        if row is None:
            continue  # obronnie — FK gwarantuje istnienie, nie powinno wystapic
        (
            broker_ticker, instrument_type, is_core, base_symbol, multiplier,
            yahoo_symbol, quote_currency, theme, exchange,
        ) = row
        out.append(
            {
                "rachunek": p["rachunek"],
                "instrument_id": p["instrument_id"],
                "settlement_currency": p["currency"],
                "qty": p["qty"],
                "broker_ticker": broker_ticker,
                "instrument_type": instrument_type,
                "is_core": is_core,
                "base_symbol": base_symbol,
                "multiplier": multiplier,
                "yahoo_symbol": yahoo_symbol,
                "quote_currency": quote_currency,
                "theme": theme,
                # exchange (brief CC-U, U2): gieldy INSTRUMENTU WYCENY — dla
                # equity/etf to ta pozycja, dla future doklejane osobno z
                # instrumentu bazowego (_base_instrument), patrz
                # `_resolve_position_price_coverage`.
                "exchange": exchange,
            }
        )
    out.sort(key=lambda d: (d["rachunek"], d["broker_ticker"]))
    return out


def _base_instrument(cur: psycopg.Cursor, base_symbol: str | None) -> dict[str, Any] | None:
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
          AND event_type IN ('split', 'reverse_split')
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


def _instrument_layer_events(cur: psycopg.Cursor, instrument_id: int) -> list[dict[str, Any]]:
    """S3 (brief CC-S, F3): zdarzenia dla `layer_factor_after` — DOKŁADNIE ten
    sam zestaw, którego używa `prices.reconstruct_raw`/`cumulative_ratio_after`
    (`ratio IS NOT NULL`, `event_type IN ('split', 'reverse_split')`) — od
    brief CC-B24 C2 (B-46) ten sam zestaw co `_instrument_split_events` i
    FIFO (`share_exchange` ignorowany wszędzie). Bez filtra `as_of` w SQL —
    `layer_factor_after` sam filtruje `event_date > as_of`, bo tu chodzi
    właśnie o zdarzenia PO D."""
    cur.execute(
        """
        SELECT event_date, ratio FROM corporate_events
        WHERE instrument_id = %s AND ratio IS NOT NULL
          AND event_type IN ('split', 'reverse_split')
        ORDER BY event_date
        """,
        (instrument_id,),
    )
    return [{"date": d, "ratio": r} for d, r in cur.fetchall()]


def _kontraktowy_rows(cur: psycopg.Cursor, as_of: date) -> list[dict[str, Any]]:
    """Wszystkie wiersze `transactions` rachunku KONTRAKTOWY do dnia D
    włącznie (`transaction_date <= as_of`), z `instruments.multiplier`
    doklejonym LEFT JOIN (NULL -> błąd w `kontraktowy_account_value`;
    mnożniki serii ze specyfikacji GPW, sql/007). Wejście dla `kontraktowy_account_value` /
    `check_kontraktowy_coverage`."""
    cur.execute(
        """
        SELECT t.transaction_date, t.currency, t.row_type, t.amount, t.qty, t.price,
               i.multiplier, i.broker_ticker
        FROM transactions t
        LEFT JOIN instruments i ON i.id = t.instrument_id
        WHERE t.rachunek LIKE %s AND t.transaction_date <= %s
        ORDER BY t.transaction_date, t.id
        """,
        (f"{KONTRAKTOWY_PREFIX}%", as_of),
    )
    cols = ("transaction_date", "currency", "row_type", "amount", "qty", "price", "multiplier", "broker_ticker")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


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


# ---------------------------------------------------------------------------
# U2 (brief CC-U) — "nigdy cicho z kapitału/ryzyka": rdzeń rozstrzygania ceny
# na D (T27) + FX/mnożnik/instrument bazowy, dzielony przez `run_risk` i
# `resolve_default_risk_date` (U3, bez sprawdzania stopów).
# ---------------------------------------------------------------------------


class IncompleteRiskItem(NamedTuple):
    """Jeden wpis niekompletności D (brief CC-U, U2) — `.items` wyjątku
    `IncompleteRiskDateError`. `price_instrument`: yahoo_symbol instrumentu
    WYCENY (dla equity/etf: ten sam instrument; dla future: baza — albo
    `base_symbol` samego kontraktu, gdy instrument bazowy nie istnieje w
    ogóle w `instruments`). `exchange`: giełda instrumentu wyceny (może być
    None — sam brak mapowania kalendarza to jedna z przyczyn)."""

    broker_ticker: str
    price_instrument: str | None
    exchange: str | None
    reason: str


class IncompleteRiskDateError(RuntimeError):
    """U2 (brief CC-U): D niekompletne wg reguły T27 (cena) albo innej
    ścieżki dawnego "cichego wypadnięcia" (brak instrumentu bazowego,
    nieobsługiwany typ, brak FX, brak mnożnika kontraktu, `stop_effective`
    niepoliczalny) — `run_risk` zbiera WSZYSTKIE takie pozycje (nie
    przerywa na pierwszej), robi `conn.rollback()` i rzuca ten wyjątek
    PRZED jakimkolwiek zapisem do `risk_daily` (przed DELETE, S4). `.items`:
    lista `IncompleteRiskItem`, jedna na pozycję niekompletną."""

    def __init__(self, as_of: date, items: list[IncompleteRiskItem]):
        self.as_of = as_of
        self.items = items
        reasons = "; ".join(f"{it.broker_ticker}[{it.reason}]" for it in items)
        super().__init__(f"D={as_of}: {len(items)} pozycji niekompletnych (T27, brief CC-U) — {reasons}")


@dataclass
class PositionPriceCoverage:
    """Wynik `_resolve_position_price_coverage` dla pozycji KOMPLETNEJ —
    wystarcza do policzenia ATR/stopów/ryzyka w `run_risk` bez ponownego
    odpytywania instrumentu/ceny/FX."""

    price_instrument_id: int
    quote_currency: str
    yahoo_symbol: str | None
    exchange: str | None
    multiplier: Decimal
    layer_factor: Decimal
    series: dict[str, list]
    price_is_stale: bool
    price_date_used: date
    fx_rate: Decimal
    fx_rate_date: date | None


def _resolve_position_price_coverage(
    cur: psycopg.Cursor,
    pos: dict[str, Any],
    as_of: date,
    calendar_facts_fn: Callable[[str | None, date], CalendarFacts] = _default_calendar_facts,
) -> tuple[PositionPriceCoverage | None, IncompleteRiskItem | None]:
    """U2/U3 (brief CC-U): rdzeń "czy ta pozycja ma komplet na D" — dzielony
    przez `run_risk` (pełne liczenie ryzyka) i `resolve_default_risk_date`
    (U3, tylko kompletność — stopy NIE są tu sprawdzane, bo wymagają
    ATR/Chandelier policzonych z serii, a U3 ma być tani). Sprawdza (w tej
    kolejności): instrument bazowy (future) / typ instrumentu, mnożnik
    (future), cenę na D wg T27 (`resolve_price_on_d`), FX <= D. Zwraca
    `(coverage, None)` przy komplecie albo `(None, item)` przy
    niekompletności — DOKŁADNIE jedno z dwóch."""
    broker_ticker = pos["broker_ticker"]
    instrument_type = pos["instrument_type"]

    if instrument_type == "future":
        base = _base_instrument(cur, pos["base_symbol"])
        if base is None:
            return None, IncompleteRiskItem(broker_ticker, pos["base_symbol"], None, "base_instrument_not_found")
        price_instrument_id = base["id"]
        quote_currency = base["currency"]
        yahoo_symbol = base["yahoo_symbol"]
        exchange = base["exchange"]
        multiplier = pos["multiplier"]
        if multiplier is None:
            return None, IncompleteRiskItem(broker_ticker, yahoo_symbol, exchange, "multiplier_missing")
        # Kontrakt terminowy nie ma wlasnych splitow — jego ilosc jest w
        # jednostkach kontraktu, nie serii cenowej bazy (S3).
        layer_factor = Decimal(1)
    elif instrument_type in ("equity", "etf"):
        price_instrument_id = pos["instrument_id"]
        quote_currency = pos["quote_currency"]
        yahoo_symbol = pos["yahoo_symbol"]
        exchange = pos["exchange"]
        multiplier = Decimal(1)
        layer_events = _instrument_layer_events(cur, price_instrument_id)
        layer_factor = layer_factor_after(layer_events, as_of)
    else:
        return None, IncompleteRiskItem(broker_ticker, None, None, "unsupported_instrument_type")

    series = _price_series(cur, price_instrument_id, as_of)
    dates = series["dates"]
    calendar_code = EXCHANGE_TO_CALENDAR_CODE.get(exchange) if exchange else None
    facts = calendar_facts_fn(calendar_code, as_of)
    status, price_date_used, reason = resolve_price_on_d(
        price_dates=dates, as_of=as_of, sessions_before=facts.sessions_before, is_session_on_d=facts.is_session_on_d
    )
    if status == "incomplete":
        return None, IncompleteRiskItem(broker_ticker, yahoo_symbol, exchange, reason)

    fx_rate, fx_rate_date = _fx_rate_on_or_before(cur, quote_currency, as_of)
    if fx_rate is None:
        return None, IncompleteRiskItem(broker_ticker, yahoo_symbol, exchange, "fx_rate_missing")

    coverage = PositionPriceCoverage(
        price_instrument_id=price_instrument_id,
        quote_currency=quote_currency,
        yahoo_symbol=yahoo_symbol,
        exchange=exchange,
        multiplier=multiplier,
        layer_factor=layer_factor,
        series=series,
        price_is_stale=(status == "stale"),
        price_date_used=price_date_used,  # type: ignore[arg-type]  # status != 'incomplete' -> zawsze data
        fx_rate=fx_rate,
        fx_rate_date=fx_rate_date,
    )
    return coverage, None


def resolve_default_risk_date(
    conn: psycopg.Connection,
    max_candidates: int = 30,
    calendar_facts_fn: Callable[[str | None, date], CalendarFacts] = _default_calendar_facts,
    today: date | None = None,
) -> date | None:
    """U3 (brief CC-U): najpóźniejsza data `<= dziś`, dla której reguła T27
    (U2) daje komplet dla WSZYSTKICH pozycji z `positions_as_of` tej daty
    (stopy NIE są sprawdzane — `_resolve_position_price_coverage` pomija
    ATR/Chandelier, U3 ma być tani). Kandydaci: `max_candidates`
    najpóźniejszych dat z `prices_daily` (malejąco, ograniczone rozsądnie);
    zatrzymuje się na pierwszej kompletnej (kandydaci już malejący, więc to
    od razu najpóźniejsza). Brak kompletu w oknie -> `None` (`run_risk`
    wtedy rzuca `RuntimeError` jak dotychczas).

    B-36 (M77): kandydat musi być kompletny JEDNOCZEŚNIE w cenach (T27) i w
    rozliczeniu rachunku KONTRAKTOWY (`check_kontraktowy_coverage`, ta sama
    reguła co bramka w `run_risk`; istotne gdy na D są otwarte kontrakty).
    Jawnie podane D (`run_risk(as_of=D)`) nie przechodzi przez ten wybór i
    nadal kończy się błędem przy niekompletności.

    `today` (brief CC-C, C6): opcjonalny, domyślnie `date.today()` jak
    dotychczas — wstrzykiwalny, żeby cykl (`cycle.py`) i testy mogły podać
    dzień przebiegu bez podmiany zegara systemowego."""
    if today is None:
        today = date.today()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT price_date FROM prices_daily WHERE price_date <= %s ORDER BY price_date DESC LIMIT %s",
            (today, max_candidates),
        )
        candidates = [row[0] for row in cur.fetchall()]

        completeness: list[tuple[date, bool]] = []
        for d in candidates:
            positions = _open_positions_as_of(cur, d)
            complete = True
            for pos in positions:
                _, incomplete = _resolve_position_price_coverage(cur, pos, d, calendar_facts_fn)
                if incomplete is not None:
                    complete = False
                    break
            if complete:
                # B-36 (M77): ta sama reguła co bramka w `run_risk` — jedno
                # wywołanie `check_kontraktowy_coverage`, bez kopii warunku.
                kontraktowy_rows = _kontraktowy_rows(cur, d)
                has_open_futures = any(p["instrument_type"] == "future" for p in positions)
                max_kontraktowy_date = max((r["transaction_date"] for r in kontraktowy_rows), default=None)
                try:
                    check_kontraktowy_coverage(len(kontraktowy_rows), max_kontraktowy_date, has_open_futures, d)
                except RuntimeError:
                    complete = False
            completeness.append((d, complete))
            if complete:
                break

    return resolve_default_risk_date_pure(completeness)


def run_risk(conn: psycopg.Connection, as_of: date | None = None, commit: bool = True) -> RiskSummary:
    """`commit=False` (brief CC-S, S4): nie wywołuje `conn.commit()` — do
    testów bazodanowych z rollbackiem (zero trwałych zmian)."""
    if as_of is None:
        as_of = resolve_default_risk_date(conn)
        if as_of is None:
            raise RuntimeError(
                "brak daty kompletnej jednoczesnie w cenach (T27) i w rozliczeniu KONTRAKTOWY (M77) "
                "w oknie kandydatow - nie da sie ustalic D"
            )

    summary = RiskSummary(risk_date=as_of)

    with conn.cursor() as cur:
        positions = _open_positions_as_of(cur, as_of)
        instrument_theme_by_id: dict[int, str] = {
            p["instrument_id"]: p["theme"] for p in positions if p["theme"] is not None
        }

        capital_by_rachunek: dict[str, Decimal] = {}
        positions_count_by_rachunek: dict[str, int] = {}

        computed: list[PositionRiskRow] = []
        incomplete_items: list[IncompleteRiskItem] = []

        for pos in positions:
            broker_ticker = pos["broker_ticker"]
            rachunek = pos["rachunek"]
            instrument_type = pos["instrument_type"]
            is_core = bool(pos["is_core"])
            # qty_d: ilość w warstwie "na D" (positions_as_of, FIFO bez
            # look-ahead) — przeliczana do warstwy split_adj niżej, jak tylko
            # znany jest instrument wyceniany (S3, brief CC-S, F1+F3).
            qty_d = pos["qty"]
            is_short = qty_d < 0  # znak niezmienny pod skalowaniem dodatnim czynnikiem f
            position_kind = "short" if is_short else "long"
            note = ""

            # --- U2 (brief CC-U): instrument bazowy/typ/cena na D (T27)/FX —
            # WSZYSTKIE dotychczasowe ścieżki cichego wypadnięcia teraz trafiają
            # do incomplete_items (zbieramy WSZYSTKIE pozycje, nie przerywamy
            # na pierwszej niekompletnej — patrz sprawdzenie po tej pętli). ---
            coverage, incomplete = _resolve_position_price_coverage(cur, pos, as_of)
            if incomplete is not None:
                incomplete_items.append(incomplete)
                continue

            quote_currency = coverage.quote_currency
            yahoo_symbol = coverage.yahoo_symbol
            multiplier = coverage.multiplier
            layer_factor = coverage.layer_factor
            price_instrument_id = coverage.price_instrument_id

            # qty: warstwa split_adj (S3) — spójna z ATR/stopami/close niżej.
            # Wartość PLN na D jest z definicji niezależna od layer_factor
            # (qty_d * close_raw(D) == qty * close_split_adj(D)).
            qty = qty_d * layer_factor

            series = coverage.series
            dates = series["dates"]

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
            # Brief CC-W, B-41: price transakcji jest w walucie rozliczenia, ATR/close
            # w walucie notowania — loty settlement != quote przeliczane kursem NBP
            # z dnia transakcji (ostatni fixing <= data); qty/daty bez zmian.
            txn_rows = convert_entry_rows_to_quote(
                txn_rows,
                pos["settlement_currency"],
                quote_currency,
                lambda c, d: _fx_rate_on_or_before(cur, c, d)[0],
            )
            entry_result = compute_weighted_entry_price(txn_rows, events, allow_short=allow_short)
            if entry_result.fx_missing:
                note = (note + ";" if note else "") + "entry_fx_missing"

            # Porównanie w TEJ SAMEJ warstwie ("na D") — entry_result.qty i
            # qty_d pochodzą oba z FIFO punktowego na D (S3, brief CC-S).
            if entry_result.qty != qty_d:
                note = (note + ";" if note else "") + "entry_qty_mismatch_vs_positions_as_of"

            # entry_price z compute_weighted_entry_price jest w warstwie "na D"
            # (te same events <= as_of co dla qty_d) — przeliczenie do
            # split_adj tym samym layer_factor co qty (S3).
            entry_price_adj = (
                entry_result.entry_price / layer_factor if entry_result.entry_price is not None else None
            )

            first_idx = None
            last_idx = None
            if entry_result.first_remaining_date is not None:
                first_idx = _index_on_or_after(dates, entry_result.first_remaining_date)
            if entry_result.last_remaining_date is not None:
                last_idx = _index_on_or_before(dates, entry_result.last_remaining_date)

            atr20_at_last_entry = atr20[last_idx] if last_idx is not None else None
            stop_2n = two_n_stop(entry_price_adj, atr20_at_last_entry, is_short)

            # --- stop_chandelier_D: zapadka od RATCHET_START deterministyczny
            # (brief CC-S, S5, naprawa F4) — tylko z cen/transakcji <= D, zero
            # zależności od risk_daily. txn_rows/events już przefiltrowane do
            # <= as_of (_entry_transactions/_instrument_split_events). ---
            ratchet_start_date = resolve_ratchet_start(txn_rows, events, as_of)
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

            risk_native, below_stop = compute_risk_native(close_d, stop_eff, abs(qty), multiplier, is_short)
            if risk_native is None:
                # U2 (brief CC-U): risk_pln niepoliczalne — stop_effective None
                # (brak stopu z powodu za krotkiej historii ATR/Chandelier/2N),
                # bo close_d juz na pewno nie-None (mamy komplet ceny z T27).
                incomplete_items.append(
                    IncompleteRiskItem(broker_ticker, yahoo_symbol, coverage.exchange, "stop_unavailable")
                )
                continue

            fx_rate = coverage.fx_rate
            fx_rate_date = coverage.fx_rate_date
            risk_pln = risk_native * fx_rate

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
                entry_price=entry_price_adj,
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
                multiplier_missing=False,
                price_is_stale=coverage.price_is_stale,
                price_date_used=coverage.price_date_used,
                name_key=coverage.price_instrument_id,
                note=note,
            )
            computed.append(row)

            # --- U4 (brief CC-U): pozycje z ceną forward-filled (T27) — nigdy
            # cicho pominięte, ale oznaczone i zliczone osobno. ---
            if row.price_is_stale:
                summary.stale_tickers.append(broker_ticker)
                summary.stale_positions_count += 1

            # --- kapitał satelity: equity/etf, is_core=false, BEZ futures
            # (§19.4 CC-R: nominał futures NIE wchodzi do kapitału satelity —
            # zamiast niego wchodzi wartość rachunku KONTRAKTOWY, patrz niżej). ---
            if instrument_type in ("equity", "etf") and not is_core:
                value_pln = close_d * qty * fx_rate
                capital_by_rachunek[rachunek] = capital_by_rachunek.get(rachunek, Decimal(0)) + value_pln
                positions_count_by_rachunek[rachunek] = positions_count_by_rachunek.get(rachunek, 0) + 1
                summary.capital_satelite_positions_total += 1
                if row.price_is_stale:
                    summary.stale_capital_pln += value_pln

        # --- U2 (brief CC-U): D niekompletne — ZERO wierszy risk_daily
        # zmienionych, rollback PRZED jakimkolwiek zapisem (przed DELETE, S4).
        # Zbieramy WSZYSTKIE pozycje niekompletne (nie przerywamy na
        # pierwszej) — patrz `incomplete_items.append(...)` w pętli wyżej. ---
        # --- B-17 (M78): kapitał satelity = NAV z §21.6 (jedna definicja,
        # `satellite.nav_on`). NAV niepełny na D -> te same ścieżki co pozycje
        # niekompletne (rollback przed zapisem + wyjątek z listą przyczyn). ---
        # import lokalny: satellite importuje risk na poziomie modułu (cykl)
        from mannaz.satellite import nav_on

        nav = nav_on(cur, as_of)
        if not nav.complete:
            for component, reason in nav.missing:
                incomplete_items.append(
                    IncompleteRiskItem(component, None, None, f"nav_incomplete:{reason}")
                )
        if incomplete_items:
            conn.rollback()
            raise IncompleteRiskDateError(as_of, incomplete_items)

        # --- kapitał satelity B-17: NAV (pozycje equity/etf spoza core, BEZ
        # nominału futures + gotówka AKCYJNY/ZAGRANICZNY + wartość rachunku
        # KONTRAKTOWY, §19.4). Kontrola pokrycia KONTRAKTOWY zostaje jako
        # bramka — nigdy cichego zera. Rozkład tylko informacyjny, z NavResult. ---
        kontraktowy_rows = _kontraktowy_rows(cur, as_of)
        has_open_futures = any(p["instrument_type"] == "future" for p in positions)
        max_kontraktowy_date = max((r["transaction_date"] for r in kontraktowy_rows), default=None)
        check_kontraktowy_coverage(len(kontraktowy_rows), max_kontraktowy_date, has_open_futures, as_of)

        capital_satelite_pln = Decimal(str(nav.value))
        kontraktowy_value_pln = Decimal(str(nav.by_account_pln.get(("KONTRAKTOWY", "PLN"), 0.0)))
        cash_pln = Decimal(str(sum(
            amount * nav.rates[cur_code]
            for (grp, cur_code), amount in nav.cash_native.items()
            if grp != "KONTRAKTOWY" and amount != 0  # 0 bez kursu nie trafia do nav.rates
        )))
        summary.kontraktowy_account_value_pln = kontraktowy_value_pln
        summary.capital_satelite_cash_pln = cash_pln
        summary.capital_satelite_positions_pln = capital_satelite_pln - cash_pln - kontraktowy_value_pln
        summary.capital_satelite_pln = capital_satelite_pln

        # --- U4 (brief CC-U): udział pozycji stale w kapitale satelity. ---
        if capital_satelite_pln:
            summary.stale_capital_pct = summary.stale_capital_pln / capital_satelite_pln * Decimal(100)

        # --- druga faza: budżety poziom 1/3 (satelita + futures, §19.4) i
        # agregaty raportu ZAGRANICZNY (bez zmian, kontrakty tam nie wchodzą). ---
        satellite_risk_zagraniczny = Decimal(0)
        satellite_value_zagraniczny = Decimal(0)
        satellite_value_zagraniczny_below_hold = Decimal(0)
        satellite_value_zagraniczny_below_stop = Decimal(0)
        stop_source_counts: dict[str, int] = {}
        budget_items: list[tuple[str, str, bool, Decimal | None, str | None]] = []

        for row in computed:
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
            # Wagi pozycji/kapitał/metryki ZAGRANICZNY: BEZ ZMIAN wobec P4.1 —
            # kontrakty tam NIE wchodzą (brief CC-R (c)/(d)), inaczej niż
            # budżety poziom 1-3 (patrz budget_items/aggregate_risk_budgets).
            is_satellite_capital_eligible = row.instrument_type in ("equity", "etf") and not row.is_core

            if is_zagraniczny and row.below_stop:
                summary.below_stop_zagraniczny_tickers.append(row.broker_ticker)

            if is_satellite_capital_eligible and is_zagraniczny and row.risk_pln is not None:
                satellite_risk_zagraniczny += row.risk_pln

            budget_items.append(
                (
                    row.broker_ticker,
                    row.instrument_type,
                    row.is_core,
                    row.risk_pln,
                    instrument_theme_by_id.get(row.instrument_id),
                )
            )

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

        # --- Poziomy 1/3 (§19.2/§19.4, kapitał = capital_satelite_pln) + poziom
        # 2 (tematy) — czysta agregacja (brief CC-R (e)), testowalna bez bazy. ---
        budget_result = aggregate_risk_budgets(
            budget_items, capital_satelite_pln, name_keys=[row.name_key for row in computed]
        )
        summary.total_risk_pct_satellite_capital = budget_result.total_risk_pct
        summary.level3_breach = budget_result.level3_breach
        summary.theme_budgets = budget_result.theme_budgets
        summary.level1_breach_tickers = budget_result.level1_breach_tickers

        pct_zagr, _ = level3_check(satellite_risk_zagraniczny, _sum_zagraniczny_capital(capital_by_rachunek))
        summary.total_risk_pct_zagraniczny_satellite_capital = pct_zagr

        if satellite_value_zagraniczny:
            summary.variant_b_zagraniczny_satellite_value_pct = (
                satellite_value_zagraniczny_below_hold / satellite_value_zagraniczny * Decimal(100)
            )
            summary.zagraniczny_satellite_value_pct_below_stop = (
                satellite_value_zagraniczny_below_stop / satellite_value_zagraniczny * Decimal(100)
            )

        # level1_results wyrównane indeksem do budget_items (patrz docstring
        # aggregate_risk_budgets) -> ten sam porządek co computed (brief CC-U:
        # `computed` zawiera już WYŁĄCZNIE PositionRiskRow — pozycje
        # niekompletne nigdy tu nie trafiają, run_risk rzucił wyżej).
        for row, (pct1, breach1), name_pct in zip(
            computed, budget_result.level1_results, budget_result.name_risk_pcts
        ):
            row.risk_pct_satellite_capital = pct1
            row.level1_breach = breach1
            row.name_risk_pct = name_pct

        # --- kontrolka nominału P4.2: FPGEZ26/FCDRZ26 (multiplier znany) ---
        for row in computed:
            if row.instrument_type == "future" and not row.multiplier_missing and row.close_d is not None:
                nominal = abs(row.qty) * _multiplier_for(cur, row.instrument_id) * row.close_d
                summary.futures_nominal_sanity[row.broker_ticker] = bool(
                    nominal > 0 and Decimal("1e4") <= nominal <= Decimal("1e6")
                )

        # --- zapis do risk_daily jako CAŁOŚĆ (brief CC-S, S4, naprawa F6):
        # DELETE wszystkich wierszy D, potem zwykły INSERT każdego wiersza
        # przebiegu (bez ON CONFLICT — po DELETE kluczy nie ma czym
        # kolidować), jeden `computed_at` dla całego przebiegu, na końcu
        # asercja liczby wierszy == liczba pozycji z positions_as_of(D). Brief
        # CC-U (U2): w tym miejscu KAŻDA pozycja z `positions_as_of` ma już
        # policzalny wiersz w `computed` — gdyby którakolwiek była
        # niekompletna, `run_risk` rzuciłby `IncompleteRiskDateError` wyżej,
        # PRZED tym DELETE (zero wierszy D zmienionych w takim przypadku).
        # Przy niezgodności tej asercji: wyjątek PRZED commitem (rollback,
        # brak trwałego zapisu). ---
        cur.execute("DELETE FROM risk_daily WHERE risk_date = %s", (summary.risk_date,))
        computed_at = datetime.now(timezone.utc)
        for row in computed:
            _write_row(cur, summary.risk_date, row, computed_at)

        cur.execute("SELECT count(*) FROM risk_daily WHERE risk_date = %s", (summary.risk_date,))
        actual_count = cur.fetchone()[0]
        if actual_count != len(positions):
            conn.rollback()
            raise RuntimeError(
                f"risk_daily: D={summary.risk_date} liczba wierszy po zapisie ({actual_count}) "
                f"!= liczba pozycji z positions_as_of ({len(positions)}) — rollback (S4, brief CC-S)."
            )

        if commit:
            conn.commit()

    summary.rows = computed
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


def _write_row(cur: psycopg.Cursor, risk_date: date, row: PositionRiskRow, computed_at: datetime) -> None:
    """S4 (brief CC-S): zwykły INSERT (bez ON CONFLICT — `run_risk` robi
    DELETE FROM risk_daily WHERE risk_date=D PRZED zapisem całego przebiegu,
    więc w obrębie D nie ma z czym kolidować). `computed_at` jeden dla
    całego przebiegu, przekazany jawnie (kolumna ma DEFAULT now(), ale wtedy
    każdy wiersz dostałby inną wartość). U4 (brief CC-U): dokłada
    `price_is_stale`/`price_date_used` (T27, migracja sql/008)."""
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
            regime, warning, multiplier_missing, price_is_stale, price_date_used,
            name_key, name_risk_pct,
            note, computed_at
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s,
            %s, %s
        )
        """,
        (
            row.rachunek, row.instrument_id, risk_date, row.atr20, row.atr22, row.sma200,
            row.stop_chandelier, row.stop_2n, ("HIGH" if row.below_stop else ("NORMAL" if row.below_stop is not None else None)),
            row.settlement_currency, row.quote_currency, row.qty, row.entry_price, row.close_d,
            row.stop_effective, row.stop_source, row.chandelier_hold, row.below_chandelier_hold,
            row.chandelier_from_entry, row.below_chandelier_from_entry,
            row.position_kind, row.risk_native, row.fx_rate, row.fx_rate_date, row.risk_pln,
            row.risk_pct_satellite_capital, row.level1_breach,
            row.regime, row.warning, row.multiplier_missing, row.price_is_stale, row.price_date_used,
            row.name_key, row.name_risk_pct,
            row.note, computed_at,
        ),
    )

"""Ocena satelity jako calosci (brief CC-OS2, E3; dokument projektowy §21.6,
M78-M89, T39-T44, §19.4, T27).

Uklad:
  (A) rdzen czysty (bez bazy i sieci): klasyfikator przeplywow, przeplyw
      zewnetrzny, NAV z gotowych wejsc, TWR, delta_w, obsuniecia, mandat,
      alerty, werdykt, regresja tygodniowa;
  (B) warstwa ladowania `load_inputs` (tylko SELECT) + skladanie wejsc
      do NAV (`compute_daily`, czyste przy gotowym `Inputs`);
  (C) `run_satellite` — orkiestracja, zapis satellite.json / satellite.md.

Konwencje: kwoty w PLN (float), kursy FX = NBP A, ostatni z data <= D (M1,
M77); ceny = prices_daily.close_split_adj x czynnik warstwy
`risk.layer_factor_after` (S3); regula ceny na D = `risk.resolve_price_on_d`
(T27). Zadnego zapisu do bazy; zadnych per-pozycyjnych ilosci/kosztow w
wynikach (tylko agregaty i tickery z przyczyna niekompletnosci).
"""

from __future__ import annotations

import bisect
import calendar
import csv
import json
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

from mannaz import risk as _risk
from mannaz.calendar_check import EXCHANGE_TO_CALENDAR_CODE

# ---------------------------------------------------------------------------
# Stale
# ---------------------------------------------------------------------------

L_MAX = 0.35  # T41
L_REL = 0.20  # T43
ALERT_WARN = 0.75  # T44
ALERT_REARM = 0.50  # T44
TIE_PCT = 0.005  # T40
GATE_TOLERANCE = 0.0001  # T39: 0,01%
SATELLITE_GROUPS = ("AKCYJNY", "ZAGRANICZNY", "KONTRAKTOWY")
KONTRAKTOWY_GROUP = "KONTRAKTOWY"
CALENDAR_CODES_AXIS = ("XWAR", "XNYS", "XETR")
BENCHMARK_SYMBOLS = {"sp500": "SXR8.DE", "spyi": "SPYI.DE"}
BENCHMARK_LABELS = {"sp500": "S&P 500 TR (SXR8.DE)", "spyi": "SPYI (SPYI.DE)"}
PRICE_TAIL = 45  # ile ostatnich cen <= D przekazywac do resolve_price_on_d

# ---------------------------------------------------------------------------
# (A) Klasyfikator przeplywow (M80)
# ---------------------------------------------------------------------------


class UnknownRowTypeError(ValueError):
    """row_type spoza slownika FLOW_CLASS — nigdy klasa domyslna (M80)."""


FLOW_CLASS: dict[str, str] = {
    "przelew_do_domu_maklerskiego": "zewnetrzny",
    "przelew_zewnetrzny": "zewnetrzny",
    "bilans_otwarcia": "zewnetrzny",
    "przelew_wewnetrzny": "wewnetrzny",
    "kupno": "handel",
    "sprzedaz": "handel",
    "zamiana_akcji": "handel",
    "zamiana_przyjecie": "handel",
    "zamiana_wydanie": "handel",
    "wykup_certyfikatow": "handel",
    "depozyt_doplata": "handel",
    "depozyt_zwrot": "handel",
    "dywidenda_gpw": "dochod",
    "dywidenda_netto": "dochod",
    "dywidenda_brutto": "dochod",
    "podatek_dywidenda": "koszt",
    "oplata_rachunek": "koszt",
    "oplata_prowadzenie_rachunku": "koszt",
    "oplata_przechowanie": "koszt",
    "oplata_transakcyjna": "koszt",
    "prowizja_wygasniecie": "koszt",
}


def classify(row_type: str, is_core: bool = False) -> str:
    """Klasa wiersza wzgledem granicy satelity. `is_core` (instrument rdzenia)
    nadpisuje klase: kazdy taki wiersz przechodzi przez granice -> zewnetrzny.
    Nieznany row_type -> UnknownRowTypeError (takze gdy is_core)."""
    if row_type not in FLOW_CLASS:
        raise UnknownRowTypeError(row_type)
    if is_core:
        return "zewnetrzny"
    return FLOW_CLASS[row_type]


def account_group(rachunek: str) -> str:
    """Grupa rachunku = pierwszy token nazwy (AKCYJNY/ZAGRANICZNY/KONTRAKTOWY);
    numery rachunkow nigdy nie trafiaja do wynikow."""
    parts = rachunek.upper().split()
    return parts[0] if parts else rachunek.upper()


@dataclass(frozen=True)
class TxRow:
    date: date
    rachunek: str
    currency: str
    row_type: str
    amount: float
    instrument_id: int | None = None
    qty: float | None = None
    price: float | None = None
    is_core: bool = False
    instrument_type: str | None = None


@dataclass
class FlowResult:
    value: float  # PLN, znak + = do satelity
    missing: list[tuple[str, str]] = field(default_factory=list)  # (opis, przyczyna)


FxFn = Callable[[str, date], "float | None"]
NonCashValueFn = Callable[[TxRow], "float | None"]


def external_flow_pln(
    rows: Sequence[TxRow], fx: FxFn, noncash_value: NonCashValueFn | None = None
) -> FlowResult:
    """Suma przeplywow zewnetrznych F w PLN dla `rows` (znak + = do satelity).
    Wiersze gotowkowe klasy 'zewnetrzny': amount x FX(waluta, data wiersza).
    `bilans_otwarcia` (niegotowkowy, instrument satelity): +qty x cena x FX
    (wartosc rynkowa z dnia transferu) przez `noncash_value`; brak wartosci ->
    wpis w `missing` (dzien niepelny). Wiersze niegotowkowe instrumentow rdzenia
    albo kontraktow nie wnosza nic do NAV satelity -> 0. Klasy inne niz
    'zewnetrzny' nie sa przeplywem. Nieznany row_type -> UnknownRowTypeError."""
    total = 0.0
    missing: list[tuple[str, str]] = []
    for r in rows:
        cls = classify(r.row_type, r.is_core)
        if cls != "zewnetrzny":
            continue
        if r.row_type in _risk.NON_CASH_ROW_TYPES:
            if r.row_type != "bilans_otwarcia" or r.is_core or r.instrument_type == "future":
                continue
            v = noncash_value(r) if noncash_value is not None else None
            if v is None:
                missing.append((f"bilans_otwarcia:{r.instrument_id}", "brak ceny"))
            else:
                total += v
            continue
        rate = fx(r.currency, r.date)
        if rate is None:
            missing.append((f"{r.row_type}:{r.currency}", "brak FX"))
            continue
        total += r.amount * rate
    return FlowResult(total, missing)


# ---------------------------------------------------------------------------
# (A) NAV z gotowych wejsc (M78)
# ---------------------------------------------------------------------------


@dataclass
class PositionInput:
    rachunek: str
    currency: str  # waluta rozliczenia pozycji
    ticker: str
    qty: float  # warstwa "na D"
    layer_factor: float  # risk.layer_factor_after (ilosc -> warstwa split_adj)
    price: float | None  # close_split_adj (x pence), None gdy brak
    price_status: str  # 'ok' | 'stale' | 'incomplete'
    price_reason: str | None
    quote_fx: float | None  # kurs waluty notowania -> PLN
    settle_fx: float | None  # kurs waluty rozliczenia -> PLN
    is_core: bool = False


@dataclass
class NavResult:
    value: float | None  # None gdy niepelny (nigdy cichego zera)
    partial_value: float  # suma tego, co wycenione (informacyjnie)
    complete: bool
    missing: list[tuple[str, str]]  # (ticker/skladnik, przyczyna)
    stale: list[str]
    by_account_pln: dict[tuple[str, str], float]  # (grupa, waluta rozliczenia) -> PLN
    by_account_native: dict[tuple[str, str], float]  # j.w. w walucie rozliczenia
    core_by_account_native: dict[tuple[str, str], float]  # rdzen (do bramki M78)
    core_complete: bool = True
    # skladniki bramki M78 (w walucie rozliczenia): papiery satelity / srodki
    positions_native: dict[tuple[str, str], float] = field(default_factory=dict)
    cash_native: dict[tuple[str, str], float] = field(default_factory=dict)
    cash_missing: list[tuple[str, str]] = field(default_factory=list)  # (grupa, waluta) bez wyceny srodkow
    rates: dict[str, float] = field(default_factory=dict)  # waluta -> kurs PLN uzyty w dniu


def nav_for_day(
    positions: Sequence[PositionInput],
    cash: dict[tuple[str, str], float],
    fx: dict[str, float | None],
    kontraktowy_pln: float | None = None,
    kontraktowy_error: str | None = None,
) -> NavResult:
    """NAV satelity w PLN na jeden dzien. `positions`: pozycje otwarte
    (equity/etf; bez kontraktow, §19.4); pozycje rdzenia sa wyceniane osobno
    (tylko bramka M78) i nie wchodza do NAV. `cash`: (grupa rachunku, waluta)
    -> srodki w walucie, bez NON_CASH_ROW_TYPES. `fx`: waluta -> kurs NBP A <= D
    (PLN = 1). `kontraktowy_pln`: `risk.kontraktowy_account_value` w PLN;
    `kontraktowy_error`: przyczyna, gdy nie da sie policzyc. Pozycja bez ceny
    (status incomplete / brak FX) -> NAV niepelny, value=None."""
    missing: list[tuple[str, str]] = []
    stale: list[str] = []
    by_pln: dict[tuple[str, str], float] = {}
    by_native: dict[tuple[str, str], float] = {}
    core_native: dict[tuple[str, str], float] = {}
    pos_native: dict[tuple[str, str], float] = {}
    cash_native: dict[tuple[str, str], float] = {}
    cash_missing: list[tuple[str, str]] = []
    rates: dict[str, float] = {"PLN": 1.0, **{c: r for c, r in fx.items() if r is not None}}
    core_complete = True
    partial = 0.0

    for p in positions:
        key = (account_group(p.rachunek), p.currency)
        if p.price is None or p.price_status == "incomplete" or p.quote_fx is None:
            reason = p.price_reason or ("brak FX" if p.quote_fx is None else "brak ceny")
            if p.is_core:
                core_complete = False
            else:
                missing.append((p.ticker, reason))
            continue
        value_pln = p.qty * p.layer_factor * p.price * p.quote_fx
        native = value_pln / p.settle_fx if p.settle_fx else None
        if p.is_core:
            if native is None:
                core_complete = False
            else:
                core_native[key] = core_native.get(key, 0.0) + native
            continue
        if p.price_status == "stale":
            stale.append(p.ticker)
        by_pln[key] = by_pln.get(key, 0.0) + value_pln
        if native is not None:
            by_native[key] = by_native.get(key, 0.0) + native
            pos_native[key] = pos_native.get(key, 0.0) + native
        partial += value_pln

    for (grp, cur), amount in cash.items():
        rate = 1.0 if cur == "PLN" else fx.get(cur)
        key = (grp, cur)
        cash_native[key] = cash_native.get(key, 0.0) + amount  # srodki w walucie nie zaleza od FX
        if rate is None:
            if amount != 0:
                missing.append((f"gotowka {grp}/{cur}", "brak FX"))
            continue
        by_pln[key] = by_pln.get(key, 0.0) + amount * rate
        by_native[key] = by_native.get(key, 0.0) + amount
        partial += amount * rate

    if kontraktowy_error is not None:
        missing.append((KONTRAKTOWY_GROUP, kontraktowy_error))
        cash_missing.append((KONTRAKTOWY_GROUP, "PLN"))
    elif kontraktowy_pln is not None:
        key = (KONTRAKTOWY_GROUP, "PLN")
        by_pln[key] = by_pln.get(key, 0.0) + kontraktowy_pln
        by_native[key] = by_native.get(key, 0.0) + kontraktowy_pln
        cash_native[key] = cash_native.get(key, 0.0) + kontraktowy_pln
        partial += kontraktowy_pln

    complete = not missing
    return NavResult(
        value=partial if complete else None,
        partial_value=partial,
        complete=complete,
        missing=missing,
        stale=stale,
        by_account_pln=by_pln,
        by_account_native=by_native,
        core_by_account_native=core_native,
        core_complete=core_complete,
        positions_native=pos_native,
        cash_native=cash_native,
        cash_missing=cash_missing,
        rates=rates,
    )


# ---------------------------------------------------------------------------
# (A) TWR (M82)
# ---------------------------------------------------------------------------


def twr(nav: Sequence[float | None], flows: Sequence[float | None]) -> tuple[list[float | None], list[float | None]]:
    """`r_D = (NAV_D - F_D)/NAV_{D-1} - 1` (przeplyw na koniec dnia). Zwraca
    (r, I_sat). r_D = None, gdy NAV_D, NAV_{D-1} albo F_D niedostepne (dzien
    niepelny) lub NAV_{D-1} <= 0. Indeks I_sat = 1 na pierwszym dniu z NAV > 0
    (po przeplywach tego dnia) i przerywa sie (None do konca) na pierwszym dniu
    z r = None — brak cichego ciagniecia przez luke. Dzien niepelny przed
    startem tylko odsuwa start."""
    n = len(nav)
    r: list[float | None] = [None] * n
    idx: list[float | None] = [None] * n
    for i in range(1, n):
        v, prev, f = nav[i], nav[i - 1], flows[i]
        if v is None or prev is None or f is None or prev <= 0:
            continue
        r[i] = (v - f) / prev - 1.0
    started = False
    broken = False
    cur = 1.0
    for i in range(n):
        if broken:
            continue
        if not started:
            if nav[i] is not None and nav[i] > 0:
                started = True
                idx[i] = 1.0
            continue
        if r[i] is None:
            broken = True
            continue
        cur *= 1.0 + r[i]
        idx[i] = cur
    return r, idx


def window_index(r: Sequence[float | None], a: int, t: int) -> list[float] | None:
    """Indeks TWR przeskalowany na 1 w dniu `a` dla dni a..t; None, gdy
    jakikolwiek r w (a, t] jest None (okno obejmuje dzien niepelny)."""
    out = [1.0]
    for i in range(a + 1, t + 1):
        if r[i] is None:
            return None
        out.append(out[-1] * (1.0 + r[i]))
    return out


# ---------------------------------------------------------------------------
# (A) Nadwyzka majatku (M80)
# ---------------------------------------------------------------------------


@dataclass
class DeltaWResult:
    value: float | None
    infeasible: bool  # "benchmark niewykonalny bez finansowania"
    reason: str | None = None


def delta_w(
    nav: Sequence[float | None],
    flows: Sequence[float | None],
    bench: Sequence[float | None],
    a: int,
    t: int,
) -> DeltaWResult:
    """`dW_T = V_T - V_a*B_T/B_a - sum_{a<s<=T} F_s*B_T/B_s` (F = C - D; V_a =
    NAV po przeplywach dnia a), rownowazne wzorowi M80. Flaga infeasible, gdy
    jednostki benchmarku `U_s = V_a/B_a + sum_{a<u<=s} F_u/B_u < 0` dla
    jakiegos s (nie dopisuje sie kredytu ani krotkich jednostek). Wartosc None,
    gdy w oknie brakuje NAV, przeplywu lub benchmarku."""
    if nav[a] is None or nav[t] is None or bench[a] is None or bench[t] is None:
        return DeltaWResult(None, False, "brak NAV/benchmarku na krancu okna")
    units = nav[a] / bench[a]
    scale = abs(units) + 1e-12
    infeasible = False
    acc = nav[a] * bench[t] / bench[a]
    for s in range(a + 1, t + 1):
        if flows[s] is None or bench[s] is None:
            return DeltaWResult(None, False, "dzien niepelny w oknie")
        acc += flows[s] * bench[t] / bench[s]
        units += flows[s] / bench[s]
        if units < -1e-9 * scale:
            infeasible = True
    return DeltaWResult(nav[t] - acc, infeasible, None)


# ---------------------------------------------------------------------------
# (A) Obsuniecia (M88)
# ---------------------------------------------------------------------------


def drawdown_series(index: Sequence[float | None]) -> list[float | None]:
    """d_t = 1 - I_t / max_{u<=t} I_u (None tam, gdzie indeks None)."""
    out: list[float | None] = []
    peak: float | None = None
    for v in index:
        if v is None:
            out.append(None)
            continue
        peak = v if peak is None or v > peak else peak
        out.append(1.0 - v / peak)
    return out


def max_drawdown(index: Sequence[float | None]) -> float | None:
    dd = [d for d in drawdown_series(index) if d is not None]
    return max(dd) if dd else None


def current_drawdown(index: Sequence[float | None]) -> float | None:
    vals = [v for v in index if v is not None]
    if not vals:
        return None
    return 1.0 - vals[-1] / max(vals)


def relative_index(index: Sequence[float | None], bench: Sequence[float | None]) -> list[float | None]:
    """Q = I / B (indeks wzgledny, M88)."""
    return [None if i is None or b is None or b == 0 else i / b for i, b in zip(index, bench)]


class Episode(NamedTuple):
    peak: date
    trough: date
    depth: float
    recovery: date | None  # None = "brak" (nieodrobione)


def top_episodes(dates: Sequence[date], index: Sequence[float | None], n: int = 3) -> list[Episode]:
    """n najglebszych rozlacznych epizodow obsuniecia: szczyt -> dolek ->
    odrobienie (pierwszy dzien z I >= szczyt) albo None. Epizody sa z natury
    rozlaczne (kazdy konczy sie odrobieniem). Zatrzymuje sie na pierwszym None."""
    eps: list[Episode] = []
    peak_i: int | None = None
    peak_v = 0.0
    trough_i: int | None = None
    trough_v = 0.0
    for i, v in enumerate(index):
        if v is None:
            break
        if peak_i is None or v >= peak_v:
            if trough_i is not None:
                eps.append(Episode(dates[peak_i], dates[trough_i], 1.0 - trough_v / peak_v, dates[i]))
            peak_i, peak_v, trough_i = i, v, None
        elif trough_i is None or v < trough_v:
            trough_i, trough_v = i, v
    if trough_i is not None and peak_i is not None:
        eps.append(Episode(dates[peak_i], dates[trough_i], 1.0 - trough_v / peak_v, None))
    eps.sort(key=lambda e: e.depth, reverse=True)
    return eps[:n]


# ---------------------------------------------------------------------------
# (A) Mandat (M88) i alerty (M89, T44)
# ---------------------------------------------------------------------------


class MandateStatus(NamedTuple):
    status: str  # 'naruszony' | 'spelniony' | 'nieoceniony'
    violations: list[str]
    missing: list[str]


def mandate_status(components: dict[str, float | None], limits: dict[str, float]) -> MandateStatus:
    """M88: skladnik przekracza limit gdy wartosc > limit. naruszony — co
    najmniej jeden DOSTEPNY skladnik przekracza (z lista naruszen i osobno
    brakow); spelniony — wszystkie dostepne i w limitach; nieoceniony — brak
    znanego naruszenia, a jakis skladnik niedostepny."""
    violations = [k for k, v in components.items() if v is not None and v > limits[k]]
    missing = [k for k, v in components.items() if v is None]
    if violations:
        return MandateStatus("naruszony", violations, missing)
    if missing:
        return MandateStatus("nieoceniony", [], missing)
    return MandateStatus("spelniony", [], [])


def alert_states(
    dd_series: Sequence[tuple[date, float | None]],
    limit: float,
    warn: float = ALERT_WARN,
    rearm: float = ALERT_REARM,
) -> tuple[list[tuple[date, str]], str]:
    """M89/T44 dla jednego skladnika. Zdarzenia (data, 'ostrzezenie'|'alert'):
    przejscie >= warn*limit -> ostrzezenie, >= limit -> alert (ostrzezenie nie
    blokuje eskalacji; skok od razu ponad limit daje sam alert). Po komunikacie
    poziomu jego ponawianie jest wstrzymane do zejscia obsuniecia ponizej
    rearm*limit. Dni None sa pomijane. Zwraca (zdarzenia, stan biezacy:
    'ok'|'ostrzezenie'|'alert')."""
    events: list[tuple[date, str]] = []
    warn_armed = True
    alert_armed = True
    last: float | None = None
    for d, dd in dd_series:
        if dd is None:
            continue
        last = dd
        if dd < rearm * limit:
            warn_armed = True
            alert_armed = True
            continue
        if dd >= limit:
            if alert_armed:
                events.append((d, "alert"))
                alert_armed = False
            warn_armed = False
        elif dd >= warn * limit and warn_armed:
            events.append((d, "ostrzezenie"))
            warn_armed = False
    if last is None:
        state = "ok"
    elif last >= limit:
        state = "alert"
    elif last >= warn * limit:
        state = "ostrzezenie"
    else:
        state = "ok"
    return events, state


# ---------------------------------------------------------------------------
# (A) Werdykt (M83)
# ---------------------------------------------------------------------------


@dataclass
class Verdict:
    result: str  # przewaga | czesciowo | strata | remis | brak (dane niezweryfikowane)
    signs: dict[str, str | None]  # prog -> '+'|'-'|'='|None (informacyjnie gdy brak)
    winners: list[str]  # progi z '+' (dla 'czesciowo')


def verdict(
    dw_by_threshold: dict[str, float | None], v_a: float, tie: float = TIE_PCT, data_verified: bool = True
) -> Verdict:
    """M83: + gdy dW > T40, - gdy dW < -T40, = gdy |dW| <= tie*V_a. Wszystkie
    + -> przewaga; wszystkie - -> strata; wszystkie = -> remis; inaczej
    czesciowo (z lista progow z +). data_verified=False albo jakikolwiek dW
    niedostepny -> 'brak (dane niezweryfikowane)' (znaki tylko informacyjnie)."""
    t40 = tie * abs(v_a)
    signs: dict[str, str | None] = {}
    for name, dw in dw_by_threshold.items():
        if dw is None:
            signs[name] = None
        elif dw > t40:
            signs[name] = "+"
        elif dw < -t40:
            signs[name] = "-"
        else:
            signs[name] = "="
    winners = [k for k, s in signs.items() if s == "+"]
    if not data_verified or any(s is None for s in signs.values()) or not signs:
        return Verdict("brak (dane niezweryfikowane)", signs, winners)
    vals = set(signs.values())
    if vals == {"+"}:
        res = "przewaga"
    elif vals == {"-"}:
        res = "strata"
    elif vals == {"="}:
        res = "remis"
    else:
        res = "czesciowo"
    return Verdict(res, signs, winners)


def verdict_label(v: Verdict, mandate: str) -> str:
    """Etykieta M83: przewaga przy naruszonym mandacie -> 'przewaga z naruszeniem mandatu'."""
    if v.result == "przewaga" and mandate == "naruszony":
        return "przewaga z naruszeniem mandatu"
    return v.result


# ---------------------------------------------------------------------------
# (A) Beta, TE, IR (M82)
# ---------------------------------------------------------------------------


@dataclass
class WeeklyRegression:
    n: int
    alpha: float | None = None
    beta: float | None = None
    se_beta: float | None = None
    te: float | None = None
    se_te: float | None = None
    ir: float | None = None
    se_ir: float | None = None


def weekly_returns(
    dates: Sequence[date], index: Sequence[float | None], bench: Sequence[float | None]
) -> tuple[list[float], list[float]]:
    """Zwroty tygodniowe (tydzien ISO = ostatni dzien osi w tygodniu) z `index`
    i `bench`; tylko dla par kolejnych tygodni z kompletnymi koncami."""
    ends: dict[tuple[int, int], int] = {}
    for i, d in enumerate(dates):
        iso = d.isocalendar()
        ends[(iso[0], iso[1])] = i
    order = [ends[k] for k in sorted(ends)]
    rs: list[float] = []
    rb: list[float] = []
    for i0, i1 in zip(order, order[1:]):
        vals = (index[i0], index[i1], bench[i0], bench[i1])
        if any(v is None or v <= 0 for v in vals):
            continue
        rs.append(index[i1] / index[i0] - 1.0)
        rb.append(bench[i1] / bench[i0] - 1.0)
    return rs, rb


def weekly_regression(
    dates: Sequence[date], i_sat: Sequence[float | None], bench: Sequence[float | None]
) -> WeeklyRegression:
    """OLS `r_sat = alpha + beta*r_B` na zwrotach tygodniowych, SE(beta) =
    sqrt(s^2/Sxx), s^2 = SSE/(n-2); TE = std(r_sat - r_B)*sqrt(52), SE(TE) ~
    TE/sqrt(2(n-1)); IR = mean(r_sat - r_B)*52/TE, SE(IR) =
    sqrt((1 + IR_w^2/2)/n)*sqrt(52). n raportowane; n < 3 -> pola None."""
    rs, rb = weekly_returns(dates, i_sat, bench)
    n = len(rs)
    out = WeeklyRegression(n=n)
    if n < 3:
        return out
    mx, my = sum(rb) / n, sum(rs) / n
    sxx = sum((x - mx) ** 2 for x in rb)
    if sxx > 0:
        sxy = sum((x - mx) * (y - my) for x, y in zip(rb, rs))
        out.beta = sxy / sxx
        out.alpha = my - out.beta * mx
        sse = sum((y - out.alpha - out.beta * x) ** 2 for x, y in zip(rb, rs))
        out.se_beta = math.sqrt(max(sse, 0.0) / (n - 2) / sxx)
    diff = [y - x for x, y in zip(rb, rs)]
    sd = statistics.stdev(diff)
    out.te = sd * math.sqrt(52)
    out.se_te = out.te / math.sqrt(2 * (n - 1))
    if sd > 0:
        md = sum(diff) / n
        out.ir = md * 52 / out.te
        ir_w = md / sd
        out.se_ir = math.sqrt((1 + ir_w**2 / 2) / n) * math.sqrt(52)
    return out


# ---------------------------------------------------------------------------
# (A) Bramka M78 (T39)
# ---------------------------------------------------------------------------


@dataclass
class AccountValue:
    """Wiersz kotwicy M78 (brief CC-OS2 E0): wartosci brokera w walucie
    rozliczenia; None = "brak" na zrodle (nigdy nie wyliczane)."""

    date: date
    account: str  # grupa rachunku
    currency: str
    positions_value: float | None  # wartosc papierow (satelita + rdzen)
    cash_total: float | None  # srodki ogolem (KONTRAKTOWY: z depozytem zablokowanym)


def m78_gate(
    snapshots: Sequence[AccountValue],
    nav_by_day: dict[date, NavResult],
    tolerance: float = GATE_TOLERANCE,
) -> dict[str, Any]:
    """Bramka M78 per (data, rachunek, waluta) i skladnik: 'papiery' (NAV
    pozycji satelity + rdzenia) oraz 'srodki' (gotowka; KONTRAKTOWY wg §19.4).
    Zgodnosc: |roznica| <= tolerance x |wartosc brokera| (T39 = 0,01%).
    Status skladnika: zgodny / niezgodny / brak danych (None u brokera) /
    niepelny (nasza strona bez wyceny). Wynik: 'zgodne' gdy wszystkie skladniki
    zgodne; 'rozjazd' gdy jakikolwiek niezgodny; inaczej 'brak danych'."""
    rows: list[dict[str, Any]] = []
    for s in snapshots:
        key = (s.account, s.currency)
        nav = nav_by_day.get(s.date)
        for comp, broker in (("papiery", s.positions_value), ("srodki", s.cash_total)):
            row: dict[str, Any] = {"date": s.date.isoformat(), "account": s.account, "currency": s.currency,
                                   "component": comp, "broker": broker, "ours": None, "diff": None,
                                   "diff_pln": None, "rel_diff": None}
            if broker is None:
                row["status"] = "brak danych"
            elif nav is None:
                row["status"] = "niepelny"
            else:
                if comp == "papiery":
                    sat_missing = any(not t.startswith("gotowka") and t != KONTRAKTOWY_GROUP for t, _ in nav.missing)
                    available = not sat_missing and nav.core_complete
                    ours = nav.positions_native.get(key, 0.0) + nav.core_by_account_native.get(key, 0.0)
                else:
                    available = key not in nav.cash_missing
                    ours = nav.cash_native.get(key, 0.0)
                if not available:
                    row["status"] = "niepelny"
                else:
                    diff = ours - broker
                    rate = nav.rates.get(s.currency)
                    ok = abs(diff) <= tolerance * abs(broker) if broker != 0 else abs(diff) < 1e-6
                    row.update({"ours": ours, "diff": diff, "diff_pln": diff * rate if rate is not None else None,
                                "rel_diff": abs(diff) / abs(broker) if broker != 0 else None,
                                "status": "zgodny" if ok else "niezgodny"})
            rows.append(row)
    n_ok = sum(r["status"] == "zgodny" for r in rows)
    if rows and n_ok == len(rows):
        result = "zgodne"
    elif any(r["status"] == "niezgodny" for r in rows):
        result = "rozjazd"
    else:
        result = "brak danych"
    return {
        "ok": result == "zgodne",
        "result": result,
        "n": len(rows),
        "n_zgodny": n_ok,
        "tolerance": tolerance,
        "missing_components": [f"{r['account']}/{r['currency']}/{r['component']}" for r in rows
                               if r["status"] in ("brak danych", "niepelny")],
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# (B) Warstwa ladowania — tylko SELECT
# ---------------------------------------------------------------------------


@dataclass
class InstrumentInfo:
    id: int
    ticker: str
    instrument_type: str | None
    is_core: bool
    multiplier: float | None
    yahoo_symbol: str | None
    quote_currency: str | None
    calendar_code: str | None
    base_symbol: str | None = None


@dataclass
class Inputs:
    axis: list[date]
    compute_days: list[date]  # os + daty migawek (bramka M78)
    tx_rows: list[TxRow]  # wiersze trzech rachunkow satelity <= d_to, sort. po dacie
    kontraktowy_rows: list[dict[str, Any]]  # `risk._kontraktowy_rows` (<= d_to)
    instruments: dict[int, InstrumentInfo]
    prices: dict[int, tuple[list[date], list[float]]]  # close_split_adj, sort. po dacie
    layer_events: dict[int, list[dict[str, Any]]]  # {'date','ratio'} (split/reverse_split)
    fx: dict[str, tuple[list[date], list[float]]]  # waluta -> (daty, kursy NBP A)
    positions_by_day: dict[date, list[dict[str, Any]]]  # positions_as_of per dzien compute_days
    warnings: list[str] = field(default_factory=list)
    no_price_tickers: list[str] = field(default_factory=list)


def build_axis(d_from: date, d_to: date, codes: Sequence[str] = CALENDAR_CODES_AXIS) -> list[date]:
    """Os dni = suma sesji XWAR, XNYS, XETR w [d_from, d_to] (exchange_calendars)."""
    import exchange_calendars as xcals

    days: set[date] = set()
    for code in codes:
        cal = xcals.get_calendar(code)
        for ts in cal.sessions_in_range(d_from.isoformat(), d_to.isoformat()):
            days.add(ts.date())
    return sorted(days)


def fx_on(fx: dict[str, tuple[list[date], list[float]]], currency: str, d: date) -> float | None:
    """Kurs NBP A z data <= d (M1, M77); PLN = 1; brak -> None."""
    if currency == "PLN":
        return 1.0
    series = fx.get(currency)
    if not series:
        return None
    dates, rates = series
    k = bisect.bisect_right(dates, d)
    return rates[k - 1] if k else None


def _f(x: Any) -> float | None:
    return None if x is None else float(x)


def load_symbol_map(path: Path | str) -> dict[str, dict[str, str]]:
    """CSV broker_ticker,isin,yahoo_symbol,exchange_mic,currency -> {ticker: wiersz}."""
    out: dict[str, dict[str, str]] = {}
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            out[row["broker_ticker"].strip()] = {k: (v or "").strip() for k, v in row.items()}
    return out


def _read_prices_cache(path: Path) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    if path.exists():
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                out.setdefault(row["broker_ticker"], {})[date.fromisoformat(row["date"])] = float(row["close_split_adj"])
    return out


def _fetch_mapped_prices(
    mapping: dict[str, dict[str, str]],
    tickers: Sequence[str],
    d_from: date,
    d_to: date,
    out_dir: Path | None,
    warnings: list[str],
) -> dict[str, dict[date, float]]:
    """Ceny instrumentow bez prices_daily z `symbol_map` przez `prices.fetch_ohlc`
    (plik prices_cache.csv w katalogu wynikow; gdy istnieje — czytany stad).
    Kontrola waluty (fetch_currency vs mapa) i skokow |log-zwrot| > 0,5 daje
    ostrzezenie, nie zmienia danych."""
    cache_path = out_dir / "prices_cache.csv" if out_dir is not None else None
    cached = _read_prices_cache(cache_path) if cache_path is not None else {}
    result: dict[str, dict[date, float]] = {t: v for t, v in cached.items() if t in tickers}
    todo = [t for t in tickers if t in mapping and t not in result]
    if todo:
        from mannaz import prices as _prices

        for t in todo:
            m = mapping[t]
            rows = _prices.fetch_ohlc(m["yahoo_symbol"], d_from - timedelta(days=45), d_to)
            series = {r.price_date: float(r.close_split_adj) for r in rows if r.close_split_adj is not None}
            cur = _prices.fetch_currency(m["yahoo_symbol"])
            if cur and m.get("currency") and cur != m["currency"]:
                warnings.append(f"{t}: waluta dostawcy {cur} != mapa {m['currency']}")
            ds = sorted(series)
            for d0, d1 in zip(ds, ds[1:]):
                if series[d0] > 0 and series[d1] > 0 and abs(math.log(series[d1] / series[d0])) > 0.5:
                    warnings.append(f"{t}: skok ceny {d0}->{d1} (|log| > 0,5)")
            result[t] = series
        if cache_path is not None:
            all_rows = dict(cached)
            all_rows.update({t: result[t] for t in todo})
            with open(cache_path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["broker_ticker", "date", "close_split_adj"])
                for t in sorted(all_rows):
                    for d in sorted(all_rows[t]):
                        w.writerow([t, d.isoformat(), repr(all_rows[t][d])])
    return result


def load_inputs(
    cur: Any,
    d_from: date,
    d_to: date,
    symbol_map: dict[str, dict[str, str]] | None = None,
    out_dir: Path | None = None,
    extra_days: Sequence[date] = (),
) -> Inputs:
    """Ladowanie hurtowe, WYLACZNIE SELECT: transakcje trzech rachunkow,
    instrumenty, ceny (close_split_adj), zdarzenia warstwy (split/reverse_split),
    FX (fx_nbp) i wiersze KONTRAKTOWY (`risk._kontraktowy_rows`) — raz; pozycje
    per dzien przez `fifo.positions_as_of` z cache po dniach bez zmian
    (transakcje z instrumentem lub zdarzenia korporacyjne w (poprzedni, D]).
    `symbol_map`: instrumenty bez cen w prices_daily dostaja ceny z
    `prices.fetch_ohlc` (prices_cache.csv w `out_dir`) i kalendarz z
    exchange_mic; bez mapy zostaja bez cen (NAV niepelny, przyczyna 'brak cen').
    `extra_days`: dodatkowe daty liczenia (migawki bramki M78)."""
    from mannaz.fifo import positions_as_of

    axis = build_axis(d_from, d_to)
    compute_days = sorted(set(axis) | {d for d in extra_days if d_from <= d <= d_to})
    warnings: list[str] = []

    cur.execute(
        "SELECT id, broker_ticker, instrument_type, is_core, multiplier, yahoo_symbol, currency, exchange, base_symbol "
        "FROM instruments"
    )
    instruments: dict[int, InstrumentInfo] = {}
    for iid, tick, itype, is_core, mult, ysym, qcur, exch, base in cur.fetchall():
        instruments[iid] = InstrumentInfo(
            id=iid,
            ticker=tick,
            instrument_type=itype,
            is_core=bool(is_core),
            multiplier=_f(mult),
            yahoo_symbol=ysym,
            quote_currency=qcur,
            calendar_code=EXCHANGE_TO_CALENDAR_CODE.get(exch) if exch else None,
            base_symbol=base,
        )

    like = " OR ".join(["t.rachunek LIKE %s"] * len(SATELLITE_GROUPS))
    cur.execute(
        "SELECT t.transaction_date, t.rachunek, t.currency, t.row_type, t.amount, t.instrument_id, t.qty, t.price, "
        "COALESCE(i.is_core, false), i.instrument_type "
        "FROM transactions t LEFT JOIN instruments i ON i.id = t.instrument_id "
        f"WHERE t.transaction_date <= %s AND ({like}) ORDER BY t.transaction_date, t.id",
        (d_to, *[f"{g}%" for g in SATELLITE_GROUPS]),
    )
    tx_rows = [
        TxRow(d, rach, c, rt, float(amt), iid, _f(q), _f(p), bool(core), itype)
        for d, rach, c, rt, amt, iid, q, p, core, itype in cur.fetchall()
    ]

    kontraktowy_rows = _risk._kontraktowy_rows(cur, d_to)

    cur.execute(
        "SELECT instrument_id, price_date, close_split_adj FROM prices_daily "
        "WHERE price_date <= %s AND close_split_adj IS NOT NULL ORDER BY instrument_id, price_date",
        (d_to,),
    )
    prices: dict[int, tuple[list[date], list[float]]] = {}
    for iid, d, c in cur.fetchall():
        ds, cs = prices.setdefault(iid, ([], []))
        ds.append(d)
        cs.append(float(c))

    cur.execute(
        "SELECT instrument_id, event_date, ratio FROM corporate_events "
        "WHERE ratio IS NOT NULL AND event_type IN ('split', 'reverse_split') ORDER BY instrument_id, event_date"
    )
    layer_events: dict[int, list[dict[str, Any]]] = {}
    for iid, d, ratio in cur.fetchall():
        layer_events.setdefault(iid, []).append({"date": d, "ratio": ratio})

    cur.execute("SELECT pair, rate_date, rate FROM fx_nbp WHERE rate_date <= %s ORDER BY pair, rate_date", (d_to,))
    fx: dict[str, tuple[list[date], list[float]]] = {}
    for pair, d, rate in cur.fetchall():
        ds, rs = fx.setdefault(pair.split("/")[0], ([], []))
        ds.append(d)
        rs.append(float(rate))

    cur.execute("SELECT DISTINCT transaction_date FROM transactions WHERE instrument_id IS NOT NULL AND transaction_date <= %s", (d_to,))
    change_dates = {r[0] for r in cur.fetchall()}
    cur.execute("SELECT DISTINCT event_date FROM corporate_events WHERE event_date <= %s", (d_to,))
    change_dates |= {r[0] for r in cur.fetchall()}
    change_sorted = sorted(change_dates)

    positions_by_day: dict[date, list[dict[str, Any]]] = {}
    last_day: date | None = None
    last_pos: list[dict[str, Any]] = []
    for d in compute_days:
        if last_day is not None:
            k = bisect.bisect_right(change_sorted, last_day)
            if not (k < len(change_sorted) and change_sorted[k] <= d):
                positions_by_day[d] = last_pos
                last_day = d
                continue
        last_pos = [p for p in positions_as_of(cur, d) if account_group(p["rachunek"]) in SATELLITE_GROUPS]
        positions_by_day[d] = last_pos
        last_day = d

    # instrumenty trzymane kiedykolwiek w oknie bez zadnej ceny
    held_ids = {p["instrument_id"] for ps in positions_by_day.values() for p in ps}
    held_ids |= {r.instrument_id for r in tx_rows if r.row_type == "bilans_otwarcia" and r.instrument_id is not None}
    no_price_ids = [
        i for i in held_ids
        if i in instruments and instruments[i].instrument_type != "future" and not prices.get(i)
    ]
    no_price_tickers = sorted(instruments[i].ticker for i in no_price_ids)
    if symbol_map and no_price_ids:
        wanted = [instruments[i].ticker for i in no_price_ids if instruments[i].ticker in symbol_map]
        fetched = _fetch_mapped_prices(symbol_map, wanted, d_from, d_to, out_dir, warnings)
        for i in no_price_ids:
            info = instruments[i]
            m = symbol_map.get(info.ticker)
            series = fetched.get(info.ticker)
            if not m or not series:
                continue
            ds = sorted(series)
            prices[i] = (ds, [series[d] for d in ds])
            info.yahoo_symbol = info.yahoo_symbol or m["yahoo_symbol"]
            info.quote_currency = info.quote_currency or m.get("currency") or None
            if info.calendar_code is None and m.get("exchange_mic"):
                info.calendar_code = m["exchange_mic"]
        no_price_tickers = sorted(instruments[i].ticker for i in no_price_ids if not prices.get(i))

    return Inputs(
        axis=axis,
        compute_days=compute_days,
        tx_rows=tx_rows,
        kontraktowy_rows=kontraktowy_rows,
        instruments=instruments,
        prices=prices,
        layer_events=layer_events,
        fx=fx,
        positions_by_day=positions_by_day,
        warnings=warnings,
        no_price_tickers=no_price_tickers,
    )


# ---------------------------------------------------------------------------
# (B2) Skladanie NAV/przeplywow z Inputs — czyste przy gotowym Inputs
# ---------------------------------------------------------------------------


class Valuer:
    """Wycena instrumentu na dzien D z Inputs: cena close_split_adj (regula T27
    przez `risk.resolve_price_on_d`, kalendarz instrumentu), czynnik warstwy,
    FX waluty notowania <= D."""

    def __init__(
        self,
        inp: Inputs,
        calendar_facts_fn: Callable[[str | None, date], Any] = _risk._default_calendar_facts,
    ) -> None:
        self.inp = inp
        self._facts_fn = calendar_facts_fn
        self._facts: dict[tuple[str | None, date], Any] = {}

    def _calendar(self, code: str | None, d: date) -> Any:
        key = (code, d)
        if key not in self._facts:
            self._facts[key] = self._facts_fn(code, d)
        return self._facts[key]

    def price(self, iid: int, d: date) -> tuple[float | None, str, str | None]:
        """(cena z pence, status ok/stale/incomplete, przyczyna)."""
        info = self.inp.instruments[iid]
        series = self.inp.prices.get(iid)
        if not series or not series[0]:
            reason = "brak cen"
            if (info.instrument_type or "").lower().startswith("cert") or info.ticker.startswith("INTL"):
                reason = "certyfikat bez wyceny"
            return None, "incomplete", reason
        dates, closes = series
        k = bisect.bisect_right(dates, d)
        tail = dates[max(0, k - PRICE_TAIL):k]
        facts = self._calendar(info.calendar_code, d)
        status, used, reason = _risk.resolve_price_on_d(
            price_dates=tail, as_of=d, sessions_before=facts.sessions_before, is_session_on_d=facts.is_session_on_d
        )
        if status == "incomplete":
            return None, status, reason
        px = closes[bisect.bisect_right(dates, used) - 1]
        pence = float(_risk.gbp_pence_factor(info.quote_currency, info.yahoo_symbol))
        return px * pence, status, None

    def layer(self, iid: int, d: date) -> float:
        return float(_risk.layer_factor_after(self.inp.layer_events.get(iid, []), d))

    def position_input(self, pos: dict[str, Any], d: date) -> PositionInput:
        iid = pos["instrument_id"]
        info = self.inp.instruments[iid]
        px, status, reason = self.price(iid, d)
        qcur = info.quote_currency or pos["currency"]
        return PositionInput(
            rachunek=pos["rachunek"],
            currency=pos["currency"],
            ticker=info.ticker,
            qty=float(pos["qty"]),
            layer_factor=self.layer(iid, d),
            price=px,
            price_status=status,
            price_reason=reason,
            quote_fx=fx_on(self.inp.fx, qcur, d),
            settle_fx=fx_on(self.inp.fx, pos["currency"], d),
            is_core=info.is_core,
        )

    def noncash_value(self, row: TxRow) -> float | None:
        """+qty x cena(D) x FX (wartosc rynkowa z dnia transferu, M80)."""
        if row.instrument_id is None or row.qty is None or row.instrument_id not in self.inp.instruments:
            return None
        info = self.inp.instruments[row.instrument_id]
        px, status, _ = self.price(row.instrument_id, row.date)
        fx = fx_on(self.inp.fx, info.quote_currency or row.currency, row.date)
        if px is None or fx is None:
            return None
        return row.qty * self.layer(row.instrument_id, row.date) * px * fx


@dataclass
class DailyResult:
    navs: dict[date, NavResult]
    flows: dict[date, float | None]  # per dzien osi, przeplyw (prev, D]; None = niepelny
    flow_issues: dict[date, list[tuple[str, str]]]  # dzien -> (opis, przyczyna)
    unknown_row_types: list[str]


def compute_daily(
    inp: Inputs,
    calendar_facts_fn: Callable[[str | None, date], Any] = _risk._default_calendar_facts,
) -> DailyResult:
    """NAV na kazdy dzien `compute_days` i przeplyw zewnetrzny F na kazdy dzien
    osi (przeplywy z (poprzedni dzien osi, D], w tym weekendowe). Nieznany
    row_type -> F None dla dnia + wpis w `unknown_row_types` (nigdy klasa
    domyslna)."""
    valuer = Valuer(inp, calendar_facts_fn)
    rows = inp.tx_rows
    row_dates = [r.date for r in rows]
    navs: dict[date, NavResult] = {}

    cash: dict[tuple[str, str], float] = {}
    ptr = 0
    kon_dates = [r["transaction_date"] for r in inp.kontraktowy_rows]
    kon_sorted = sorted(range(len(kon_dates)), key=lambda i: kon_dates[i])
    kon_rows_sorted = [inp.kontraktowy_rows[i] for i in kon_sorted]
    kon_dates_sorted = [kon_dates[i] for i in kon_sorted]

    for d in inp.compute_days:
        while ptr < len(rows) and rows[ptr].date <= d:
            r = rows[ptr]
            ptr += 1
            grp = account_group(r.rachunek)
            if grp == KONTRAKTOWY_GROUP or r.row_type in _risk.NON_CASH_ROW_TYPES:
                continue
            cash[(grp, r.currency)] = cash.get((grp, r.currency), 0.0) + r.amount
        positions = [valuer.position_input(p, d) for p in inp.positions_by_day.get(d, [])
                     if inp.instruments[p["instrument_id"]].instrument_type != "future"]
        k = bisect.bisect_right(kon_dates_sorted, d)
        kon_val: float | None = None
        kon_err: str | None = None
        if k:
            try:
                kon_val = float(_risk.kontraktowy_account_value(kon_rows_sorted[:k], d))
            except ValueError as exc:
                kon_err = "KONTRAKTOWY: " + str(exc).split("dla ")[0][:80]
        fx_map = {c: fx_on(inp.fx, c, d) for c in {c for (_, c) in cash}}
        navs[d] = nav_for_day(positions, cash, fx_map, kon_val, kon_err)

    flows: dict[date, float | None] = {}
    issues: dict[date, list[tuple[str, str]]] = {}
    unknown: set[str] = set()
    fx_fn: FxFn = lambda c, d: fx_on(inp.fx, c, d)
    prev: date | None = None
    for d in inp.axis:
        if prev is None:
            flows[d] = 0.0
            prev = d
            continue
        lo = bisect.bisect_right(row_dates, prev)
        hi = bisect.bisect_right(row_dates, d)
        try:
            res = external_flow_pln(rows[lo:hi], fx_fn, valuer.noncash_value)
            if res.missing:
                flows[d] = None
                issues[d] = res.missing
            else:
                flows[d] = res.value
        except UnknownRowTypeError as exc:
            flows[d] = None
            unknown.add(str(exc))
            issues[d] = [(str(exc), "nieznany row_type")]
        prev = d
    return DailyResult(navs, flows, issues, sorted(unknown))


# ---------------------------------------------------------------------------
# Progi (M81)
# ---------------------------------------------------------------------------


def bench_on_axis(
    axis: Sequence[date], prices_eur: dict[date, float], eurpln: Callable[[date], float | None]
) -> tuple[list[float | None], list[bool]]:
    """B_j w PLN na osi: ostatnia cena <= D x EUR/PLN <= D. Drugi wynik:
    flaga dnia bez sesji Xetry (uzyta cena starsza niz D)."""
    ds = sorted(prices_eur)
    out: list[float | None] = []
    stale: list[bool] = []
    for d in axis:
        k = bisect.bisect_right(ds, d)
        rate = eurpln(d)
        if not k or rate is None:
            out.append(None)
            stale.append(False)
            continue
        out.append(prices_eur[ds[k - 1]] * rate)
        stale.append(ds[k - 1] != d)
    return out, stale


def load_benchmarks(
    d_from: date, d_to: date, out_dir: Path
) -> dict[str, dict[date, tuple[float, str]]]:
    """SXR8.DE i SPYI.DE (EUR) przez `prices.fetch_ohlc`: pole
    adj_close_total_return, fallback close_split_adj (kolumna `field`).
    Cache `benchmarks_cache.csv` w katalogu wynikow; gdy istnieje — czytany."""
    path = out_dir / "benchmarks_cache.csv"
    result: dict[str, dict[date, tuple[float, str]]] = {k: {} for k in BENCHMARK_SYMBOLS}
    if path.exists():
        sym_to_key = {v: k for k, v in BENCHMARK_SYMBOLS.items()}
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                key = sym_to_key.get(row["symbol"])
                if key:
                    result[key][date.fromisoformat(row["date"])] = (float(row["price_eur"]), row["field"])
        if all(result.values()):
            return result
        result = {k: {} for k in BENCHMARK_SYMBOLS}
    from mannaz import prices as _prices

    for key, sym in BENCHMARK_SYMBOLS.items():
        for r in _prices.fetch_ohlc(sym, d_from - timedelta(days=10), d_to):
            if r.adj_close_total_return is not None:
                result[key][r.price_date] = (float(r.adj_close_total_return), "adj_close_total_return")
            elif r.close_split_adj is not None:
                result[key][r.price_date] = (float(r.close_split_adj), "close_split_adj")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "date", "price_eur", "field"])
        for key, sym in BENCHMARK_SYMBOLS.items():
            for d in sorted(result[key]):
                w.writerow([sym, d.isoformat(), repr(result[key][d][0]), result[key][d][1]])
    return result


def compare_with_p3_series(
    bench_pln: dict[str, dict[date, float]], path: Path | str
) -> dict[str, dict[str, Any]]:
    """Kontrolka: max |wzgledna roznica| B_j (cena EUR x EUR/PLN, bez
    normalizacji) vs `p3_series.csv` (kolumny SP500_V_PLN, SPYI_V_PLN) w
    czesci wspolnej dat. Klucze: 'sp500', 'spyi'; wynik {n, max_rel_diff}."""
    cols = {"sp500": "SP500_V_PLN", "spyi": "SPYI_V_PLN"}
    ref: dict[str, dict[date, float]] = {k: {} for k in cols}
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            d = date.fromisoformat(row["date"])
            for k, c in cols.items():
                if row.get(c):
                    ref[k][d] = float(row[c])
    out: dict[str, dict[str, Any]] = {}
    for k in cols:
        common = sorted(set(ref[k]) & set(bench_pln.get(k, {})))
        diffs = [abs(bench_pln[k][d] / ref[k][d] - 1.0) for d in common if ref[k][d] != 0]
        out[k] = {"n": len(diffs), "max_rel_diff": max(diffs) if diffs else None}
    return out


def _opt_value(raw: str | None) -> float | None:
    v = (raw or "").strip()
    return None if v == "" or v.lower() == "brak" else float(v)


def load_account_values(path: Path | str) -> list[AccountValue]:
    """CSV kotwicy M78 wg briefu CC-OS2 E0: date,account,currency,positions_value,
    cash_total — jeden wiersz na rachunek i walute; "brak" albo puste pole ->
    None (skladnik bez danych, nie zero). `account` sprowadzane do grupy."""
    out: list[AccountValue] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            out.append(AccountValue(
                date=date.fromisoformat(row["date"].strip()),
                account=account_group(row["account"].strip()),
                currency=row["currency"].strip(),
                positions_value=_opt_value(row.get("positions_value")),
                cash_total=_opt_value(row.get("cash_total")),
            ))
    return sorted(out, key=lambda s: (s.date, s.account, s.currency))


# ---------------------------------------------------------------------------
# Analiza szeregow (czysta) — okna, obsuniecia, mandat, alerty, werdykt
# ---------------------------------------------------------------------------


def minus_months(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _window_bounds(axis: Sequence[date], s_idx: int, t_idx: int, months: int | None) -> tuple[int, bool]:
    if months is None:
        return s_idx, False
    a = bisect.bisect_right(list(axis), minus_months(axis[t_idx], months)) - 1
    if a < s_idx:
        return s_idx, True
    return a, False


def _rel(index: list[float] | None, bench: Sequence[float | None], a: int) -> list[float | None] | None:
    if index is None:
        return None
    seg = bench[a:a + len(index)]
    if any(b is None or b <= 0 for b in seg):
        return None
    return relative_index(index, [b / seg[0] for b in seg])


def window_summary(
    axis: Sequence[date],
    nav: Sequence[float | None],
    flows: Sequence[float | None],
    r: Sequence[float | None],
    bench: dict[str, Sequence[float | None]],
    a: int,
    t: int,
    data_verified: bool,
    shorter_than_requested: bool = False,
) -> dict[str, Any]:
    """Podsumowanie okna [a, t] (indeksy osi): TWR, obsuniecia (bezwzgledne i
    wzgledne), epizody, mandat (M88), dW per prog, werdykt (M83), regresja."""
    dates = list(axis[a:t + 1])
    complete = all(v is not None for v in nav[a:t + 1]) and all(f is not None for f in flows[a + 1:t + 1])
    i_win = window_index(r, a, t)
    out: dict[str, Any] = {
        "from": axis[a].isoformat(),
        "to": axis[t].isoformat(),
        "n_days": t - a + 1,
        "complete": complete,
        "shorter_than_requested": shorter_than_requested,
        "v_a": nav[a],
        "v_t": nav[t],
        "twr": None if i_win is None else i_win[-1] - 1.0,
    }
    comps: dict[str, float | None] = {"max_dd_abs": max_drawdown(i_win) if i_win else None}
    limits = {"max_dd_abs": L_MAX}
    out["abs"] = {
        "max_dd": comps["max_dd_abs"],
        "episodes": [_ep(e) for e in top_episodes(dates, i_win)] if i_win else None,
    }
    dws: dict[str, float | None] = {}
    out["bench"] = {}
    for key, b in bench.items():
        q = _rel(i_win, b, a)
        dw = delta_w(nav, flows, b, a, t)
        dws[key] = dw.value
        comps[f"max_dd_rel_{key}"] = max_drawdown(q) if q else None
        limits[f"max_dd_rel_{key}"] = L_REL
        reg = weekly_regression(dates, i_win, [None if x is None else x for x in b[a:t + 1]]) if i_win else WeeklyRegression(0)
        out["bench"][key] = {
            "label": BENCHMARK_LABELS.get(key, key),
            "delta_w_pln": dw.value,
            "infeasible": dw.infeasible,
            "infeasible_note": "benchmark niewykonalny bez finansowania" if dw.infeasible else None,
            "max_dd_rel": comps[f"max_dd_rel_{key}"],
            "episodes_rel": [_ep(e) for e in top_episodes(dates, q)] if q else None,
            "regression": regression_dict(reg),
        }
    ms = mandate_status(comps, limits)
    out["mandate"] = {"status": ms.status, "violations": ms.violations, "missing": ms.missing, "components": comps}
    v = verdict(dws, nav[a] if nav[a] is not None else 0.0, TIE_PCT, data_verified and complete)
    out["verdict"] = {
        "result": v.result,
        "label": verdict_label(v, ms.status),
        "signs": v.signs,
        "winners": v.winners,
        "data_verified": bool(data_verified and complete),
    }
    return out


def regression_dict(reg: WeeklyRegression) -> dict[str, Any]:
    return {k: getattr(reg, k) for k in ("n", "alpha", "beta", "se_beta", "te", "se_te", "ir", "se_ir")}


def _ep(e: Episode) -> dict[str, Any]:
    return {
        "peak": e.peak.isoformat(),
        "trough": e.trough.isoformat(),
        "depth": e.depth,
        "recovery": e.recovery.isoformat() if e.recovery else None,
    }


def analyze_series(
    axis: Sequence[date],
    nav: Sequence[float | None],
    flows: Sequence[float | None],
    bench: dict[str, Sequence[float | None]],
    data_verified: bool,
    window_months: int = 24,
) -> dict[str, Any]:
    """Czysta analiza szeregow osi: r, I_sat, B znormalizowane na S, Q_j, okna
    [S,T] i [T-24m, T], obsuniecie biezace od S, alerty (M89), rejestr
    naruszen (okna 24 m. konczace sie w kazdym tygodniu)."""
    r, idx = twr(nav, flows)
    s_idx = next((i for i, v in enumerate(idx) if v is not None), None)
    t_idx = len(axis) - 1
    res: dict[str, Any] = {"S": axis[s_idx].isoformat() if s_idx is not None else None, "T": axis[t_idx].isoformat() if axis else None}
    if s_idx is None:
        res["windows"] = {}
        res["r"], res["I_sat"], res["B_norm"], res["Q"] = r, idx, {}, {}
        return res
    b_norm: dict[str, list[float | None]] = {}
    q_series: dict[str, list[float | None]] = {}
    for key, b in bench.items():
        base = b[s_idx]
        bn = [None if (x is None or base is None or base <= 0) else x / base for x in b]
        b_norm[key] = bn
        q_series[key] = relative_index(idx, bn)
    windows: dict[str, Any] = {}
    for name, months in (("od_S", None), (f"{window_months}m", window_months)):
        a, shorter = _window_bounds(axis, s_idx, t_idx, months)
        windows[name] = window_summary(axis, nav, flows, r, bench, a, t_idx, data_verified, shorter)
    # biezace obsuniecie od maksimum od S + alerty (M89), osobno dla 3 skladnikow
    alerts: dict[str, Any] = {}
    series_by_comp = {"abs": (idx, L_MAX), **{f"rel_{k}": (q_series[k], L_REL) for k in bench}}
    for comp, (ser, lim) in series_by_comp.items():
        dd = drawdown_series(ser)
        events, state = alert_states(list(zip(axis, dd)), lim)
        alerts[comp] = {
            "limit": lim,
            "current_drawdown": current_drawdown(ser),
            "state": state,
            "events": [{"date": d.isoformat(), "level": lv} for d, lv in events],
        }
    # rejestr naruszen: okna 24 m. konczace sie na koncach tygodni
    registry: list[dict[str, Any]] = []
    week_ends: dict[tuple[int, int], int] = {}
    for i, d in enumerate(axis):
        if i >= s_idx:
            iso = d.isocalendar()
            week_ends[(iso[0], iso[1])] = i
    for i in sorted(week_ends.values()):
        a, _ = _window_bounds(axis, s_idx, i, window_months)
        iw = window_index(r, a, i)
        if iw is None:
            continue
        comps = {"max_dd_abs": max_drawdown(iw)}
        lims = {"max_dd_abs": L_MAX}
        for key in bench:
            q = _rel(iw, bench[key], a)
            comps[f"max_dd_rel_{key}"] = max_drawdown(q) if q else None
            lims[f"max_dd_rel_{key}"] = L_REL
        ms = mandate_status(comps, lims)
        if ms.violations:
            registry.append({"window_end": axis[i].isoformat(), "violations": ms.violations})
    res.update(
        {
            "windows": windows,
            "alerts": alerts,
            "violation_registry": registry,
            "r": r,
            "I_sat": idx,
            "B_norm": b_norm,
            "Q": q_series,
        }
    )
    return res


# ---------------------------------------------------------------------------
# (C) run_satellite
# ---------------------------------------------------------------------------


def _clean(o: Any) -> Any:
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, date):
        return o.isoformat()
    return o


def _pct(x: float | None) -> str:
    return "n/d" if x is None else f"{x * 100:.2f}%"


def _num(x: float | None, nd: int = 2) -> str:
    return "n/d" if x is None else f"{x:,.{nd}f}"


def render_markdown(report: dict[str, Any]) -> str:
    L: list[str] = []
    L.append("# Ocena satelity (satellite.md)")
    L.append("")
    L.append(
        f"Zakres: {report['d_from']} .. {report['d_to']}; os dni = XWAR + XNYS + XETR; "
        f"kwoty w PLN; FX = NBP A (<= D); ceny = prices_daily.close_split_adj x czynnik warstwy; "
        f"progi = {BENCHMARK_SYMBOLS['sp500']} i {BENCHMARK_SYMBOLS['spyi']} "
        f"(pole adj_close_total_return, fallback close_split_adj) x EUR/PLN NBP A."
    )
    L.append("")
    L.append("## Dane")
    L.append(f"- dni osi: {report['n_axis_days']}; dni niepelne: {report['n_incomplete_days']}")
    L.append(f"- S (start indeksu): {report['S']}; T: {report['T']}")
    gate = report.get("gate")
    L.append(
        "- bramka M78: "
        + ("brak pliku account_values (dane niezweryfikowane)" if gate is None
           else f"{gate['result']} ({gate['n_zgodny']}/{gate['n']} skladnikow zgodnych, T39 = {gate['tolerance'] * 100:.2f}%)")
    )
    for r in (gate or {}).get("rows", []):
        L.append(f"  - {r['date']} {r['account']}/{r['currency']} {r['component']}: {r['status']}; broker {_num(r['broker'])}, "
                 f"NAV {_num(r['ours'])}, roznica {_num(r['diff'])} ({_num(r['diff_pln'])} PLN, {_pct(r['rel_diff'])})")
    L.append(f"- status danych: {'zweryfikowane' if report['data_verified'] else 'niezweryfikowane'}")
    for reason in report["data_unverified_reasons"]:
        L.append(f"  - {reason}")
    if report["unknown_row_types"]:
        L.append(f"- nieznane row_type: {', '.join(report['unknown_row_types'])}")
    if report["missing_summary"]:
        L.append("- przyczyny niepelnego NAV (liczba dni): " + "; ".join(f"{k}: {v}" for k, v in report["missing_summary"].items()))
    if report.get("p3_control"):
        L.append("- kontrolka vs p3_series.csv (max |wzgl. roznica|): " + "; ".join(
            f"{k}: n={v['n']}, max={v['max_rel_diff']}" for k, v in report["p3_control"].items()))
    for w in report["warnings"]:
        L.append(f"- uwaga: {w}")
    for name, win in report["windows"].items():
        L.append("")
        L.append(f"## Okno {name}: {win['from']} .. {win['to']} ({win['n_days']} dni)")
        if win["shorter_than_requested"]:
            L.append("- historia krotsza niz zadane okno (okno od S)")
        L.append(f"- kompletne: {win['complete']}; V_a = {_num(win['v_a'])} PLN; V_T = {_num(win['v_t'])} PLN; TWR = {_pct(win['twr'])}")
        L.append(f"- max obsuniecie bezwzgledne: {_pct(win['abs']['max_dd'])} (L_max {L_MAX * 100:.0f}%)")
        for e in win["abs"]["episodes"] or []:
            L.append(f"  - epizod: szczyt {e['peak']}, dolek {e['trough']}, {_pct(e['depth'])}, odrobienie {e['recovery'] or 'brak'}")
        for key, b in win["bench"].items():
            L.append(f"- {b['label']}: dW = {_num(b['delta_w_pln'])} PLN" + (f" [{b['infeasible_note']}]" if b["infeasible"] else "")
                     + f"; max obsuniecie wzgledne {_pct(b['max_dd_rel'])} (L_rel {L_REL * 100:.0f}%)")
            reg = b["regression"]
            L.append(f"  - tyg.: n={reg['n']}, beta={_num(reg['beta'], 3)} (SE {_num(reg['se_beta'], 3)}), TE={_pct(reg['te'])} (SE {_pct(reg['se_te'])}), "
                     f"IR={_num(reg['ir'], 3)} (SE {_num(reg['se_ir'], 3)})")
        m = win["mandate"]
        L.append(f"- mandat: {m['status']}; naruszenia: {', '.join(m['violations']) or '-'}; braki: {', '.join(m['missing']) or '-'}")
        v = win["verdict"]
        L.append(f"- werdykt wyniku: {v['label']} (znaki: {v['signs']}; dane zweryfikowane: {v['data_verified']})")
    L.append("")
    L.append("## Alerty (obsuniecie biezace od S)")
    for comp, a in report.get("alerts", {}).items():
        ev = ", ".join(f"{e['date']} {e['level']}" for e in a["events"]) or "brak zdarzen"
        L.append(f"- {comp}: stan {a['state']}, obsuniecie biezace {_pct(a['current_drawdown'])} (limit {a['limit'] * 100:.0f}%); {ev}")
    L.append("")
    L.append(f"## Rejestr naruszen (okna 24 m., konce tygodni): {len(report.get('violation_registry', []))} okien z przekroczeniem")
    return "\n".join(L) + "\n"


def run_satellite(
    conn: Any,
    d_from: date,
    d_to: date,
    out_dir: Path | str,
    account_values: Path | str | None = None,
    symbol_map: Path | str | None = None,
    p3_series_path: Path | str | None = None,
) -> dict[str, Any]:
    """Orkiestracja E3: `conn.read_only = True` (zero zapisow do bazy), os dni,
    NAV/przeplywy, progi, okna [S,T] i 24 m., bramka M78 (gdy `account_values`),
    zapis satellite.json i satellite.md w `out_dir`. Zwraca podsumowanie
    (agregaty)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    conn.read_only = True
    snapshots = load_account_values(account_values) if account_values else []
    smap = load_symbol_map(symbol_map) if symbol_map else None
    with conn.cursor() as cur:
        inp = load_inputs(cur, d_from, d_to, smap, out, extra_days=sorted({s.date for s in snapshots}))
    daily = compute_daily(inp)

    benches_raw = load_benchmarks(d_from, d_to, out)
    eurpln = lambda d: fx_on(inp.fx, "EUR", d)  # noqa: E731
    bench: dict[str, list[float | None]] = {}
    bench_stale: dict[str, list[bool]] = {}
    for key, rows in benches_raw.items():
        bench[key], bench_stale[key] = bench_on_axis(inp.axis, {d: v[0] for d, v in rows.items()}, eurpln)
    bench_fields = {k: sorted({v[1] for v in rows.values()}) for k, rows in benches_raw.items()}

    axis = inp.axis
    nav = [daily.navs[d].value for d in axis]
    flows = [daily.flows[d] for d in axis]
    gate = m78_gate(snapshots, daily.navs) if snapshots else None
    day_incomplete = [d for d, n, f in zip(axis, nav, flows) if n is None or f is None]
    reasons: list[str] = []
    if gate is None:
        reasons.append("brak bramki M78 (nie podano account_values)")
    elif gate["result"] == "rozjazd":
        reasons.append("bramka M78: rozjazd powyzej T39")
    if gate is not None and gate["missing_components"]:
        reasons.append("bramka M78: brak kotwicy dla " + ", ".join(gate["missing_components"]))
    if daily.unknown_row_types:
        reasons.append("klasyfikacja przeplywow niekompletna (nieznany row_type)")
    verified_base = gate is not None and gate["ok"] and not daily.unknown_row_types

    analysis = analyze_series(axis, nav, flows, bench, data_verified=verified_base)
    for w in analysis["windows"].values():
        if not w["complete"]:
            reasons.append(f"okno {w['from']}..{w['to']} zawiera dzien niepelny")
    missing_summary: dict[str, int] = {}
    for d in axis:
        for tick, why in daily.navs[d].missing:
            k = f"{tick if tick == KONTRAKTOWY_GROUP else 'pozycja'}:{why}"
            missing_summary[k] = missing_summary.get(k, 0) + 1
    if inp.no_price_tickers:
        reasons.append(f"instrumenty bez cen w prices_daily i bez mapy: {len(inp.no_price_tickers)}")

    p3_control = None
    if p3_series_path:
        p3_control = compare_with_p3_series(
            {k: {d: v for i, (d, v) in enumerate(zip(axis, b)) if v is not None and not bench_stale[k][i]}
             for k, b in bench.items()},
            p3_series_path,
        )

    report: dict[str, Any] = {
        "d_from": d_from.isoformat(),
        "d_to": d_to.isoformat(),
        "conventions": {
            "currency": "PLN",
            "fx": "NBP A, ostatni kurs z data <= D",
            "prices": "prices_daily.close_split_adj x layer_factor_after, regula T27",
            "benchmarks": {k: {"symbol": BENCHMARK_SYMBOLS[k], "fields": bench_fields[k]} for k in BENCHMARK_SYMBOLS},
        },
        "n_axis_days": len(axis),
        "n_incomplete_days": len(day_incomplete),
        "S": analysis["S"],
        "T": analysis["T"],
        "gate": gate,
        "data_verified": verified_base and not any(not w["complete"] for w in analysis["windows"].values()),
        "data_unverified_reasons": sorted(set(reasons)),
        "unknown_row_types": daily.unknown_row_types,
        "missing_summary": missing_summary,
        "no_price_tickers": inp.no_price_tickers,
        "warnings": inp.warnings,
        "p3_control": p3_control,
        "windows": analysis["windows"],
        "alerts": analysis.get("alerts", {}),
        "violation_registry": analysis.get("violation_registry", []),
    }
    series = []
    for i, d in enumerate(axis):
        n = daily.navs[d]
        series.append(
            {
                "date": d,
                "nav_pln": n.value,
                "nav_by_account_pln": {f"{g}/{c}": v for (g, c), v in sorted(n.by_account_pln.items())},
                "complete": n.complete and flows[i] is not None,
                "missing": [f"{t}:{w}" for t, w in n.missing] + [f"{t}:{w}" for t, w in daily.flow_issues.get(d, [])],
                "F_D": flows[i],
                "r": analysis["r"][i],
                "I_sat": analysis["I_sat"][i],
                "B_sp": bench["sp500"][i],
                "B_spyi": bench["spyi"][i],
                "Q_sp": analysis["Q"].get("sp500", [None] * len(axis))[i],
                "Q_spyi": analysis["Q"].get("spyi", [None] * len(axis))[i],
            }
        )
    payload = dict(report)
    payload["series"] = series
    (out / "satellite.json").write_text(json.dumps(_clean(payload), ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "satellite.md").write_text(render_markdown(report), encoding="utf-8")
    return report

"""Ocena satelity jako calosci (brief CC-OS2, E3; dokument projektowy §21.6,
M78-M89, T39-T44, §19.4, T27).

Uklad:
  (A) rdzen czysty (bez bazy i sieci): klasyfikator przeplywow, przeplyw
      zewnetrzny, NAV z gotowych wejsc, TWR, delta_w, obsuniecia, mandat,
      alerty, werdykt, regresja tygodniowa;
  (B) warstwa ladowania `load_inputs` (tylko SELECT) + skladanie wejsc
      do NAV (`compute_daily`, czyste przy gotowym `Inputs`);
  (C) `compute_satellite` (wspolne liczenie), `run_satellite` (CLI, zapis
      satellite.json / satellite.md) i `run_satellite_stage` (etap cyklu,
      B-38B: rejestr 1 od S1, rejestr 2 od T42, rejestr trwaly M89).

Konwencje: kwoty w PLN (float), kursy FX = NBP A, ostatni z data <= D (M1,
M77); ceny = prices_daily.close_split_adj x czynnik warstwy
`risk.layer_factor_after` (S3); regula ceny na D = `risk.resolve_price_on_d`
(T27). Zapis do bazy wylacznie w `persist_mandate_registry` (etap cyklu, tabele
rejestru M89); poza tym tylko SELECT. Zadnych per-pozycyjnych ilosci/kosztow w
wynikach (tylko agregaty i tickery z przyczyna niekompletnosci).
"""

from __future__ import annotations

import bisect
import calendar
import csv
import json
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, Sequence

from mannaz import risk as _risk
from mannaz.calendar_check import EXCHANGE_TO_CALENDAR_CODE
from mannaz.provenance import code_sha as _code_sha

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
MODEL_SHARE_LIMIT = 0.10  # T45: dzienny udzial wyceny modelowej w NAV powyzej -> dane niezweryfikowane
MODEL_POINT_ROW_TYPES = ("kupno", "sprzedaz", "zamiana_wydanie", "zamiana_przyjecie")
NEAR_LIMIT_PP = 0.01  # skladnik mandatu blizej limitu niz 1 pp -> wynik wrazliwy na wycene modelowa
S1 = date(2023, 10, 12)  # rejestr 1: start indeksu satelity
# Rejestr 2 (rew. 4.17, decyzja ownera 2026-10-14, raport-B38B P1): te same wielkosci co
# rejestr 1, liczone od T42; werdykt M83 dla rejestru 2 nieobecny do T42 + 24 m.
T42 = date(2026, 9, 30)
REGISTRY2_VERDICT_MONTHS = 24
BENCHMARK_SOURCE = "prices_daily.close_split_adj (warstwa split_adj, ciagla po splitach; T27)"
# mapowanie skladnikow mandatu: nazwa w analizie -> skladnik rejestru M89 (tabele satellite_mandate_*)
COMPONENT_BY_MANDATE_KEY = {"max_dd_abs": "abs", "max_dd_rel_sp500": "rel_sp500", "max_dd_rel_spyi": "rel_spyi"}

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
    "wymiana_waluty": "wewnetrzny",  # przewalutowanie na tym samym rachunku (B-63)
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
    "oplata_wymiana_waluty": "koszt",
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
    price_status: str  # 'ok' | 'stale' | 'incomplete' | 'model' (T45)
    price_reason: str | None
    quote_fx: float | None  # kurs waluty notowania -> PLN
    settle_fx: float | None  # kurs waluty rozliczenia -> PLN
    is_core: bool = False
    instrument_id: int | None = None  # B-38a: klucz skladnikow atrybucji
    quote_currency: str | None = None


@dataclass
class PositionValue:
    """B-38a (H1): skladniki wyceny pozycji satelity zwrocone przez `nav_for_day`
    (ta sama wycena co NAV; atrybucja nie wycenia drugi raz)."""

    value_pln: float
    price: float  # cena Valuer z pence
    quote_fx: float  # kurs waluty notowania -> PLN
    quote_currency: str | None
    status: str  # ok | stale | model
    ticker: str
    instrument_id: int | None


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
    model_value_pln: float = 0.0  # T45: czesc NAV z wyceny modelowej (pozycje satelity)
    model_tickers: list[str] = field(default_factory=list)
    # B-38a: skladniki per pozycja satelity: (grupa, instrument_id|ticker, waluta rozliczenia) -> PositionValue
    position_values: dict[tuple[str, Any, str], PositionValue] = field(default_factory=dict)
    kontraktowy_pln: float | None = None  # wartosc KONTRAKTOWY uzyta w NAV (gdy uzyta)

    @property
    def model_share(self) -> float | None:
        """Udzial wyceny modelowej w NAV (None gdy NAV niepelny albo <= 0)."""
        if self.value is None or self.value <= 0:
            return None
        return self.model_value_pln / self.value


def model_curve(
    points: Sequence[tuple[date, float, float]], first_quote: tuple[date, float] | None = None
) -> tuple[list[date], list[float]]:
    """T45: punkty wyceny modelowej (data, ilosc, cena w warstwie split_adj i
    walucie notowania). Kilka punktow jednego dnia -> srednia wazona |ilosc|.
    `first_quote` (data, cena) pierwszego notowania zamyka krzywa; punkty od
    tej daty sa pomijane (dalej obowiazuja notowania)."""
    by_day: dict[date, list[tuple[float, float]]] = {}
    for d, qty, price in points:
        if first_quote is not None and d >= first_quote[0]:
            continue
        if not qty or not price or price <= 0:
            continue
        by_day.setdefault(d, []).append((abs(qty), price))
    dates = sorted(by_day)
    prices = [sum(q * p for q, p in by_day[d]) / sum(q for q, _ in by_day[d]) for d in dates]
    if first_quote is not None:
        dates.append(first_quote[0])
        prices.append(first_quote[1])
    return dates, prices


def model_price_on(curve: tuple[list[date], list[float]], d: date) -> float | None:
    """T45: cena modelowa na D — odcinkami liniowo miedzy kolejnymi punktami
    (po dniach kalendarzowych). Przed pierwszym i za ostatnim punktem -> None
    (bez ekstrapolacji)."""
    dates, prices = curve
    k = bisect.bisect_right(dates, d)
    if k == 0:
        return None
    if dates[k - 1] == d:
        return prices[k - 1]
    if k == len(dates):
        return None
    d0, d1 = dates[k - 1], dates[k]
    return prices[k - 1] + (prices[k] - prices[k - 1]) * (d - d0).days / (d1 - d0).days


def nav_on(cur: Any, d: date, calendar_facts_fn: Callable[[str | None, date], Any] = _risk._default_calendar_facts) -> NavResult:
    """Wspolna definicja NAV_D dla satellite i risk (B-17, M78): NAV satelity
    z par. 21.6 na jeden dzien D. Tylko SELECT (bez sieci: symbol_map=None,
    out_dir=None); instrumenty bez cen daja NAV niepelny."""
    inp = load_inputs(cur, d, d, None, None, extra_days=[d])
    return compute_daily(inp, calendar_facts_fn).navs[d]


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
    model_pln = 0.0
    model_tickers: list[str] = []
    pos_values: dict[tuple[str, Any, str], PositionValue] = {}
    kon_used: float | None = None

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
        elif p.price_status == "model":
            model_pln += value_pln
            model_tickers.append(p.ticker)
        by_pln[key] = by_pln.get(key, 0.0) + value_pln
        pv_key = (key[0], p.instrument_id if p.instrument_id is not None else p.ticker, p.currency)
        prev_pv = pos_values.get(pv_key)
        if prev_pv is None:
            pos_values[pv_key] = PositionValue(
                value_pln, p.price, p.quote_fx, p.quote_currency, p.price_status, p.ticker, p.instrument_id
            )
        else:
            prev_pv.value_pln += value_pln
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
        kon_used = kontraktowy_pln
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
        model_value_pln=model_pln,
        model_tickers=sorted(model_tickers),
        position_values=pos_values,
        kontraktowy_pln=kon_used,
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
    kontraktowy_rows: list[dict[str, Any]]  # `risk.read_kontraktowy_rows` (<= d_to)
    instruments: dict[int, InstrumentInfo]
    prices: dict[int, tuple[list[date], list[float]]]  # close_split_adj, sort. po dacie
    layer_events: dict[int, list[dict[str, Any]]]  # {'date','ratio'} (split/reverse_split)
    fx: dict[str, tuple[list[date], list[float]]]  # waluta -> (daty, kursy NBP A)
    positions_by_day: dict[date, list[dict[str, Any]]]  # positions_as_of per dzien compute_days
    warnings: list[str] = field(default_factory=list)
    no_price_tickers: list[str] = field(default_factory=list)
    # T45: krzywe wyceny modelowej (split_adj, waluta notowania) dla dni przed pierwszym notowaniem
    model_curves: dict[int, tuple[list[date], list[float]]] = field(default_factory=dict)


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
    FX (fx_nbp) i wiersze KONTRAKTOWY (`risk.read_kontraktowy_rows`) — raz; pozycje
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

    kontraktowy_rows = _risk.read_kontraktowy_rows(cur, d_to)

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

    model_curves = build_model_curves(
        tx_rows, instruments, prices, layer_events, fx, positions_by_day, held_ids
    )

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
        model_curves=model_curves,
    )


def build_model_curves(
    tx_rows: Sequence[TxRow],
    instruments: dict[int, InstrumentInfo],
    prices: dict[int, tuple[list[date], list[float]]],
    layer_events: dict[int, list[dict[str, Any]]],
    fx: dict[str, tuple[list[date], list[float]]],
    positions_by_day: dict[date, list[dict[str, Any]]],
    held_ids: set[int],
) -> dict[int, tuple[list[date], list[float]]]:
    """T45: krzywe wyceny modelowej dla instrumentow trzymanych w oknie (bez
    kontraktow). Punkty: ceny transakcyjne (MODEL_POINT_ROW_TYPES), wykup
    certyfikatow = kwota / ilosc trzymana w poprzednim dniu liczenia, oraz
    pierwsze notowanie jako punkt koncowy. Ceny przeliczane do waluty
    notowania (NBP A z dnia punktu) i do warstwy split_adj (/ layer_factor)."""
    pos_days = sorted(positions_by_day)
    points: dict[int, list[tuple[date, float, float]]] = {}
    for r in tx_rows:
        iid = r.instrument_id
        if iid is None or iid not in held_ids or iid not in instruments:
            continue
        info = instruments[iid]
        if info.instrument_type == "future":
            continue
        if r.row_type in MODEL_POINT_ROW_TYPES and r.qty and r.price:
            qty, price = r.qty, r.price
        elif r.row_type == "wykup_certyfikatow":
            k = bisect.bisect_left(pos_days, r.date)
            if k == 0:
                continue
            qty = sum(float(p["qty"]) for p in positions_by_day[pos_days[k - 1]] if p["instrument_id"] == iid)
            if not qty:
                continue
            price = r.amount / qty
        else:
            continue
        quote = info.quote_currency or r.currency
        if quote != r.currency:
            f_tx, f_q = fx_on(fx, r.currency, r.date), fx_on(fx, quote, r.date)
            if f_tx is None or not f_q:
                continue
            price = price * f_tx / f_q
        layer = float(_risk.layer_factor_after(layer_events.get(iid, []), r.date))
        points.setdefault(iid, []).append((r.date, qty, price / layer))
    curves: dict[int, tuple[list[date], list[float]]] = {}
    for iid, pts in points.items():
        info = instruments[iid]
        series = prices.get(iid)
        first = None
        if series and series[0]:
            pence = float(_risk.gbp_pence_factor(info.quote_currency, info.yahoo_symbol))
            first = (series[0][0], series[1][0] * pence)
        curve = model_curve(pts, first)
        if curve[0] and (first is None or curve[0][0] < first[0]):
            curves[iid] = curve
    return curves


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
        """(cena z pence, status ok/stale/incomplete/model, przyczyna). Wycena
        modelowa (T45) wylacznie przed pierwszym notowaniem instrumentu albo gdy
        notowan nie ma wcale; instrument z notowaniem <= D nigdy jej nie dostaje."""
        info = self.inp.instruments[iid]
        series = self.inp.prices.get(iid)
        if not series or not series[0] or d < series[0][0]:
            curve = self.inp.model_curves.get(iid)
            mp = model_price_on(curve, d) if curve else None
            if mp is not None:
                return mp, "model", None
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
            instrument_id=iid,
            quote_currency=qcur,
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


def benchmark_series(inp: Inputs) -> tuple[dict[str, dict[date, float] | None], dict[str, str]]:
    """Progi z `prices_daily` ta sama sciezka ceny co pozycje (`Inputs.prices` =
    close_split_adj, jak w `Valuer.price`; mnoznik pence jak tam). Instrument
    progu = instrument o `yahoo_symbol` z BENCHMARK_SYMBOLS (najnizszy id). Czynnika
    warstwy NIE mnozymy: `layer_factor_after` przelicza ILOSC na warstwe split_adj
    (S3), a `close_split_adj` jest juz ciagla w czasie (split SPYI uwzgledniony
    przez dostawce); wymnozenie dawaloby cene surowa z skokiem w dniu splitu.
    Zwraca (ceny EUR per prog | None, przyczyny braku per prog)."""
    ymap = build_yahoo_map(inp.instruments)
    out: dict[str, dict[date, float] | None] = {}
    why: dict[str, str] = {}
    for key, sym in BENCHMARK_SYMBOLS.items():
        iid = ymap.get(sym)
        if iid is None:
            out[key], why[key] = None, f"prog {sym}: brak instrumentu w instruments"
            continue
        series = inp.prices.get(iid)
        if not series or not series[0]:
            out[key], why[key] = None, f"prog {sym}: brak cen w prices_daily"
            continue
        info = inp.instruments[iid]
        pence = float(_risk.gbp_pence_factor(info.quote_currency, info.yahoo_symbol))
        out[key] = {d: c * pence for d, c in zip(series[0], series[1])}
    return out, why


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
    near = sorted(k for k, v in comps.items() if v is not None and abs(limits[k] - v) < NEAR_LIMIT_PP)
    out["mandate"] = {"status": ms.status, "violations": ms.violations, "missing": ms.missing, "components": comps,
                      "near_limit": near}
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
        dd_by_date = dict(zip(axis, dd))
        alerts[comp] = {
            "limit": lim,
            "current_drawdown": current_drawdown(ser),
            "state": state,
            "events": [{"date": d.isoformat(), "level": lv, "dd": dd_by_date[d]} for d, lv in events],
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
            registry.append(
                {
                    "window_end": axis[i].isoformat(),
                    "violations": ms.violations,
                    "window_start": axis[a].isoformat(),
                    "components": comps,
                }
            )
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
# (B3) Atrybucja M85 (B-38a) — czysta, na gotowych NavResult/Inputs
# ---------------------------------------------------------------------------

ATTRIB_DAILY_TOL = 0.01  # decyzja 4: |roznica dzienna| > 0,01 PLN -> naruszenie
ATTRIB_CUM_TOL = 1.00  # decyzja 4: |roznica skumulowana| > 1,00 PLN -> naruszenie
ATTRIB_FEE_ROW_TYPES = (
    "oplata_rachunek", "oplata_prowadzenie_rachunku", "oplata_przechowanie", "oplata_wymiana_waluty",
)
ATTRIB_CROSS = (("by_account", "account"), ("by_emitent", "emitent"), ("by_currency", "currency"))
ATTRIB_POSITIONAL_KINDS = ("pozycja", "kontrakt")
ATTRIB_CONVENTIONS = (
    "PLN; wycena = NAV (nav_for_day/Valuer); FX = NBP A <= D; wiersze w walucie rozliczenia po NBP A "
    "z daty wiersza; lokalny zawiera transakcje_dnia"
)


class _DayIncomplete(Exception):
    """Brak skladnika dnia (kurs, cena) — atrybucja dnia = None."""


@dataclass
class AttribLine:
    """Atomowa linia atrybucji jednego dnia (PLN). `total` = lokalny + fx;
    `lokalny` zawiera `transakcje_dnia`."""

    kind: str  # pozycja | kontrakt | gotowka | oplaty | koszty_nieprzypisane | przelew | kontraktowy_nierozbite
    account: str  # grupa rachunku
    emitent: str  # etykieta
    emitent_key: Any
    currency: str
    total: float = 0.0
    lokalny: float = 0.0
    fx: float = 0.0
    transakcje_dnia: float = 0.0
    model: bool = False
    n_rows: int = 0
    instrument: str | None = None
    kontrakt_bez_bazy: bool = False  # A1: future bez base_symbol / bez bazy w instrumentach


def build_yahoo_map(instruments: dict[int, InstrumentInfo]) -> dict[str, int]:
    """yahoo_symbol -> id instrumentu (pierwszy id w kolejnosci rosnacej przy duplikatach)."""
    out: dict[str, int] = {}
    for iid in sorted(instruments):
        ys = instruments[iid].yahoo_symbol
        if ys and ys not in out:
            out[ys] = iid
    return out


def _base_id_for(
    info: InstrumentInfo, instruments: dict[int, InstrumentInfo], yahoo_map: dict[str, int]
) -> int | None:
    """Id instrumentu bazowego kontraktu (`yahoo_symbol == base_symbol`) albo None."""
    base_id = yahoo_map.get(info.base_symbol) if info.base_symbol else None
    return base_id if base_id is not None and base_id in instruments else None


def emitent_key_for(
    iid: int, instruments: dict[int, InstrumentInfo], yahoo_map: dict[str, int]
) -> tuple[Any, str]:
    """(klucz, etykieta) emitenta wg wspolnej reguly `risk.emitent_key` (§19.2):
    equity/etf — wlasny id; future — id instrumentu o yahoo_symbol == base_symbol
    kontraktu; brak bazy -> wlasny id i etykieta = ticker serii (flaga:
    `is_future_no_base`). Tu tylko rozwiazanie bazy z `instruments`."""
    info = instruments.get(iid)
    if info is None:
        return iid, f"instrument {iid}"
    key = _risk.emitent_key(iid, info.instrument_type, _base_id_for(info, instruments, yahoo_map))
    return key, instruments[key].ticker


def is_future_no_base(iid: Any, instruments: dict[int, InstrumentInfo], yahoo_map: dict[str, int]) -> bool:
    """A1: future bez base_symbol albo z baza nieznaleziona w instrumentach."""
    info = instruments.get(iid) if isinstance(iid, int) else None
    if info is None or info.instrument_type != "future":
        return False
    return _base_id_for(info, instruments, yahoo_map) is None


def _emitent_of(key_id: Any, instruments: dict[int, InstrumentInfo], yahoo_map: dict[str, int]) -> tuple[Any, str]:
    if isinstance(key_id, int):
        return emitent_key_for(key_id, instruments, yahoo_map)
    return key_id, str(key_id)


def _acc(d: dict[Any, list[float]], key: Any, val: float, n: int = 0) -> None:
    cell = d.setdefault(key, [0.0, 0])
    cell[0] += val
    cell[1] += n


def attribute_day(
    nav_p: NavResult,
    nav_d: NavResult,
    rows: Sequence[TxRow],
    kon_rows: Sequence[dict[str, Any]],
    fx_fn: FxFn,
    noncash_value: NonCashValueFn,
    instruments: dict[int, InstrumentInfo],
    yahoo_map: dict[str, int],
) -> list[AttribLine] | None:
    """Atomowe linie atrybucji dnia d wzgledem p (czysta). `rows`: wiersze
    transakcji z (p, d]; `kon_rows`: wiersze KONTRAKTOWY z (p, d]. Linie liczone
    niezaleznie z pozycji/gotowki/wierszy; tozsamosc to kontrola, nie plug.
    None, gdy NAV(p)/NAV(d) = None, brak kursu/ceny albo nieznany row_type."""
    if nav_p.value is None or nav_d.value is None:
        return None
    try:
        return _attribute_day(nav_p, nav_d, rows, kon_rows, fx_fn, noncash_value, instruments, yahoo_map)
    except (_DayIncomplete, ValueError):  # UnknownRowTypeError jest ValueError
        return None


def _attribute_day(
    nav_p: NavResult,
    nav_d: NavResult,
    rows: Sequence[TxRow],
    kon_rows: Sequence[dict[str, Any]],
    fx_fn: FxFn,
    noncash_value: NonCashValueFn,
    instruments: dict[int, InstrumentInfo],
    yahoo_map: dict[str, int],
) -> list[AttribLine]:
    non_cash_types = _risk.NON_CASH_ROW_TYPES
    pos_flow: dict[tuple[str, Any, str], float] = {}
    pos_hit: set[tuple[str, Any, str]] = set()
    cash_rows: dict[tuple[str, str], float] = {}
    other: dict[tuple[str, str, str], list[float]] = {}

    for r in rows:
        grp = account_group(r.rachunek)
        cls = classify(r.row_type, r.is_core)
        if grp == KONTRAKTOWY_GROUP:
            continue  # KONTRAKTOWY idzie przez `kon_rows`
        if r.row_type == "wymiana_waluty":
            continue  # obie nogi zostaja w liniach gotowki (rachunek, waluta); spread = ich suma (B-63)
        non_cash = r.row_type in non_cash_types
        x = 1.0
        if not non_cash:
            rate = fx_fn(r.currency, r.date)
            if rate is None:
                raise _DayIncomplete(f"brak FX {r.currency}")
            x = rate
            cash_rows[(grp, r.currency)] = cash_rows.get((grp, r.currency), 0.0) + r.amount * x
        if cls == "zewnetrzny":
            if r.row_type == "bilans_otwarcia" and not r.is_core and r.instrument_type != "future":
                v = noncash_value(r)
                if v is None or r.instrument_id is None:
                    raise _DayIncomplete("bilans_otwarcia bez wartosci")
                k = (grp, r.instrument_id, r.currency)
                pos_flow[k] = pos_flow.get(k, 0.0) - v
                pos_hit.add(k)
            continue  # przeplyw zewnetrzny siedzi w F
        val = 0.0 if non_cash else r.amount * x
        if r.row_type == "przelew_wewnetrzny":
            _acc(other, ("przelew", grp, r.currency), val)
        elif r.row_type in ATTRIB_FEE_ROW_TYPES:
            _acc(other, ("oplaty", grp, r.currency), val)
        elif r.instrument_id is not None:
            k = (grp, r.instrument_id, r.currency)
            pos_flow[k] = pos_flow.get(k, 0.0) + val
            pos_hit.add(k)
        else:  # koszty bez instrumentu i wszystko niepokryte — nigdy po cichu
            _acc(other, ("koszty_nieprzypisane", grp, r.currency), val, 1)

    lines: list[AttribLine] = []

    # --- pozycje ---
    keys = set(nav_p.position_values) | set(nav_d.position_values) | pos_hit
    for k in sorted(keys, key=lambda t: (t[0], str(t[1]), t[2])):
        grp, kid, scur = k
        pv_p = nav_p.position_values.get(k)
        pv_d = nav_d.position_values.get(k)
        v_p = pv_p.value_pln if pv_p else 0.0
        v_d = pv_d.value_pln if pv_d else 0.0
        w = v_d - v_p + pos_flow.get(k, 0.0)
        c_lok = c_fx = 0.0
        if pv_p is not None and pv_d is not None and pv_p.price and pv_p.quote_fx:
            r_l = pv_d.price / pv_p.price - 1.0
            r_fx = pv_d.quote_fx / pv_p.quote_fx - 1.0
            c_lok = v_p * (r_l + 0.5 * r_l * r_fx)
            c_fx = v_p * (r_fx + 0.5 * r_l * r_fx)
        td = w - c_lok - c_fx
        ekey, elabel = _emitent_of(kid, instruments, yahoo_map)
        info = instruments.get(kid) if isinstance(kid, int) else None
        pv = pv_d or pv_p
        qcur = (pv.quote_currency if pv else None) or (info.quote_currency if info else None) or scur
        ticker = pv.ticker if pv else (info.ticker if info else str(kid))
        lines.append(AttribLine(
            kind="pozycja", account=grp, emitent=elabel, emitent_key=ekey, currency=qcur,
            total=w, lokalny=c_lok + td, fx=c_fx, transakcje_dnia=td,
            model=bool((pv_p and pv_p.status == "model") or (pv_d and pv_d.status == "model")),
            instrument=ticker, kontrakt_bez_bazy=is_future_no_base(kid, instruments, yahoo_map),
        ))

    # --- gotowka AKCYJNY/ZAGRANICZNY ---
    cash_keys = {k for k in set(nav_p.cash_native) | set(nav_d.cash_native) if k[0] != KONTRAKTOWY_GROUP}
    for g, c in sorted(cash_keys):
        s_p = nav_p.cash_native.get((g, c), 0.0)
        s_d = nav_d.cash_native.get((g, c), 0.0)
        if c == "PLN":
            x_p = x_d = 1.0
        else:
            x_p, x_d = nav_p.rates.get(c), nav_d.rates.get(c)
            if (s_p != 0 and (x_p is None or x_d is None)) or (s_d != 0 and x_d is None):
                raise _DayIncomplete(f"brak kursu {c}")
        c_fx = s_p * (x_d - x_p) if s_p != 0 else 0.0
        val_p = s_p * x_p if s_p != 0 else 0.0
        val_d = s_d * x_d if s_d != 0 else 0.0
        rest = val_d - val_p - c_fx - cash_rows.get((g, c), 0.0)
        lines.append(AttribLine(
            kind="gotowka", account=g, emitent=f"gotowka {g}/{c}", emitent_key=f"gotowka {g}/{c}",
            currency=c, total=c_fx + rest, lokalny=rest, fx=c_fx, transakcje_dnia=0.0,
        ))

    # --- przelewy, oplaty, koszty nieprzypisane (satelita) ---
    labels = {"przelew": "przelewy wewnetrzne"}
    for (kind, g, c), (val, n) in sorted(other.items()):
        label = labels.get(kind) or (f"oplaty {g}" if kind == "oplaty" else f"koszty nieprzypisane {g}")
        lines.append(AttribLine(
            kind=kind, account=g, emitent=label, emitent_key=label, currency=c,
            total=val, lokalny=val, n_rows=int(n),
        ))

    # --- KONTRAKTOWY ---
    if kon_rows:
        by_ticker = {i.ticker: iid for iid, i in sorted(instruments.items(), reverse=True)}
        kon_przelew = 0.0
        kon_nierozbite = 0.0
        kon_n_nierozbite = 0
        kon_contract: dict[int, float] = {}
        has_przelew = False
        for row in kon_rows:
            cls = classify(row["row_type"])
            if cls == "zewnetrzny":
                continue
            c = float(_risk.kontraktowy_account_value([row], row["transaction_date"]))
            if row["row_type"] == "przelew_wewnetrzny":
                kon_przelew += c
                has_przelew = True
            else:
                iid = by_ticker.get(row.get("broker_ticker")) if row.get("broker_ticker") else None
                if iid is None:
                    kon_nierozbite += c
                    kon_n_nierozbite += 1
                else:
                    kon_contract[iid] = kon_contract.get(iid, 0.0) + c
        if has_przelew:
            lines.append(AttribLine(
                kind="przelew", account=KONTRAKTOWY_GROUP, emitent="przelewy wewnetrzne",
                emitent_key="przelewy wewnetrzne", currency="PLN", total=kon_przelew, lokalny=kon_przelew,
            ))
        for iid in sorted(kon_contract):
            ekey, elabel = emitent_key_for(iid, instruments, yahoo_map)
            val = kon_contract[iid]
            lines.append(AttribLine(
                kind="kontrakt", account=KONTRAKTOWY_GROUP, emitent=elabel, emitent_key=ekey, currency="PLN",
                total=val, lokalny=val, instrument=instruments[iid].ticker,
                kontrakt_bez_bazy=is_future_no_base(iid, instruments, yahoo_map),
            ))
        if kon_n_nierozbite:
            lines.append(AttribLine(
                kind="kontraktowy_nierozbite", account=KONTRAKTOWY_GROUP, emitent="KONTRAKTOWY (nierozbite)",
                emitent_key="KONTRAKTOWY (nierozbite)", currency="PLN", total=kon_nierozbite,
                lokalny=kon_nierozbite, n_rows=kon_n_nierozbite,
            ))
    return lines


def identity_diff(
    lines: Sequence[AttribLine] | None, nav_p_value: float | None, nav_d_value: float | None, flow: float | None
) -> float | None:
    """Kontrola tozsamosci dnia (bez pluga): sum(total linii) - (NAV(d) - NAV(p) - F).
    None, gdy brak linii albo NAV/F niedostepne."""
    if lines is None or nav_p_value is None or nav_d_value is None or flow is None:
        return None
    return sum(ln.total for ln in lines) - (nav_d_value - nav_p_value - flow)


def _empty_cell() -> dict[str, Any]:
    return {"total": 0.0, "lokalny": 0.0, "fx": 0.0, "transakcje_dnia": 0.0,
            "model": False, "n_rows": 0, "positional": False, "bez_bazy": False}


def _merge_cells(acc: dict[str, dict[str, Any]], agg: dict[str, dict[str, Any]]) -> None:
    for k, v in agg.items():
        a = acc.setdefault(k, _empty_cell())
        for f in ("total", "lokalny", "fx", "transakcje_dnia", "n_rows"):
            a[f] += v[f]
        a["model"] = a["model"] or v["model"]
        a["positional"] = a["positional"] or v["positional"]
        a["bez_bazy"] = a["bez_bazy"] or v["bez_bazy"]


def aggregate_lines(lines: Sequence[AttribLine], key: str) -> dict[str, dict[str, Any]]:
    """Przekroj: suma linii po `key` in {account, emitent, currency}."""
    if key not in ("account", "emitent", "currency"):
        raise ValueError(key)
    out: dict[str, dict[str, Any]] = {}
    for ln in lines:
        a = out.setdefault(getattr(ln, key), _empty_cell())
        a["total"] += ln.total
        a["lokalny"] += ln.lokalny
        a["fx"] += ln.fx
        a["transakcje_dnia"] += ln.transakcje_dnia
        a["model"] = a["model"] or ln.model
        a["n_rows"] += ln.n_rows
        a["positional"] = a["positional"] or ln.kind in ATTRIB_POSITIONAL_KINDS
        a["bez_bazy"] = a["bez_bazy"] or ln.kontrakt_bez_bazy
    return out


def kontraktowy_ext_int(kon_rows: Sequence[dict[str, Any]]) -> float:
    """A2: suma wkladow wierszy KONTRAKTOWY klasy 'zewnetrzny' oraz przelew_wewnetrzny
    (te nie sa w liniach kontraktowych), wg `risk.kontraktowy_account_value`."""
    tot = 0.0
    for row in kon_rows:
        if classify(row["row_type"]) == "zewnetrzny" or row["row_type"] == "przelew_wewnetrzny":
            tot += float(_risk.kontraktowy_account_value([row], row["transaction_date"]))
    return tot


def _line_nonzero(ln: AttribLine) -> bool:
    return bool(ln.total or ln.lokalny or ln.fx or ln.transakcje_dnia)


def _cross_rows(
    cells: dict[str, dict[str, Any]], held_labels: set[str] | None = None
) -> list[dict[str, Any]]:
    rows = []
    for k, v in cells.items():
        row = {"key": k, "total": v["total"], "lokalny": v["lokalny"], "fx": v["fx"],
               "transakcje_dnia": v["transakcje_dnia"], "model": v["model"], "n_rows": v["n_rows"]}
        if held_labels is not None:
            row["closed"] = bool(v["positional"] and k not in held_labels)
            row["kontrakt_bez_bazy"] = bool(v["bez_bazy"])
        rows.append(row)
    rows.sort(key=lambda r: (-r["total"], r["key"]))
    return rows


def compute_attribution(
    inp: Inputs,
    daily: DailyResult,
    axis: Sequence[date],
    windows: dict[str, Any],
    calendar_facts_fn: Callable[[str | None, date], Any] = _risk._default_calendar_facts,
) -> dict[str, Any]:
    """Atrybucja M85 na osi (czysta przy gotowych Inputs/DailyResult): linie
    dzienne z tych samych okien wierszy (poprzedni dzien osi, D] co F, agregacja
    do okien i przekrojow, kontrola tozsamosci (nie rzuca wyjatku — naruszenia
    w `identity.*.violations`). Zwraca {'attribution': ..., 'attribution_daily': ...}."""
    valuer = Valuer(inp, calendar_facts_fn)
    yahoo_map = build_yahoo_map(inp.instruments)
    fx_fn: FxFn = lambda c, d: fx_on(inp.fx, c, d)  # noqa: E731
    rows = inp.tx_rows
    row_dates = [r.date for r in rows]
    kon = sorted(inp.kontraktowy_rows, key=lambda r: r["transaction_date"])
    kon_dates = [r["transaction_date"] for r in kon]

    day_lines: dict[date, list[AttribLine] | None] = {}
    day_expected: dict[date, float | None] = {}
    day_cells: dict[date, dict[str, dict[str, dict[str, Any]]]] = {}
    diffs: dict[str, dict[date, float]] = {name: {} for name, _ in ATTRIB_CROSS}
    kdiffs: dict[date, float] = {}
    daily_out: list[dict[str, Any]] = []
    n_none = 0
    for i in range(1, len(axis)):
        p, d = axis[i - 1], axis[i]
        nav_p, nav_d = daily.navs[p], daily.navs[d]
        f = daily.flows[d]
        expected = (nav_d.value - nav_p.value - f) if (
            nav_d.value is not None and nav_p.value is not None and f is not None) else None
        lines: list[AttribLine] | None = None
        if expected is not None:
            lo, hi = bisect.bisect_right(row_dates, p), bisect.bisect_right(row_dates, d)
            klo, khi = bisect.bisect_right(kon_dates, p), bisect.bisect_right(kon_dates, d)
            lines = attribute_day(nav_p, nav_d, rows[lo:hi], kon[klo:khi], fx_fn, valuer.noncash_value,
                                  inp.instruments, yahoo_map)
        day_lines[d] = lines
        day_expected[d] = expected
        if lines is None:
            n_none += 1
            daily_out.append({"date": d.isoformat(), "nav_change_minus_flow": expected, "kontraktowy_diff": None, "lines": None})
            continue
        kon_diff: float | None = None
        try:
            k_lines = sum(ln.total for ln in lines if ln.account == KONTRAKTOWY_GROUP and ln.kind != "przelew")
            k_exp = ((nav_d.kontraktowy_pln or 0.0) - (nav_p.kontraktowy_pln or 0.0)
                     - kontraktowy_ext_int(kon[klo:khi]))
            kon_diff = k_lines - k_exp
            kdiffs[d] = kon_diff
        except ValueError:
            kon_diff = None
        cells = {name: aggregate_lines(lines, key) for name, key in ATTRIB_CROSS}
        day_cells[d] = cells
        for name, _ in ATTRIB_CROSS:
            diffs[name][d] = sum(v["total"] for v in cells[name].values()) - expected
        daily_out.append({
            "date": d.isoformat(),
            "nav_change_minus_flow": expected,
            "kontraktowy_diff": kon_diff,
            "lines": [asdict(ln) for ln in lines if _line_nonzero(ln)],
        })

    identity: dict[str, Any] = {}
    n_days = max(len(axis) - 1, 0)
    for name, _ in ATTRIB_CROSS:
        ds = diffs[name]
        cum = 0.0
        cum_max = 0.0
        cum_first: date | None = None
        for d in sorted(ds):
            cum += ds[d]
            cum_max = max(cum_max, abs(cum))
            if cum_first is None and abs(cum) > ATTRIB_CUM_TOL:
                cum_first = d
        identity[name] = {
            "max_abs_daily_diff": max((abs(v) for v in ds.values()), default=0.0),
            "sum_daily_diff": sum(ds.values()),
            "max_abs_cumulative_diff": cum_max,
            "cumulative_violation_first_day": cum_first.isoformat() if cum_first else None,
            "n_days": n_days,
            "n_days_none": n_none,
            "violations": [d.isoformat() for d in sorted(ds) if abs(ds[d]) > ATTRIB_DAILY_TOL],
        }

    identity["kontraktowy"] = {
        "max_abs_daily_diff": max((abs(v) for v in kdiffs.values()), default=0.0),
        "sum_daily_diff": sum(kdiffs.values()),
        "n_days": len(kdiffs),
        "violations": [d.isoformat() for d in sorted(kdiffs) if abs(kdiffs[d]) > ATTRIB_DAILY_TOL],
    }

    axis_idx = {d: i for i, d in enumerate(axis)}
    win_out: dict[str, Any] = {}
    for wname, w in windows.items():
        a, t = axis_idx[date.fromisoformat(w["from"])], axis_idx[date.fromisoformat(w["to"])]
        days = [axis[j] for j in range(a + 1, t + 1)]
        acc: dict[str, dict[str, dict[str, Any]]] = {name: {} for name, _ in ATTRIB_CROSS}
        none_days = [d for d in days if day_lines.get(d) is None]
        bez_bazy: dict[Any, float] = {}
        for d in days:
            for ln in day_lines.get(d) or []:
                if ln.kontrakt_bez_bazy and _line_nonzero(ln):
                    bez_bazy[ln.emitent_key] = bez_bazy.get(ln.emitent_key, 0.0) + ln.total
        for d in days:
            if d in day_cells:
                for name, _ in ATTRIB_CROSS:
                    _merge_cells(acc[name], day_cells[d][name])
        complete = not none_days
        nav_a, nav_t = daily.navs[axis[a]].value, daily.navs[axis[t]].value
        flows_w = [daily.flows[d] for d in days]
        nav_change = (nav_t - nav_a - sum(flows_w)) if (
            nav_a is not None and nav_t is not None and all(x is not None for x in flows_w)) else None
        sum_lines = sum(v["total"] for v in acc["by_account"].values())
        control: dict[str, Any] = {
            "complete": complete,
            "sum_lines": sum_lines,
            "nav_change_minus_flows": nav_change,
            "diff": (sum_lines - nav_change) if (complete and nav_change is not None) else None,
            "n_days": len(days),
            "n_days_none": len(none_days),
            "cross": {},
            "kontraktowy": {
                "max_abs_daily_diff": max((abs(kdiffs[d]) for d in days if d in kdiffs), default=0.0),
                "sum_daily_diff": sum(kdiffs[d] for d in days if d in kdiffs),
            },
        }
        for name, _ in ATTRIB_CROSS:
            wd = [diffs[name][d] for d in days if d in diffs[name]]
            control["cross"][name] = {
                "max_abs_daily_diff": max((abs(v) for v in wd), default=0.0),
                "sum_daily_diff": sum(wd),
            }
        held = _held_emitents(daily.navs[axis[t]], inp.positions_by_day.get(axis[t], []), inp.instruments, yahoo_map)
        win_out[wname] = {
            "from": w["from"], "to": w["to"], "complete": complete, "control": control,
            "kontrakty_bez_bazy": {"n_series": len(bez_bazy), "total_pln": sum(bez_bazy.values())},
            "by_account": _cross_rows(acc["by_account"]),
            "by_emitent": _cross_rows(acc["by_emitent"], held),
            "by_currency": _cross_rows(acc["by_currency"]),
        }
    attribution = {"conventions": ATTRIB_CONVENTIONS, "identity": identity, "windows": win_out}
    return {"attribution": attribution, "attribution_daily": daily_out}


def _held_emitents(
    nav: NavResult, positions: Sequence[dict[str, Any]], instruments: dict[int, InstrumentInfo],
    yahoo_map: dict[str, int],
) -> set[str]:
    """Etykiety emitentow trzymanych na dzien NAV: pozycje z NAV + otwarte kontrakty."""
    held: set[str] = set()
    for (_, kid, _), _pv in nav.position_values.items():
        held.add(_emitent_of(kid, instruments, yahoo_map)[1])
    for pos in positions:
        info = instruments.get(pos["instrument_id"])
        if info is not None and info.instrument_type == "future" and pos.get("qty"):
            held.add(emitent_key_for(pos["instrument_id"], instruments, yahoo_map)[1])
    return held


# ---------------------------------------------------------------------------
# (B4) Ryzyko portfela M86 (B-38a) — jeden dzien T; loader tylko SELECT
# ---------------------------------------------------------------------------

M86_COLUMNS = (
    "risk_date", "rachunek", "instrument_id", "settlement_currency", "qty", "close_d", "fx_rate", "risk_pln",
    "risk_pct_satellite_capital", "name_key", "position_kind", "broker_ticker", "instrument_type", "is_core",
    "multiplier", "theme",
)


def load_risk_snapshot(cur: Any, t: date) -> list[dict[str, Any]]:
    """risk_daily z ostatniego risk_date <= t (tylko SELECT). `rachunek` jest od
    razu sprowadzany do grupy (`account`); numer rachunku nie opuszcza funkcji."""
    cur.execute(
        "SELECT r.risk_date, r.rachunek, r.instrument_id, r.settlement_currency, r.qty, r.close_d, "
        "r.fx_rate, r.risk_pln, r.risk_pct_satellite_capital, r.name_key, r.position_kind, "
        "i.broker_ticker, i.instrument_type, i.is_core, i.multiplier, i.theme "
        "FROM risk_daily r JOIN instruments i ON i.id = r.instrument_id "
        "WHERE r.risk_date = (SELECT max(risk_date) FROM risk_daily WHERE risk_date <= %s)",
        (t,),
    )
    out: list[dict[str, Any]] = []
    for row in cur.fetchall():
        rec = dict(zip(M86_COLUMNS, row))
        rec["account"] = account_group(rec.pop("rachunek"))
        out.append(rec)
    return out


def _share(x: float | None, nav: float | None) -> float | None:
    return None if x is None or nav is None or nav <= 0 else x / nav


def m86_snapshot(
    rows: Sequence[dict[str, Any]],
    nav_t: NavResult,
    t: date,
    instruments: dict[int, InstrumentInfo],
    yahoo_map: dict[str, int],
) -> dict[str, Any]:
    """M86 na dzien T z gotowych wierszy risk_daily i NAV(T) (czysta, bez serii)."""
    nav_val = nav_t.value
    risk_date = rows[0]["risk_date"] if rows else None
    elig = [r for r in rows if _risk.is_risk_budget_eligible(r["instrument_type"], bool(r["is_core"]))]
    incomplete = sorted(r["broker_ticker"] for r in elig
                        if r["risk_pct_satellite_capital"] is None or r["risk_pln"] is None)
    heat_pct = None if incomplete else sum(float(r["risk_pct_satellite_capital"]) for r in elig)
    heat_pln = None if incomplete else sum(float(r["risk_pln"]) for r in elig)
    lim3 = float(_risk.LEVEL3_PCT)
    heat = {
        "heat_pct": heat_pct, "heat_pln": heat_pln, "limit_pct": lim3,
        "breach": None if heat_pct is None else heat_pct > lim3,
        "n_eligible": len(elig), "incomplete": incomplete,
    }
    lim2 = float(_risk.LEVEL2_PCT)
    themes: dict[str, dict[str, Any]] = {}
    no_theme: list[str] = []
    for r in elig:
        if not r["theme"]:
            no_theme.append(r["broker_ticker"])
            continue
        th = themes.setdefault(r["theme"], {"pct": 0.0, "tickers": [], "incomplete": False})
        if r["risk_pct_satellite_capital"] is None:
            th["incomplete"] = True
        else:
            th["pct"] += float(r["risk_pct_satellite_capital"])
        th["tickers"].append(r["broker_ticker"])
    level2 = [
        {"theme": k, "pct": v["pct"], "limit_pct": lim2, "breach": v["pct"] > lim2,
         "incomplete": v["incomplete"], "tickers": sorted(v["tickers"])}
        for k, v in sorted(themes.items())
    ]
    # kontrakty
    nominal_missing: list[str] = []
    nominals: list[float] = []
    for r in rows:
        if r["instrument_type"] != "future":
            continue
        if r["multiplier"] is None or r["qty"] is None or r["close_d"] is None or r["fx_rate"] is None:
            nominal_missing.append(r["broker_ticker"])
            continue
        sign = -1.0 if r["position_kind"] == "short" else 1.0
        nominals.append(sign * abs(float(r["qty"])) * float(r["close_d"]) * float(r["multiplier"]) * float(r["fx_rate"]))
    if nominal_missing:
        gross = net = None
    else:
        gross, net = sum(abs(x) for x in nominals), sum(nominals)
    futures = {
        "n": len(nominals) + len(nominal_missing), "nominal_gross_pln": gross, "nominal_net_pln": net,
        "gross_share_nav": _share(gross, nav_val), "net_share_nav": _share(net, nav_val),
        "missing_multiplier": sorted(nominal_missing),
    }
    # koncentracja z NAV(T)
    by_em: dict[str, float] = {}
    for (_, kid, _), pv in nav_t.position_values.items():
        lab = _emitent_of(kid, instruments, yahoo_map)[1]
        by_em[lab] = by_em.get(lab, 0.0) + pv.value_pln
    total_long = sum(by_em.values())
    weights = {k: v / total_long for k, v in by_em.items()} if total_long > 0 else {}
    top5 = sorted(by_em.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    cash_pln = 0.0
    cash_missing: list[str] = []
    for (g, c), s in nav_t.cash_native.items():
        if g == KONTRAKTOWY_GROUP:
            continue
        rate = nav_t.rates.get(c)
        if rate is None:
            if s != 0:
                cash_missing.append(f"{g}/{c}")
            continue
        cash_pln += s * rate
    concentration = {
        "hhi": sum(w * w for w in weights.values()) if weights else None,
        "top5": [{"emitent": k, "value_pln": v, "weight": weights.get(k)} for k, v in top5],
        "n_emitents": len(by_em),
        "long_equity_pln": total_long,
        "cash_pln": cash_pln, "cash_share_nav": _share(cash_pln, nav_val), "cash_missing": sorted(cash_missing),
        "kontraktowy_pln": nav_t.kontraktowy_pln, "kontraktowy_share_nav": _share(nav_t.kontraktowy_pln, nav_val),
        "futures_net_share_nav": futures["net_share_nav"],
    }
    return {
        "risk_date": risk_date.isoformat() if risk_date else None,
        "T": t.isoformat(),
        "dates_differ": risk_date is not None and risk_date != t,
        "nav_t": nav_val,
        "n_rows": len(rows),
        "heat": heat,
        "level2": level2,
        "level2_no_theme": sorted(no_theme),
        "futures": futures,
        "kontraktowy_free_vs_deposit": {
            "status": "brak pomiaru",
            "reason": "wymagany depozyt KONTRAKTOWY nie jest zapisany w bazie (P4)",
        },
        "concentration": concentration,
    }


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
        f"({BENCHMARK_SOURCE}) x EUR/PLN NBP A."
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
    mv = report.get("model_valuation") or {}
    if mv.get("n_days"):
        L.append(
            f"- wycena modelowa (T45): {mv['n_days']} dni; maks. dzienny udzial {_pct(mv['max_share'])} "
            f"({mv['max_share_date']}), sredni {_pct(mv['mean_share'])}; dni > {mv['limit'] * 100:.0f}%: "
            f"{mv['days_over_limit']}; instrumenty: {', '.join(mv['tickers'])}"
        )
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
        if m.get("near_limit"):
            L.append(f"  - skladniki < 1 pp od limitu: {', '.join(m['near_limit'])}"
                     + (" — WYNIK WRAZLIWY NA WYCENE MODELOWA" if m.get("sensitive_to_model") else ""))
        v = win["verdict"]
        L.append(f"- werdykt wyniku: {v['label']} (znaki: {v['signs']}; dane zweryfikowane: {v['data_verified']})")
    _render_attribution(L, report.get("attribution"))
    L.append("")
    L.append("## Alerty (obsuniecie biezace od S)")
    for comp, a in report.get("alerts", {}).items():
        ev = ", ".join(f"{e['date']} {e['level']}" for e in a["events"]) or "brak zdarzen"
        L.append(f"- {comp}: stan {a['state']}, obsuniecie biezace {_pct(a['current_drawdown'])} (limit {a['limit'] * 100:.0f}%); {ev}")
    L.append("")
    L.append(f"## Rejestr naruszen (okna 24 m., konce tygodni): {len(report.get('violation_registry', []))} okien z przekroczeniem")
    _render_registry2(L, report.get("registry2"))
    _render_swaps(L, report.get("swaps"))
    _render_persistence(L, report.get("persistence"))
    _render_m86(L, report.get("m86"))
    return "\n".join(L) + "\n"


def _attr_table(L: list[str], title: str, rows: Sequence[dict[str, Any]], with_flags: bool = False) -> None:
    L.append(f"{title}")
    L.append("")
    L.append("| klucz | total | lokalny | fx | transakcje_dnia |" + (" flagi |" if with_flags else ""))
    L.append("|---|---:|---:|---:|---:|" + ("---|" if with_flags else ""))
    for r in rows:
        flags = []
        if r.get("model"):
            flags.append("model")
        if r.get("closed"):
            flags.append("zamknieta")
        if r.get("kontrakt_bez_bazy"):
            flags.append("kontrakt_bez_bazy")
        if r.get("n_rows"):
            flags.append(f"n={r['n_rows']}")
        L.append(f"| {r['key']} | {_num(r['total'])} | {_num(r['lokalny'])} | {_num(r['fx'])} | "
                 f"{_num(r['transakcje_dnia'])} |" + (f" {', '.join(flags) or '-'} |" if with_flags else ""))
    L.append("")


def _render_attribution(L: list[str], att: dict[str, Any] | None) -> None:
    if not att:
        return
    for name, w in att.get("windows", {}).items():
        c = w["control"]
        L.append("")
        L.append(f"## Atrybucja M85 - okno {name}: {w['from']} .. {w['to']}")
        L.append("")
        L.append(f"- kompletne: {w['complete']} (dni None: {c['n_days_none']} z {c['n_days']}); {att.get('conventions', '')}")
        max_d = max((v["max_abs_daily_diff"] for v in c["cross"].values()), default=None)
        L.append(f"- suma linii = {_num(c['sum_lines'])} PLN; zmiana NAV minus przeplywy = {_num(c['nav_change_minus_flows'])} PLN; "
                 f"roznica = {_num(c['diff'], 4)} PLN; max |roznica dzienna| = {_num(max_d, 4)} PLN; "
                 f"kontrola KONTRAKTOWY max |roznica dzienna| = {_num(c['kontraktowy']['max_abs_daily_diff'], 4)} PLN; "
                 f"kontrakty bez bazy: {w['kontrakty_bez_bazy']['n_series']} serii, {_num(w['kontrakty_bez_bazy']['total_pln'])} PLN")
        L.append("")
        em = w["by_emitent"]
        _attr_table(L, "TOP10 emitentow (total):", em[:10], with_flags=True)
        bottom = sorted(em, key=lambda r: (r["total"], r["key"]))[:10]
        _attr_table(L, "BOTTOM10 emitentow (total):", bottom, with_flags=True)
        _attr_table(L, "Wg rachunku:", w["by_account"])
        _attr_table(L, "Wg waluty:", w["by_currency"])
    ident = att.get("identity") or {}
    viol = {k: v["violations"] for k, v in ident.items() if v.get("violations")}
    if viol:
        L.append("")
        L.append("- UWAGA tozsamosc atrybucji: naruszenia dzienne w przekrojach "
                 + "; ".join(f"{k}: {len(v)} dni (pierwszy {v[0]})" for k, v in viol.items()))


def _render_registry2(L: list[str], r2: dict[str, Any] | None) -> None:
    if not r2:
        return
    L.append("")
    L.append(f"## Rejestr 2 (od T42 = {r2['T42']})")
    if r2["status"] != "liczony":
        L.append(f"- status: {r2['status']} (D < T42); rejestr 2 nieobecny")
        return
    L.append(f"- S: {r2['S']}; werdykt M83: {r2['verdict_label']}; dane zweryfikowane: {r2['data_verified']}")
    for reason in r2["data_unverified_reasons"]:
        L.append(f"  - {reason}")
    for name, win in r2["windows"].items():
        L.append(f"- okno {name}: {win['from']} .. {win['to']} ({win['n_days']} dni; kompletne: {win['complete']}); "
                 f"TWR = {_pct(win['twr'])}; V_a = {_num(win['v_a'])} PLN; V_T = {_num(win['v_t'])} PLN")
        L.append("  - " + "; ".join(
            f"dW {b['label']} = {_num(b['delta_w_pln'])} PLN" + (" [niewykonalny]" if b["infeasible"] else "")
            for b in win["bench"].values()))
        m = win["mandate"]
        L.append(f"  - mandat: {m['status']}; naruszenia: {', '.join(m['violations']) or '-'}; "
                 f"max obsuniecia: " + ", ".join(f"{k} {_pct(v)}" for k, v in m["components"].items()))
    for comp, a in r2["alerts"].items():
        ev = ", ".join(f"{e['date']} {e['level']}" for e in a["events"]) or "brak zdarzen"
        L.append(f"- alert {comp}: stan {a['state']}, obsuniecie biezace {_pct(a['current_drawdown'])} "
                 f"(limit {a['limit'] * 100:.0f}%); {ev}")
    L.append(f"- rejestr naruszen (okna 24 m. lub od T42, konce tygodni): {len(r2['violation_registry'])} okien z przekroczeniem")


def _render_swaps(L: list[str], swaps: Sequence[dict[str, Any]] | None) -> None:
    if swaps is None:
        return
    L.append("")
    L.append("## Zamiany (zamiana_wydanie / zamiana_przyjecie)")
    if not swaps:
        L.append("- brak")
        return
    L.append("")
    L.append("| data | dzien osi | grupa kont | wydany | przyjety | wynik dnia wydany | wynik dnia przyjety | suma | uwagi |")
    L.append("|---|---|---|---|---|---:|---:|---:|---|")
    for s in swaps:
        notes = []
        if s["ten_sam_emitent"]:
            notes.append("ten sam emitent")
        if s["day"] is None:
            notes.append("poza atrybucja")
        L.append(f"| {s['date']} | {s['day'] or '-'} | {s['account']} | {s['wydany']} | {s['przyjety']} | "
                 f"{_num(s['wydany_pln'])} | {_num(s['przyjety_pln'])} | {_num(s['suma_pln'])} | {', '.join(notes) or '-'} |")
    L.append("")


def _render_persistence(L: list[str], pers: dict[Any, dict[str, Any]] | None) -> None:
    if not pers:
        return
    L.append("")
    L.append("## Rejestr trwaly M89")
    for reg in sorted(pers):
        p = pers[reg]
        if p.get("absent"):
            L.append(f"- rejestr {reg}: nieobecny (przed T42)")
            continue
        L.append(f"- rejestr {reg}: " + persistence_summary_line(p))


def persistence_summary_line(p: dict[str, Any]) -> str:
    """Jedna linia opisu przebiegu rejestru trwalego M89 (satellite.md i raport cyklu)."""
    ev, vi = p["events"], p["violations"]
    mode = "zapis" if p["write"] else "bez zapisu (na sucho)"
    if p["initial_feed"]:
        head = f"zasilenie poczatkowe: {len(ev['new'])} zdarzen, {len(vi['new'])} naruszen"
    else:
        head = (f"zdarzenia: nowe {len(ev['new'])}, zapisane wczesniej {ev['previous']}; "
                f"naruszenia: nowe {len(vi['new'])}, zapisane wczesniej {vi['previous']}")
    lost = [f"{x['component']} {x['event_date']} {x['level']}" for x in ev["not_reproduced"]] + [
        f"naruszenie {x['component']} {x['window_end']}" for x in vi["not_reproduced"]]
    return (f"{head}; nieodtworzone: {', '.join(lost) if lost else 'brak'}; "
            f"naruszenia z pelnych tygodni: {vi['computed_full_weeks']} "
            f"(okien z przekroczeniem: {vi['windows_with_exceedance']}, w biezacym tygodniu do zapisu w kolejnym cyklu: "
            f"{vi['pending_current_week']}); status danych: {p['data_status']}; {mode}")


def _render_m86(L: list[str], m: dict[str, Any] | None) -> None:
    if not m:
        return
    L.append("")
    L.append(f"## Ryzyko portfela M86 (dzien T = {m['T']})")
    if m["dates_differ"]:
        L.append(f"- UWAGA: risk_daily z dnia {m['risk_date']} != T {m['T']}")
    else:
        L.append(f"- risk_daily: {m['risk_date']}")
    h = m["heat"]
    L.append(f"- heat (poziom 3): {_num(h['heat_pct'])}% kapitalu satelity (limit {h['limit_pct']:.0f}%), "
             f"{_num(h['heat_pln'])} PLN; naruszenie: {h['breach']}; pozycji: {h['n_eligible']}"
             + (f"; niekompletne: {', '.join(h['incomplete'])}" if h["incomplete"] else ""))
    for th in m["level2"]:
        L.append(f"  - motyw {th['theme']}: {_num(th['pct'])}% (limit {th['limit_pct']:.0f}%), naruszenie: {th['breach']}; "
                 f"{', '.join(th['tickers'])}")
    if m["level2_no_theme"]:
        L.append(f"  - bez motywu: {', '.join(m['level2_no_theme'])}")
    f = m["futures"]
    L.append(f"- kontrakty ({f['n']}): nominal brutto {_num(f['nominal_gross_pln'])} PLN ({_pct(f['gross_share_nav'])} NAV), "
             f"netto {_num(f['nominal_net_pln'])} PLN ({_pct(f['net_share_nav'])} NAV)"
             + (f"; brak mnoznika: {', '.join(f['missing_multiplier'])}" if f["missing_multiplier"] else ""))
    fd = m["kontraktowy_free_vs_deposit"]
    L.append(f"- wolne srodki vs depozyt KONTRAKTOWY: {fd['status']} ({fd['reason']})")
    k = m["concentration"]
    L.append(f"- koncentracja: HHI = {_num(k['hhi'], 4)}; emitentow: {k['n_emitents']}; akcje/ETF long = {_num(k['long_equity_pln'])} PLN")
    for e in k["top5"]:
        L.append(f"  - {e['emitent']}: {_num(e['value_pln'])} PLN ({_pct(e['weight'])})")
    L.append(f"- gotowka AKCYJNY/ZAGRANICZNY: {_num(k['cash_pln'])} PLN ({_pct(k['cash_share_nav'])} NAV); "
             f"KONTRAKTOWY: {_num(k['kontraktowy_pln'])} PLN ({_pct(k['kontraktowy_share_nav'])} NAV); "
             f"netto kontrakty: {_pct(k['futures_net_share_nav'])} NAV")


# ---------------------------------------------------------------------------
# (C2) Rejestr 2 (od T42), zamiany, rejestr trwaly M89, wspolne liczenie
# ---------------------------------------------------------------------------


def registry2_label() -> str:
    """M83 dla rejestru 2: etykieta stala do T42 + 24 m. (ISO)."""
    return f"bez werdyktu do {minus_months(T42, -REGISTRY2_VERDICT_MONTHS).isoformat()}"


def analyze_registry2(
    axis: Sequence[date],
    nav: Sequence[float | None],
    flows: Sequence[float | None],
    bench: dict[str, Sequence[float | None]],
    verified_base: bool,
    model_over: Sequence[date] = (),
) -> dict[str, Any]:
    """Rejestr 2 = te same wielkosci co rejestr 1 (TWR, dW wobec obu progow,
    obsuniecia, mandat M88, alerty M89, rejestr naruszen) liczone od T42 do D:
    `analyze_series` na osi/NAV/F/progach obcietych od pierwszego dnia osi >= T42
    (okno kroczace = min(24 m., od T42) — `_window_bounds`). Werdykt M83:
    etykieta stala `registry2_label()` niezaleznie od danych. D < T42 ->
    status 'przed T42'."""
    out: dict[str, Any] = {
        "T42": T42.isoformat(),
        "verdict_label": registry2_label(),
        "status": "przed T42",
        "S": None,
        "windows": {},
        "alerts": {},
        "violation_registry": [],
        "data_verified": False,
        "data_unverified_reasons": [],
    }
    k = bisect.bisect_left(list(axis), T42)
    if k >= len(axis):
        return out
    reasons: list[str] = []
    verified = verified_base
    over = [d for d in model_over if d >= T42]
    if over:
        verified = False
        reasons.append(f"rejestr 2: udzial wyceny modelowej > {MODEL_SHARE_LIMIT * 100:.0f}% NAV w {len(over)} dniach (T45)")
    an = analyze_series(
        list(axis[k:]), list(nav[k:]), list(flows[k:]), {key: list(b[k:]) for key, b in bench.items()},
        data_verified=verified,
    )
    for w in an.get("windows", {}).values():
        if not w["complete"]:
            verified = False
            reasons.append(f"rejestr 2: okno {w['from']}..{w['to']} zawiera dzien niepelny")
        v = w["verdict"]
        v["result_computed"] = v["result"]
        v["result"] = "bez werdyktu"
        v["label"] = out["verdict_label"]
    out.update(
        {
            "status": "liczony",
            "S": an["S"],
            "windows": an.get("windows", {}),
            "alerts": an.get("alerts", {}),
            "violation_registry": an.get("violation_registry", []),
            "data_verified": bool(verified),
            "data_unverified_reasons": reasons,
        }
    )
    return out


def compute_swaps(
    inp: Inputs, axis: Sequence[date], att_daily: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Sekcja 'Zamiany' (decyzja 8): para wierszy zamiana_wydanie /
    zamiana_przyjecie z tego samego dnia i rachunku; wynik dnia obu linii
    emitentow z atrybucji dziennej M85 (linie emitentow zostaja osobne) i ich
    suma. Wynik dnia = suma `total` linii pozycji/kontraktu danej grupy kont i
    emitenta w dniu osi, do ktorego wpada wiersz (pierwszy dzien osi >= data
    wiersza; wiersze do pierwszego dnia osi wlacznie nie sa atrybuowane). Bez
    numerow rachunkow (grupa kont). None, gdy atrybucja dnia niedostepna."""
    by_day = {e["date"]: e["lines"] for e in att_daily}
    yahoo_map = build_yahoo_map(inp.instruments)
    groups: dict[tuple[date, str], tuple[list[TxRow], list[TxRow]]] = {}
    for r in inp.tx_rows:
        if r.row_type in ("zamiana_wydanie", "zamiana_przyjecie"):
            g = groups.setdefault((r.date, r.rachunek), ([], []))
            g[0 if r.row_type == "zamiana_wydanie" else 1].append(r)
    axis_l = list(axis)
    out: list[dict[str, Any]] = []
    for (dt, rach), (given, taken) in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        k = bisect.bisect_left(axis_l, dt)
        day = axis_l[k] if 0 < k < len(axis_l) else None
        lines = by_day.get(day.isoformat()) if day is not None else None
        grp = account_group(rach)

        def leg(r: TxRow | None) -> tuple[str, Any, float | None]:
            if r is None or r.instrument_id is None or r.instrument_id not in inp.instruments:
                return "-", None, None
            ek = emitent_key_for(r.instrument_id, inp.instruments, yahoo_map)[0]
            ticker = inp.instruments[r.instrument_id].ticker
            if lines is None:
                return ticker, ek, None
            val = sum(
                ln["total"] for ln in lines
                if ln["kind"] in ATTRIB_POSITIONAL_KINDS and ln["account"] == grp and ln["emitent_key"] == ek
            )
            return ticker, ek, val

        for n in range(max(len(given), len(taken))):
            t_out, k_out, v_out = leg(given[n] if n < len(given) else None)
            t_in, k_in, v_in = leg(taken[n] if n < len(taken) else None)
            out.append(
                {
                    "date": dt.isoformat(),
                    "day": day.isoformat() if day is not None else None,
                    "account": grp,
                    "wydany": t_out,
                    "przyjety": t_in,
                    "wydany_pln": v_out,
                    "przyjety_pln": v_in,
                    "suma_pln": (v_out + v_in) if (v_out is not None and v_in is not None) else None,
                    "ten_sam_emitent": k_out is not None and k_out == k_in,
                }
            )
    return out


def _iso_week(d: date) -> tuple[int, int]:
    iso = d.isocalendar()
    return (iso[0], iso[1])


def persist_mandate_registry(
    cur: Any,
    registry: int,
    alerts: dict[str, Any],
    violation_registry: Sequence[dict[str, Any]],
    d: date,
    data_status: str,
    run_at: datetime,
    code_sha_value: str | None,
    write: bool = True,
) -> dict[str, Any]:
    """Rejestr trwaly M89 (tabele satellite_mandate_events / _violations, tylko
    dopisywanie). Z przeliczenia (`alerts`, `violation_registry` z
    `analyze_series`) buduje wiersze zdarzen (skladnik abs/rel_sp500/rel_spyi,
    dd_current = obsuniecie w dniu zdarzenia) i naruszen (per skladnik
    naruszajacy). NARUSZENIA TYLKO Z PELNYCH TYGODNI: do zapisu ida wpisy z
    window_end w tygodniu ISO wczesniejszym niz tydzien ISO dnia D (okno do D w
    srodku tygodnia nie odtwarza sie w kolejnym cyklu); zdarzenia zapisywane
    wszystkie. Czyta wiersze rejestru (SELECT) i klasyfikuje wg kluczy UNIQUE
    tabel: nowe (brak w tabeli), zapisane wczesniej (w tabeli i w przeliczeniu),
    zapisane-nieodtworzone (w tabeli, brak w przeliczeniu; wiersz zostaje).
    Strona zapisana jest ograniczona jak strona liczona (zdarzenia <= D,
    naruszenia z pelnych tygodni), zeby tydzien D nie dawal falszywych roznic.
    Zasilenie poczatkowe = obie tabele bez wierszy rejestru przed przebiegiem.
    write=True: INSERT ... ON CONFLICT DO NOTHING (bez commit — commit robi
    cykl); write=False: zero INSERT, wynik opisuje, co zostaloby wstawione."""
    week_d = _iso_week(d)
    ev_rows: dict[tuple[str, date, str], dict[str, Any]] = {}
    for comp, a in alerts.items():
        for ev in a["events"]:
            if ev.get("dd") is None:
                continue
            ev_rows[(comp, date.fromisoformat(ev["date"]), ev["level"])] = {
                "component": comp, "event_date": ev["date"], "level": ev["level"],
                "dd_current": ev["dd"], "limit": a["limit"],
            }
    vi_rows: dict[tuple[str, date], dict[str, Any]] = {}
    pending_current_week = 0
    for entry in violation_registry:
        w_end = date.fromisoformat(entry["window_end"])
        if _iso_week(w_end) >= week_d:
            pending_current_week += 1
            continue
        for name in entry["violations"]:
            comp = COMPONENT_BY_MANDATE_KEY[name]
            vi_rows[(comp, w_end)] = {
                "component": comp, "window_start": entry["window_start"], "window_end": entry["window_end"],
                "max_dd": entry["components"][name], "limit": L_MAX if comp == "abs" else L_REL,
            }

    cur.execute("SELECT component, event_date, level FROM satellite_mandate_events WHERE registry = %s", (registry,))
    stored_ev_all = {(c, e, lv) for c, e, lv in cur.fetchall()}
    cur.execute("SELECT component, window_end FROM satellite_mandate_violations WHERE registry = %s", (registry,))
    stored_vi_all = {(c, w) for c, w in cur.fetchall()}
    initial_feed = not stored_ev_all and not stored_vi_all
    stored_ev = {k for k in stored_ev_all if k[1] <= d}
    stored_vi = {k for k in stored_vi_all if _iso_week(k[1]) < week_d}

    new_ev = [ev_rows[k] for k in sorted(ev_rows, key=lambda k: (k[1], k[0], k[2])) if k not in stored_ev_all]
    new_vi = [vi_rows[k] for k in sorted(vi_rows, key=lambda k: (k[1], k[0])) if k not in stored_vi_all]
    lost_ev = sorted(stored_ev - set(ev_rows), key=lambda k: (k[1], k[0], k[2]))
    lost_vi = sorted(stored_vi - set(vi_rows), key=lambda k: (k[1], k[0]))

    if write:
        for r in new_ev:
            cur.execute(
                "INSERT INTO satellite_mandate_events "
                "(registry, component, event_date, level, dd_current, limit_value, data_status, first_seen_run_at, code_sha) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (registry, component, event_date, level) DO NOTHING",
                (registry, r["component"], r["event_date"], r["level"], r["dd_current"], r["limit"],
                 data_status, run_at, code_sha_value),
            )
        for r in new_vi:
            cur.execute(
                "INSERT INTO satellite_mandate_violations "
                "(registry, component, window_start, window_end, max_dd, limit_value, data_status, first_seen_run_at, code_sha) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (registry, component, window_end) DO NOTHING",
                (registry, r["component"], r["window_start"], r["window_end"], r["max_dd"], r["limit"],
                 data_status, run_at, code_sha_value),
            )
    return {
        "registry": registry,
        "write": write,
        "data_status": data_status,
        "initial_feed": initial_feed,
        "events": {
            "computed": len(ev_rows),
            "new": new_ev,
            "previous": len(set(ev_rows) & stored_ev_all),
            "not_reproduced": [{"component": c, "event_date": e.isoformat(), "level": lv} for c, e, lv in lost_ev],
        },
        "violations": {
            "computed_full_weeks": len(vi_rows),
            "windows_with_exceedance": len(violation_registry),
            "pending_current_week": pending_current_week,
            "new": new_vi,
            "previous": len(set(vi_rows) & stored_vi_all),
            "not_reproduced": [{"component": c, "window_end": w.isoformat()} for c, w in lost_vi],
        },
    }


def compute_satellite(
    cur: Any,
    d_from: date,
    d_to: date,
    snapshots: Sequence[AccountValue] = (),
    smap: dict[str, dict[str, str]] | None = None,
    out_dir: Path | None = None,
    p3_series_path: Path | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Wspolne liczenie E3 (CLI `run_p3 satellite` i etap cyklu): tylko SELECT
    na przekazanym kursorze; os dni, NAV/przeplywy, progi z `prices_daily`
    (`benchmark_series`), rejestr 1 (od S) i rejestr 2 (od T42), okna, bramka M78
    (gdy `snapshots`), atrybucja M85, zamiany, ryzyko M86. Nie zapisuje plikow
    ani do bazy. Zwraca (report, payload); payload = report + serie dzienne."""
    inp = load_inputs(cur, d_from, d_to, smap, out_dir, extra_days=sorted({s.date for s in snapshots}))
    daily = compute_daily(inp)

    bench_prices, bench_why = benchmark_series(inp)
    eurpln = lambda d: fx_on(inp.fx, "EUR", d)  # noqa: E731
    axis = inp.axis
    bench: dict[str, list[float | None]] = {}
    bench_stale: dict[str, list[bool]] = {}
    reasons: list[str] = []
    for key, prices_eur in bench_prices.items():
        if prices_eur is None:
            bench[key], bench_stale[key] = [None] * len(axis), [False] * len(axis)
            reasons.append(bench_why[key])
            continue
        bench[key], bench_stale[key] = bench_on_axis(axis, prices_eur, eurpln)
        n_none = sum(1 for v in bench[key] if v is None)
        if n_none:
            reasons.append(f"prog {BENCHMARK_SYMBOLS[key]}: brak ceny <= D albo kursu EUR/PLN w {n_none} dniach osi")

    nav = [daily.navs[d].value for d in axis]
    flows = [daily.flows[d] for d in axis]
    gate = m78_gate(list(snapshots), daily.navs) if snapshots else None
    day_incomplete = [d for d, n, f in zip(axis, nav, flows) if n is None or f is None]
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
    modelled = {inp.instruments[i].ticker for i in inp.model_curves}
    unpriced = [t for t in inp.no_price_tickers if t not in modelled]
    if unpriced:
        reasons.append(f"instrumenty bez cen w prices_daily, bez mapy i bez wyceny modelowej: {len(unpriced)}")

    # T45: dzienny udzial wyceny modelowej w NAV; > MODEL_SHARE_LIMIT -> dane niezweryfikowane
    model_days = [(d, daily.navs[d].model_share, daily.navs[d].model_tickers) for d in axis
                  if daily.navs[d].model_tickers]
    shares = [(s, d) for d, s, _ in model_days if s is not None]
    model_max = max(shares) if shares else None
    model_over = [d for s, d in shares if s > MODEL_SHARE_LIMIT]
    if model_over:
        reasons.append(f"udzial wyceny modelowej > {MODEL_SHARE_LIMIT * 100:.0f}% NAV w {len(model_over)} dniach (T45)")
    model_summary = {
        "n_days": len(model_days),
        "max_share": model_max[0] if model_max else None,
        "max_share_date": model_max[1] if model_max else None,
        "mean_share": statistics.mean(s for s, _ in shares) if shares else None,
        "days_over_limit": len(model_over),
        "limit": MODEL_SHARE_LIMIT,
        "tickers": sorted({t for _, _, ts in model_days for t in ts}),
    }
    for w in analysis["windows"].values():
        w["mandate"]["sensitive_to_model"] = bool(w["mandate"]["near_limit"]) and bool(model_days)

    reg2 = analyze_registry2(axis, nav, flows, bench, verified_base, model_over)
    for w in reg2["windows"].values():
        w["mandate"]["sensitive_to_model"] = bool(w["mandate"]["near_limit"]) and bool(model_days)

    p3_control = None
    if p3_series_path:
        p3_control = compare_with_p3_series(
            {k: {d: v for i, (d, v) in enumerate(zip(axis, b)) if v is not None and not bench_stale[k][i]}
             for k, b in bench.items()},
            p3_series_path,
        )

    data_verified_1 = (
        verified_base and not model_over
        and not any(not w["complete"] for w in analysis["windows"].values())
        and not any(prices is None for prices in bench_prices.values())
    )
    report: dict[str, Any] = {
        "d_from": d_from.isoformat(),
        "d_to": d_to.isoformat(),
        "conventions": {
            "currency": "PLN",
            "fx": "NBP A, ostatni kurs z data <= D",
            "prices": "prices_daily.close_split_adj x layer_factor_after, regula T27",
            "benchmarks": {k: {"symbol": BENCHMARK_SYMBOLS[k], "source": BENCHMARK_SOURCE} for k in BENCHMARK_SYMBOLS},
        },
        "n_axis_days": len(axis),
        "n_incomplete_days": len(day_incomplete),
        "S": analysis["S"],
        "T": analysis["T"],
        "nav_d_pln": nav[-1] if nav else None,
        "gate": gate,
        "data_verified": bool(data_verified_1),
        "model_valuation": model_summary,
        "data_unverified_reasons": sorted(set(reasons)),
        "unknown_row_types": daily.unknown_row_types,
        "missing_summary": missing_summary,
        "no_price_tickers": inp.no_price_tickers,
        "warnings": inp.warnings,
        "p3_control": p3_control,
        "windows": analysis["windows"],
        "alerts": analysis.get("alerts", {}),
        "violation_registry": analysis.get("violation_registry", []),
        "registry2": reg2,
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
                "model_share": n.model_share,
                "model_tickers": n.model_tickers,
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
    # B-38a: atrybucja M85 (z tych samych NavResult/F co powyzej) i ryzyko M86 (SELECT risk_daily)
    att = compute_attribution(inp, daily, axis, analysis["windows"])
    report["attribution"] = att["attribution"]
    report["swaps"] = compute_swaps(inp, axis, att["attribution_daily"])
    if axis:
        risk_rows = load_risk_snapshot(cur, axis[-1])
        report["m86"] = m86_snapshot(risk_rows, daily.navs[axis[-1]], axis[-1], inp.instruments,
                                     build_yahoo_map(inp.instruments))
    payload = dict(report)
    payload["attribution_daily"] = att["attribution_daily"]
    payload["series"] = series
    return report, payload


def write_satellite_outputs(out_dir: Path | str, report: dict[str, Any], payload: dict[str, Any]) -> Path:
    """Zapis satellite.json i satellite.md w `out_dir`; zwraca sciezke satellite.md."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "satellite.json").write_text(json.dumps(_clean(payload), ensure_ascii=False, indent=1), encoding="utf-8")
    md = out / "satellite.md"
    md.write_text(render_markdown(report), encoding="utf-8")
    return md


def run_satellite(
    conn: Any,
    d_from: date,
    d_to: date,
    out_dir: Path | str,
    account_values: Path | str | None = None,
    symbol_map: Path | str | None = None,
    p3_series_path: Path | str | None = None,
) -> dict[str, Any]:
    """CLI `run_p3 satellite`: `conn.read_only = True` (zero zapisow do bazy),
    `compute_satellite`, zapis satellite.json / satellite.md w `out_dir`.
    Zwraca podsumowanie (agregaty). Rejestr trwaly M89 NIE jest tu zapisywany."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    conn.read_only = True
    snapshots = load_account_values(account_values) if account_values else []
    smap = load_symbol_map(symbol_map) if symbol_map else None
    with conn.cursor() as cur:
        report, payload = compute_satellite(cur, d_from, d_to, snapshots, smap, out, p3_series_path)
    write_satellite_outputs(out, report, payload)
    return report


# ---------------------------------------------------------------------------
# (C3) Etap satelity cyklu
# ---------------------------------------------------------------------------


class SatelliteStageError(RuntimeError):
    """Etap satelity nie moze zapisac wynikow (np. katalog poza .gitignore)."""


@dataclass
class SatelliteStageResult:
    """Podsumowanie etapu satelity dla raportu cyklu."""

    d: date
    report: dict[str, Any]
    persistence: dict[int, dict[str, Any]]
    out_dir: Path | None = None
    md_path: Path | None = None
    written: bool = False  # True: wiersze M89 wstawione (write=True)


_UNSET: Any = object()


def run_satellite_stage(
    conn: Any,
    d: date,
    out_dir: Path | None = None,
    run_at: datetime | None = None,
    write: bool = True,
    check_ignored: Callable[[Path], bool] | None = None,
    code_sha_value: Any = _UNSET,
) -> SatelliteStageResult:
    """Etap satelity cyklu (decyzja 6): d_from = S1, d_to = D, bez account_values
    (bramka M78 nieobecna -> dane niezweryfikowane, przyczyna w raporcie). Czyta
    baze przez polaczenie cyklu (NIE ustawia `conn.read_only`); zapisuje
    wylacznie do dwoch tabel M89 (rejestr 1 i 2) i — gdy `out_dir` —
    satellite.json / satellite.md (przed jakimkolwiek zapisem
    `check_ignored(out_dir/satellite.md)` musi byc True, inaczej
    `SatelliteStageError`). write=False: zero INSERT; z `out_dir=None` rowniez
    zero plikow (pomiar na sucho). Commit robi wywolujacy."""
    if out_dir is not None and check_ignored is not None and not check_ignored(Path(out_dir) / "satellite.md"):
        raise SatelliteStageError(f"katalog satelity nie jest objety .gitignore: {out_dir}; nic nie zapisano")
    if run_at is None:
        run_at = datetime.now(timezone.utc)
    sha = _code_sha() if code_sha_value is _UNSET else code_sha_value
    with conn.cursor() as cur:
        report, payload = compute_satellite(cur, S1, d)
        persistence: dict[int, dict[str, Any]] = {}
        status1 = "zweryfikowane" if report["data_verified"] else "niezweryfikowane"
        persistence[1] = persist_mandate_registry(
            cur, 1, report["alerts"], report["violation_registry"], d, status1, run_at, sha, write
        )
        reg2 = report["registry2"]
        if reg2["status"] == "liczony":
            status2 = "zweryfikowane" if reg2["data_verified"] else "niezweryfikowane"
            persistence[2] = persist_mandate_registry(
                cur, 2, reg2["alerts"], reg2["violation_registry"], d, status2, run_at, sha, write
            )
        else:
            persistence[2] = {"registry": 2, "absent": True, "write": write}
    report["persistence"] = persistence
    payload["persistence"] = persistence
    md_path = write_satellite_outputs(out_dir, report, payload) if out_dir is not None else None
    return SatelliteStageResult(
        d=d, report=report, persistence=persistence,
        out_dir=Path(out_dir) if out_dir is not None else None, md_path=md_path, written=write,
    )

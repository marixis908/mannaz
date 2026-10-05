"""Czyste funkcje dashboardu satelity (brief CC-D, krok D4).

Zero zależności od bazy/sieci — wszystkie wejścia to proste struktury
(dict/list/Decimal/date), testowalne wprost danymi syntetycznymi
(tests/test_dashboard.py). Kontrakt danych: tmp/dashboard/kontrakt-danych.md
(rozstrzyga wątpliwości w razie sprzeczności z tym modułem).

`generate.py` woła te funkcje po pobraniu surowych wierszy z bazy —
ten moduł nigdy nie widzi połączenia z Postgresem.

Konwencja braku danych: gdziekolwiek wielkości nie da się policzyć, funkcja
zwraca `None` (albo pole `brak_pomiar_opis` z treścią) zamiast zgadywać —
warstwa renderująca (JS w template.html) zamienia `None` na literalny napis
"brak pomiaru" z opisem. Ten moduł NIGDY nie zwraca napisu "brak pomiaru"
wprost (to detal warstwy prezentacji), tylko `None`/pola statusowe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any


class UnhandledCurrencyError(Exception):
    """STOP (kontrakt §3): waluta notowania pozycji nieobsługiwana —
    grosze GBp/GBX albo brak pary w `fx_nbp`. Generator łapie ten wyjątek
    i kończy proces kodem 2, bez drukowania żadnej kwoty."""


# Yahoo miesza czasem GBP/GBp (pułapka x100, patrz docs §9.4 i
# `mannaz.prices.check_currency`) — gdyby `instruments.currency` kiedyś
# zawierało dosłownie "GBp"/"GBX" (grosze), dashboard STOPuje zamiast zgadywać
# współczynnik (inaczej niż `mannaz.risk.gbp_pence_factor`, który koryguje
# ATR/stopy przez sufiks ".L" — tu chodzi o samą wycenę składu, twardy STOP).
_PENCE_CURRENCIES = frozenset({"GBp", "GBX"})


# ---------------------------------------------------------------------------
# Formatowanie liczb w konwencji PL (spacja jako separator tysięcy DLA
# KAŻDEJ liczby >= 1000, przecinek jako separator dziesiętny) — Intl pl-PL
# pomija grupowanie 4-cyfrowych liczb, więc robimy to ręcznie. Ten sam
# algorytm jest powtórzony w JS w template.html (bez wspólnego kodu — różne
# środowiska wykonania), ale reguła musi być identyczna.
# ---------------------------------------------------------------------------


def format_pl_number(value: Decimal | int | float, decimals: int = 0) -> str:
    """`value` NIE MOŻE być None — wywołujący odpowiada za obsługę braku
    danych przed wywołaniem tej funkcji (patrz docstring modułu)."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    sign = "-" if d < 0 else ""
    d = abs(d)
    if decimals > 0:
        quant = Decimal(1).scaleb(-decimals)
        d = d.quantize(quant, rounding=ROUND_HALF_UP)
        text = f"{d:f}"
        int_part, _, frac_part = text.partition(".")
        frac_part = (frac_part + ("0" * decimals))[:decimals]
    else:
        d = d.quantize(Decimal(1), rounding=ROUND_HALF_UP)
        int_part = f"{d:f}"
        frac_part = ""

    groups: list[str] = []
    while len(int_part) > 3:
        groups.insert(0, int_part[-3:])
        int_part = int_part[:-3]
    groups.insert(0, int_part)
    grouped = " ".join(groups)

    if decimals > 0:
        return f"{sign}{grouped},{frac_part}"
    return f"{sign}{grouped}"


# ---------------------------------------------------------------------------
# Rynek z sufiksu yahoo_symbol (kontrakt §4, docs §9.4)
# ---------------------------------------------------------------------------

_CANADA_SUFFIXES = (".TO", ".V", ".NE")
_EUROPE_SUFFIXES = (
    ".AS", ".DE", ".F", ".PA", ".MI", ".MC", ".SW", ".L",
    ".ST", ".CO", ".HE", ".OL", ".BR", ".LS", ".VI", ".IR",
)


def market_from_yahoo_symbol(yahoo_symbol: str | None) -> str:
    """brak sufiksu -> USA; `.WA` -> GPW; kanadyjskie -> Kanada; europejskie
    (lista §9.4) -> Europa; inny sufiks/brak yahoo_symbol -> "nieustalony"."""
    if not yahoo_symbol:
        return "nieustalony"
    if yahoo_symbol.endswith(".WA"):
        return "GPW"
    if yahoo_symbol.endswith(_CANADA_SUFFIXES):
        return "Kanada"
    if yahoo_symbol.endswith(_EUROPE_SUFFIXES):
        return "Europa"
    if "." not in yahoo_symbol:
        return "USA"
    return "nieustalony"


# ---------------------------------------------------------------------------
# Klasyfikacja zbiorów SAT/CORE/FUT/INNE (kontrakt §2)
# ---------------------------------------------------------------------------

_SAT_CORE_RACHUNKI = ("AKCYJNY", "ZAGRANICZNY")
_SAT_CORE_TYPES = ("equity", "etf")


def classify_position(rachunek_label: str, instrument_type: str, is_core: bool) -> str:
    """Zwraca "SAT" | "CORE" | "FUT" | "INNE" wg kontraktu §2. `rachunek_label`
    = PIERWSZE SŁOWO pola `rachunek` (numer rachunku nigdy nie wchodzi tutaj
    ani dalej do HTML/logów)."""
    if rachunek_label in _SAT_CORE_RACHUNKI and instrument_type in _SAT_CORE_TYPES:
        return "CORE" if is_core else "SAT"
    if rachunek_label == "KONTRAKTOWY" and instrument_type == "future":
        return "FUT"
    return "INNE"


def account_label(rachunek_raw: str) -> str:
    """Etykieta rachunku = pierwsze słowo `rachunek` (kontrakt §4) — jedyna
    forma, w jakiej rachunek wolno przekazać dalej (numer rachunku nigdy nie
    trafia do HTML ani logów)."""
    return rachunek_raw.split()[0] if rachunek_raw and rachunek_raw.split() else rachunek_raw


# ---------------------------------------------------------------------------
# Waluta nieobsłużona / FX (kontrakt §3)
# ---------------------------------------------------------------------------


def resolve_fx_factor(currency: str, rate: Decimal | None, currency_supported: bool) -> Decimal | None:
    """`rate`: ostatni kurs NBP A <= D dla pary currency/PLN, już pobrany
    przez wywołującego (None gdy brak takiego wiersza). `currency_supported`:
    czy ta waluta ma w ogóle JAKĄKOLWIEK parę w `fx_nbp` (niezależnie od
    daty) — rozstrzyga wywołujący jednym zapytaniem `SELECT 1 ... LIMIT 1`
    bez filtra daty. PLN -> zawsze 1.

    Dwa różne stany (kontrakt §3, wiersze "FX" i "waluta nieobsłużona" nie są
    tym samym):
      - waluta STRUKTURALNIE nieobsługiwana (grosze GBp/GBX albo zero par w
        `fx_nbp` dla tej waluty w ogóle) -> `UnhandledCurrencyError`, STOP
        całego przebiegu (kontrakt §3 „waluta nieobsłużona").
      - waluta obsługiwana, ale brak konkretnego kursu <= D (np. data
        wcześniejsza niż pierwszy fetch) -> zwraca `None`, BEZ STOP —
        pozycja poza BW, `brak pomiaru: FX` w stopce (kontrakt §3 „gdy brak"
        w wierszu FX)."""
    if currency == "PLN":
        return Decimal(1)
    if currency in _PENCE_CURRENCIES or not currency_supported:
        raise UnhandledCurrencyError(
            f"STOP: waluta nieobsluzona ({currency}) — grosze albo brak pary w fx_nbp (docs §9.4)"
        )
    return rate


# ---------------------------------------------------------------------------
# Konsolidacja (kontrakt §4)
# ---------------------------------------------------------------------------


def consolidation_key(isin: str | None, broker_ticker: str, market: str) -> tuple:
    if isin:
        return ("isin", isin)
    return ("ticker_market", broker_ticker, market)


# ---------------------------------------------------------------------------
# Status ceny pozycji wg T27 (kontrakt §3/§4) — "ok" < "stale" < "incomplete"
# ranga rosnąca; status pozycji scalonej = najgorszy ze składowych.
# ---------------------------------------------------------------------------

_PRICE_STATUS_RANK = {"ok": 0, "stale": 1, "incomplete": 2}


def worst_price_status(statuses: list[str]) -> str:
    """Najgorszy status ceny ze zbioru (kontrakt §4: `incomplete` > `stale` >
    `ok`). Pusta lista -> `KeyError` się nie zdarza (wywołujący zawsze woła to
    z co najmniej jednym elementem — grupa konsolidacji ma >=1 wiersz)."""
    return max(statuses, key=lambda s: _PRICE_STATUS_RANK[s])


@dataclass
class ConsolidationResult:
    positions: list[dict[str, Any]]
    count_before: int
    count_after: int
    merge_count: int = 0  # liczba pozycji "wchłoniętych" (redukcja liczności)
    ambiguous_count: int = 0  # grupy ISIN z różnymi rynkami — NIE scalone
    mixed_currency_merge_count: int = 0


def consolidate_positions(rows: list[dict[str, Any]]) -> ConsolidationResult:
    """rows: dicty z co najmniej kluczami: isin, broker_ticker, market,
    settlement_currency, value_pln (Decimal|None), residual_cost (Decimal|None),
    rachunek (etykieta, NIE numer). Klucz konsolidacji: ISIN; bez ISIN ->
    broker_ticker + rynek (kontrakt §4). Ten sam ISIN z różnymi rynkami
    (różny sufiks yahoo_symbol) jest NIEJEDNOZNACZNY — nie scala się, tylko
    flaguje + liczy."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for r in rows:
        key = consolidation_key(r["isin"], r["broker_ticker"], r["market"])
        groups.setdefault(key, []).append(r)

    out: list[dict[str, Any]] = []
    merge_count = 0
    ambiguous_count = 0
    mixed_currency_merge_count = 0

    for key, group in groups.items():
        markets = {g["market"] for g in group}
        if key[0] == "isin" and len(markets) > 1:
            ambiguous_count += 1
            for g in group:
                item = dict(g)
                item["consolidated"] = False
                item["ambiguous"] = True
                item["mixed_currency"] = False
                item["components"] = [g]
                out.append(item)
            continue

        if len(group) == 1:
            item = dict(group[0])
            item["consolidated"] = False
            item["ambiguous"] = False
            item["mixed_currency"] = False
            item["components"] = [group[0]]
            out.append(item)
            continue

        merge_count += len(group) - 1
        currencies = {g["settlement_currency"] for g in group}
        mixed_currency = len(currencies) > 1
        if mixed_currency:
            mixed_currency_merge_count += 1

        value_pln_values = [g["value_pln"] for g in group]
        value_pln_sum = sum(value_pln_values, Decimal(0)) if all(v is not None for v in value_pln_values) else None

        residual_cost_sum = None
        if not mixed_currency:
            residual_costs = [g["residual_cost"] for g in group]
            if all(c is not None for c in residual_costs):
                residual_cost_sum = sum(residual_costs, Decimal(0))

        accounts = sorted({g["rachunek"] for g in group})
        merged = dict(group[0])
        merged.update(
            {
                "value_pln": value_pln_sum,
                "residual_cost": residual_cost_sum,
                "rachunek": "/".join(accounts),
                "consolidated": True,
                "ambiguous": False,
                "mixed_currency": mixed_currency,
                "components": group,
                "settlement_currency": next(iter(currencies)) if not mixed_currency else None,
                # Status ceny pozycji scalonej = najgorszy ze składowych
                # (kontrakt §4) — nie tylko pierwszego komponentu.
                "price_status": worst_price_status([g["price_status"] for g in group]),
            }
        )
        out.append(merged)

    return ConsolidationResult(
        positions=out,
        count_before=len(rows),
        count_after=len(out),
        merge_count=merge_count,
        ambiguous_count=ambiguous_count,
        mixed_currency_merge_count=mixed_currency_merge_count,
    )


# ---------------------------------------------------------------------------
# Wagi (kontrakt §3/§8)
# ---------------------------------------------------------------------------


def compute_weights(rows: list[dict[str, Any]], bw: Decimal | None) -> list[Decimal | None]:
    """Waga = value_pln / BW; brak wartości pozycji albo BW=0/None -> None
    (pozycja/mianownik poza obliczeniem, kontrakt §3)."""
    if not bw:
        return [None for _ in rows]
    return [(r["value_pln"] / bw) if r["value_pln"] is not None else None for r in rows]


# ---------------------------------------------------------------------------
# Liczniki n/N i sumy "wszystko-albo-nic" (kontrakt §2/§3/§7): gdy choć jedna
# pozycja zbioru ma cenę T27 `incomplete` (brak wartości PLN), suma (BW,
# wartość CORE, nominał FUT) jest `brak pomiaru` z licznikiem n/N — NIGDY
# suma częściowa, pozycja NIGDY nie wypada z mianownika N.
# ---------------------------------------------------------------------------


def count_complete(rows: list[dict[str, Any]], field_name: str = "value_pln") -> tuple[int, int]:
    """n = liczba wierszy z `field_name` policzalnym (not None), N = liczba
    wszystkich wierszy zbioru (mianownik NIGDY nie maleje)."""
    n = sum(1 for r in rows if r.get(field_name) is not None)
    return n, len(rows)


def compute_sum_if_complete(rows: list[dict[str, Any]], field_name: str = "value_pln") -> Decimal | None:
    """Suma `field_name` po wierszach WYŁĄCZNIE, gdy KAŻDY wiersz zbioru ma tę
    wielkość policzalną (n == N); inaczej `None` (kontrakt: „nie liczyć sum
    częściowych"). Zbiór pusty (N=0) -> suma pusta = 0 (wszystko policzone,
    bo nie ma czego liczyć)."""
    n, big_n = count_complete(rows, field_name)
    if n < big_n:
        return None
    return sum((r[field_name] for r in rows), Decimal(0))


# ---------------------------------------------------------------------------
# Wynik (a) w walucie rozliczenia (kontrakt §3)
# ---------------------------------------------------------------------------


@dataclass
class ResultA:
    amount: Decimal | None
    pct: Decimal | None
    cross_rate_flag: bool
    brak_opis: str | None


def compute_result_a(
    qty: Decimal,
    close_raw: Decimal | None,
    residual_cost: Decimal | None,
    quote_currency: str,
    settlement_currency: str,
    fx_quote: Decimal | None,
    fx_settlement: Decimal | None,
) -> ResultA:
    """`wartość na D w walucie rozliczenia − residual_cost`; gdy waluta
    notowania = rozliczenia: `qty × close_raw`; gdy różne: kurs krzyżowy
    NBP A <= D przez PLN = FX(notowania)/FX(rozliczenia), ze znacznikiem
    "zawiera kurs notowanie/rozliczenie". `%` = wynik / residual_cost."""
    if close_raw is None:
        return ResultA(None, None, False, "cena ≤ D niedostępna")
    if residual_cost is None or residual_cost <= 0:
        return ResultA(None, None, False, "residual_cost ≤ 0 albo NULL")

    if quote_currency == settlement_currency:
        value_in_settlement = qty * close_raw
        cross_rate_flag = False
    else:
        if fx_quote is None or fx_settlement is None or fx_settlement == 0:
            return ResultA(None, None, False, "brak kursu krzyżowego NBP A ≤ D")
        cross_rate = fx_quote / fx_settlement
        value_in_settlement = qty * close_raw * cross_rate
        cross_rate_flag = True

    amount = value_in_settlement - residual_cost
    pct = (amount / residual_cost) * Decimal(100)
    return ResultA(amount, pct, cross_rate_flag, None)


# ---------------------------------------------------------------------------
# Testy pochodzenia ryzyka (kontrakt §5)
# ---------------------------------------------------------------------------


def evaluate_test1_single_run(computed_at_values: list[datetime]) -> bool:
    """Test 1 rew. 3 (kontrakt §5): PASS gdy WSZYSTKIE wiersze `risk_daily`
    D ryzyka dzielą jeden `computed_at` ALBO rozrzut (max-min) <= 5 s.
    Pusta lista (brak wierszy na D ryzyka) -> FAIL (nie ma przebiegu)."""
    if not computed_at_values:
        return False
    lo = min(computed_at_values)
    hi = max(computed_at_values)
    return (hi - lo).total_seconds() <= 5


def evaluate_test2_position_set_match(risk_daily_keys: set, positions_keys: set) -> bool:
    return risk_daily_keys == positions_keys


def evaluate_test3_closed_positions_provenance(
    has_transactions_after_risk_date: bool,
    closed_positions: list[dict[str, Any]],
) -> str:
    """Test 3 rew. 3 z decyzją ownera (nadpisuje wcześniejszą wersję bez
    `has_transactions_after_risk_date`, kontrakt §5): kontrola dodatnia —
    `closed_positions`: pozycje otwarte na D ryzyka, nieobecne TERAZ w
    `positions_fifo` (qty<>0). Każdy dict: {'has_risk_daily_row': bool,
    'computed_at': datetime|None, 'max_sell_created_at': datetime|None}.
    `has_transactions_after_risk_date`: czy w `transactions` istnieje
    JAKIKOLWIEK wiersz z `transaction_date > D ryzyka` (dowolny rachunek/
    instrument/typ). Zwraca:
      - `"nie_dotyczy"` — brak jakichkolwiek transakcji po D ryzyka w ogóle;
        kontrola dodatnia nie ma czego sprawdzić Z DEFINICJI (nie blokuje
        PASS zbiorczego, o ile testy 1/2 PASS);
      - `"niedostepna"` — transakcje po D ryzyka ISTNIEJĄ, ale żadna pozycja
        otwarta na D ryzyka nie została odnaleziona jako zamknięta TERAZ —
        kontrola dodatnia nie mogła się wykonać MIMO danych po D (traktowane
        jak FAIL przy renderowaniu, ale to ODDZIELNY status: overall
        "NIEROZSTRZYGNIETY", nie "FAIL");
      - `"pass"` — dla KAŻDEJ znalezionej pozycji zamkniętej: ma wiersz w
        risk_daily, a jego `computed_at` jest późniejszy niż ostatnia znana
        transakcja zamykająca;
      - `"fail"` — inaczej."""
    if not has_transactions_after_risk_date:
        return "nie_dotyczy"
    if not closed_positions:
        return "niedostepna"
    for item in closed_positions:
        if not item["has_risk_daily_row"]:
            return "fail"
        if item["computed_at"] is None or item["max_sell_created_at"] is None:
            return "fail"
        if not (item["computed_at"] > item["max_sell_created_at"]):
            return "fail"
    return "pass"


@dataclass
class ProvenanceTest:
    name: str
    status: str  # "PASS" | "FAIL" | "nie dotyczy" | "niedostepna"


@dataclass
class ProvenanceResult:
    passed: bool  # True WYŁĄCZNIE gdy overall_status == "PASS" (brama renderowania sekcji ryzyka)
    overall_status: str = "PASS"  # "PASS" | "FAIL" | "NIEROZSTRZYGNIETY"
    tests: list[ProvenanceTest] = field(default_factory=list)
    failed_tests: list[str] = field(default_factory=list)
    note: str | None = None


_TEST_NAMES = {
    1: "test 1: jeden przebieg na D ryzyka (jeden computed_at albo rozrzut <=5s)",
    2: "test 2: zbior pozycji risk_daily(D) = positions_as_of(D)",
    3: "test 3: kontrola dodatnia - pochodzenie wiersza pozycji zamknietej po D ryzyka",
}

_TEST3_LABELS = {
    "pass": "PASS",
    "fail": "FAIL",
    "nie_dotyczy": "nie dotyczy",
    "niedostepna": "niedostepna",
}


def evaluate_provenance(test1_ok: bool, test2_ok: bool, test3_status: str) -> ProvenanceResult:
    """Status zbiorczy (kontrakt §5 rew. 3 + decyzja ownera): FAIL gdy
    KTORYKOLWIEK test zwrocil FAIL (test1/test2 bool, test3 == "fail").
    Inaczej: test3 == "niedostepna" (transakcje po D ryzyka są, ale kontrola
    dodatnia nie znalazła czego sprawdzić) -> overall "NIEROZSTRZYGNIETY",
    traktowany PRZY RENDEROWANIU jak FAIL (sekcja ryzyka i kolumny ryzyka =
    brak pomiaru), ale to ODDZIELNA etykieta od "FAIL". Inaczej (test3 in
    {"pass", "nie_dotyczy"}) -> "PASS". `test3_status`: patrz
    `evaluate_test3_closed_positions_provenance`."""
    test3_label = _TEST3_LABELS[test3_status]
    tests = [
        ProvenanceTest(_TEST_NAMES[1], "PASS" if test1_ok else "FAIL"),
        ProvenanceTest(_TEST_NAMES[2], "PASS" if test2_ok else "FAIL"),
        ProvenanceTest(_TEST_NAMES[3], test3_label),
    ]
    any_fail = (not test1_ok) or (not test2_ok) or (test3_status == "fail")
    if any_fail:
        overall = "FAIL"
    elif test3_status == "niedostepna":
        overall = "NIEROZSTRZYGNIETY"
    else:
        overall = "PASS"

    failed = [t.name for t in tests if t.status == "FAIL"]
    note = None
    if test3_status == "nie_dotyczy":
        note = "kontrola dodatnia nie dotyczy: brak danych po D"
    elif test3_status == "niedostepna":
        note = "kontrola dodatnia niedostepna mimo danych po D"

    return ProvenanceResult(
        passed=(overall == "PASS"),
        overall_status=overall,
        tests=tests,
        failed_tests=failed,
        note=note,
    )


# ---------------------------------------------------------------------------
# Agregacja ryzyka (kontrakt §5) — `is_risk_budget_eligible` importowana z
# `mannaz.risk` przez wywołującego (`generate.py`) i przekazana tutaj jako
# argument, żeby ten moduł nie zależał od ścieżki importu pakietu mannaz przy
# uruchamianiu samych testów (`tests/test_dashboard.py` importuje ją wprost
# z `mannaz.risk`, tak samo jak generator — jedna definicja, bez duplikatu).
# ---------------------------------------------------------------------------


def sum_open_risk_pct(rows: list[dict[str, Any]], is_eligible) -> Decimal:
    """Σ `risk_pct_satellite_capital` po wierszach budżetowych (equity/etf
    spoza core + future), bez `multiplier_missing` (kontrakt §5)."""
    total = Decimal(0)
    for r in rows:
        if (
            is_eligible(r["instrument_type"], r["is_core"])
            and not r["multiplier_missing"]
            and r["risk_pct_satellite_capital"] is not None
        ):
            total += r["risk_pct_satellite_capital"]
    return total


def _level1_names(rows: list[dict[str, Any]], is_eligible):
    from mannaz.risk import level1_by_name  # jedna definicja, jak is_risk_budget_eligible

    items = [
        (
            r.get("name_key"),
            is_eligible(r["instrument_type"], r["is_core"]),
            r.get("broker_ticker") or str(r.get("instrument_id")),
            r.get("settlement_currency"),
            r.get("risk_pct_satellite_capital"),
            r.get("name_risk_pct"),
            r.get("level1_breach"),
        )
        for r in rows
    ]
    labels = {r["name_key"]: r["name_label"] for r in rows if r.get("name_key") is not None and r.get("name_label")}
    return level1_by_name(items, labels)


def count_level1_name_breaches(rows: list[dict[str, Any]], is_eligible) -> int:
    """B-32: liczba NAZW (nie wierszy) z przekroczeniem poziomu 1; nazwy
    niepełne nie są liczone jako czyste — patrz `count_level1_incomplete_names`."""
    return sum(1 for n in _level1_names(rows, is_eligible) if n.breach)


def count_level1_incomplete_names(rows: list[dict[str, Any]], is_eligible) -> int:
    return sum(1 for n in _level1_names(rows, is_eligible) if n.incomplete)


def price_coverage(rows: list[dict[str, Any]], is_eligible) -> tuple[int, int]:
    """n = wiersze SAT+FUT z close_d IS NOT NULL, N = wszystkie SAT+FUT na D
    ryzyka (kontrakt §5)."""
    eligible = [r for r in rows if is_eligible(r["instrument_type"], r["is_core"])]
    n = sum(1 for r in eligible if r["close_d"] is not None)
    return n, len(eligible)


def count_stale_risk_rows(rows: list[dict[str, Any]], is_eligible) -> tuple[int, int]:
    """Licznik cen nieświeżych w `risk_daily` (kontrakt §5, zastępuje
    etykietę B-19): n = wiersze SAT+FUT z `price_is_stale = True`, N =
    wszystkie wiersze SAT+FUT na D ryzyka."""
    eligible = [r for r in rows if is_eligible(r["instrument_type"], r["is_core"])]
    n_stale = sum(1 for r in eligible if r.get("price_is_stale"))
    return n_stale, len(eligible)


def group_risk_pct_by_theme(rows: list[dict[str, Any]], is_eligible) -> dict[str, Decimal]:
    """Σ `risk_pct_satellite_capital` per `instruments.theme` wśród wierszy
    budżetowych; brak tematu -> klucz "bez tematu" (B-20: `theme` jest
    atrybutem BIEŻĄCYM instrumentu, bez historii)."""
    sums: dict[str, Decimal] = {}
    for r in rows:
        if not is_eligible(r["instrument_type"], r["is_core"]):
            continue
        if r["multiplier_missing"] or r["risk_pct_satellite_capital"] is None:
            continue
        key = r["theme"] if r["theme"] else "bez tematu"
        sums[key] = sums.get(key, Decimal(0)) + r["risk_pct_satellite_capital"]
    return sums


# ---------------------------------------------------------------------------
# Kontrakty terminowe — nominał (kontrakt §7/§19.4)
# ---------------------------------------------------------------------------


def compute_futures_nominal_pln(
    qty: Decimal,
    multiplier: Decimal | None,
    base_close_raw: Decimal | None,
    base_fx: Decimal | None,
) -> Decimal | None:
    """nominał = qty × multiplier × close_raw(baza ≤ D) w PLN, znak zgodny
    z qty (krótka pozycja < 0). Brak mnożnika/ceny bazy/FX -> None (brak
    pomiaru, licznik w stopce)."""
    if multiplier is None or base_close_raw is None or base_fx is None:
        return None
    return qty * multiplier * base_close_raw * base_fx


# ---------------------------------------------------------------------------
# Świeżość / STOP sesji (kontrakt §0/§8)
# ---------------------------------------------------------------------------


def count_weekday_sessions_between(d_start: date, d_end: date) -> int:
    """Liczba dni roboczych (pon-pt) między `d_start` (WYŁĄCZNIE) a `d_end`
    (włącznie) — przybliżenie liczby sesji kalendarzem, bez świąt (kontrakt
    §8: „sesje od D do dnia generowania"). `d_end <= d_start` -> 0."""
    if d_end <= d_start:
        return 0
    count = 0
    cur = d_start + timedelta(days=1)
    while cur <= d_end:
        if cur.weekday() < 5:
            count += 1
        cur += timedelta(days=1)
    return count

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


# ---------------------------------------------------------------------------
# Rewizja 2 (brief D2): sekcje S1, S3-S9 renderowane STATYCZNIE do HTML
# (po stronie Pythona, zeby test E4 mogl sprawdzic konwencje parserem HTML,
# bez uruchamiania JS). Zasada nadrzedna: dashboard CZYTA, nie liczy metryk -
# ponizsze funkcje tylko formatuja i agreguja (sumy) wartosci z payloadu.
#
# Mechanizm konwencji liczb (zasada 4): kazdy element liczbowy ma klase `num`
# i atrybut `data-conv="waluta|warstwa|zrodlo|data"` (cztery niepuste pola
# rozdzielone `|`, patrz `conv()`). Brak pomiaru = `<span class="na">brak
# pomiaru: <powod></span>` (nigdy 0/NaN/puste). Sekcje rew. 1 (JS) dostaja
# `data-conv` w runtime z atrybutu `data-conv-default` sekcji (template.html).
# ---------------------------------------------------------------------------

import html as _html  # noqa: E402

CONV_COUNT = "bez waluty (licznik)"
CONV_SHARE = "bez waluty (udzial)"
CONV_KS_SHARE = "bez waluty (udzial % kapitalu satelity)"
SECTION_NAMES = ("s3", "s4", "s5a", "s5b", "s6", "s7", "s8", "s9")


def conv(currency: str, layer: str, source: str, on_date: Any) -> str:
    """Konwencja liczby: `waluta|warstwa|zrodlo|data` (zasada 4 briefu)."""
    parts = [currency, layer, source, iso_or_text(on_date)]
    return "|".join(str(p).replace("|", "/") for p in parts)


def iso_or_text(value: Any) -> str:
    if value is None:
        return "brak"
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _e(value: Any) -> str:
    return _html.escape("" if value is None else str(value), quote=True)


def brak_text(reason: str | None) -> str:
    r = (reason or "").strip()
    if r.startswith("brak pomiaru"):
        return r
    return f"brak pomiaru: {r}" if r else "brak pomiaru"


def brak_html(reason: str | None) -> str:
    t = brak_text(reason)
    return f'<span class="na" title="{_e(t)}">{_e(t)}</span>'


def num_html(text: str, conv_str: str) -> str:
    return f'<span class="num" data-conv="{_e(conv_str)}">{_e(text)}</span>'


def fmt_points(value: Any, decimals: int = 2) -> str:
    """Wartosc juz w punktach procentowych (np. risk_pct_satellite_capital)."""
    return format_pl_number(value, decimals) + "%"


def fmt_fraction(value: Any, decimals: int = 2) -> str:
    """Ulamek (np. 0.35) pokazany jako procent - tylko zmiana jednostki."""
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return format_pl_number(d * 100, decimals) + "%"


def _td(inner: str, cls: str = "") -> str:
    return f'<td class="{cls}">{inner}</td>' if cls else f"<td>{inner}</td>"


def _txt(value: Any) -> str:
    return _td(_e(value), "txt")


def _table(headers: list[str], rows: list[str], caption: str | None = None) -> str:
    cap = f'<caption class="note">{caption}</caption>' if caption else ""
    head = "".join(f'<th class="txt">{_e(h)}</th>' for h in headers)
    return (
        f'<div class="table-scroll"><table>{cap}<thead><tr>{head}</tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _kpi(label: str, inner: str, title: str = "") -> str:
    t = f' title="{_e(title)}"' if title else ""
    return f'<div class="kpi"><div class="label">{_e(label)}</div><div class="value small"{t}>{inner}</div></div>'


def _tag(text: str, kind: str, title: str = "") -> str:
    t = f' title="{_e(title)}"' if title else ""
    return f'<span class="tag {kind}"{t}>{_e(text)}</span>'


def _section(sec_id: str, title: str, body: str, conv_footer: str, co_zasili: str | None = None) -> str:
    foot = f'<div class="note" data-role="conv-footer">Konwencja: {_e(conv_footer)}</div>'
    if co_zasili:
        foot += f'<div class="note" data-role="co-zasili">Co zasili: {_e(co_zasili)}</div>'
    return (
        f'<section id="{sec_id}" data-sat="1"><h2>{_e(title)}</h2>'
        f'<div class="panel">{body}{foot}</div></section>'
    )


def _no_d_section(sec_id: str, title: str, reason: str, co_zasili: str | None = None) -> str:
    return _section(sec_id, title, brak_html(reason), "brak pomiaru - nie ma liczb do opisania", co_zasili)


def _risk_unavailable(block: dict[str, Any] | None) -> str | None:
    if block is None:
        return "brak danych sekcji"
    if block.get("status") != "ok":
        return block.get("reason") or "brak oceny ryzyka"
    return None


def brak_sections(sat: dict[str, Any]) -> list[str]:
    """Nazwy sekcji (S3..S9), ktore sa w CALOSCI `brak pomiaru` (do stdout)."""
    if sat.get("no_d"):
        return list(SECTION_NAMES)
    out = ["s6", "s8"]
    for name in ("s3", "s4", "s5a", "s5b", "s7", "s9"):
        blk = sat.get(name)
        if blk is None or blk.get("status") != "ok":
            out.append(name)
    return sorted(out)


# --- S1 naglowek -----------------------------------------------------------


def render_s1_html(sat: dict[str, Any]) -> str:
    s1 = sat.get("s1") or {}
    no_d = sat.get("no_d")
    boxes: list[str] = []
    if s1.get("d") is None:
        boxes.append(_kpi("D skladu", brak_html(no_d or "brak daty D wg B-36")))
    else:
        boxes.append(_kpi(
            "D skladu",
            f'<span class="date">{_e(iso_or_text(s1["d"]))}</span> <span class="nd">({_e(s1.get("d_source"))})</span>',
            "positions_as_of(D); domyslnie mannaz.risk.resolve_default_risk_date (B-36)",
        ))
    if s1.get("d_risk") is None:
        boxes.append(_kpi("Data oceny ryzyka", brak_html("brak oceny ryzyka <= D" if s1.get("d") is not None else (no_d or "brak daty D"))))
    else:
        boxes.append(_kpi(
            "Data oceny ryzyka",
            f'<span class="date">{_e(iso_or_text(s1["d_risk"]))}</span> <span class="nd">({_e(s1.get("risk_source"))})</span>',
            "risk_daily.risk_date; domyslnie max(risk_date) <= D",
        ))
    if s1.get("price_N") is None:
        boxes.append(_kpi("Kompletnosc cen (T27)", brak_html(no_d or "brak daty D")))
    else:
        c = conv(CONV_COUNT, "cena close_raw (T27: ok+stale / wszystkie)", "mannaz.risk.resolve_position_price_coverage", s1.get("d"))
        boxes.append(_kpi(
            "Kompletnosc cen (T27) n/N",
            num_html(f'{s1["price_n"]}/{s1["price_N"]}', c),
            "n = pozycje z kompletna cena wg T27 (ok + stale), N = wszystkie pozycje otwarte na D",
        ))
    kon = s1.get("kontraktowy")
    if kon is None:
        boxes.append(_kpi("Rozliczenie KONTRAKTOWY (M77)", brak_html(no_d or "brak daty D")))
    elif kon.get("ok"):
        boxes.append(_kpi("Rozliczenie KONTRAKTOWY (M77)", _tag("kompletne", "ok"), "mannaz.risk.check_kontraktowy_coverage"))
    else:
        boxes.append(_kpi("Rozliczenie KONTRAKTOWY (M77)", brak_html(kon.get("message") or "niekompletne")))
    boxes.append(_kpi("Wersja kodu (code_sha)", _e(s1.get("code_sha") or "brak (git niedostepny)"), "mannaz.provenance.code_sha()"))
    if s1.get("source_runs_n") is None:
        boxes.append(_kpi("source_runs", brak_html("odczyt nieudany")))
    else:
        c = conv(CONV_COUNT, "metadane importu (bez warstwy cen)", "source_runs", s1.get("source_runs_max_fetched_at") or s1.get("d"))
        fetched = iso_or_text(s1.get("source_runs_max_fetched_at"))[:19]
        boxes.append(_kpi(
            "source_runs: liczba / max(fetched_at)",
            f'{num_html(str(s1["source_runs_n"]), c)} / <span class="date">{_e(fetched)}</span>',
            "count(*) i max(fetched_at) z source_runs",
        ))
    body = f'<div class="grid grid-3">{"".join(boxes)}</div>'
    return _section(
        "sec-s1", "1. Naglowek: daty skladu i oceny ryzyka", body,
        "liczniki bez waluty; zrodla: mannaz.risk (T27, M77), source_runs; data = D skladu / znacznik fetched_at",
    )


# --- S3 ryzyko poziomu 1 per emitent ----------------------------------------


def render_s3_html(sat: dict[str, Any]) -> str:
    title = "S3. Ryzyko poziomu 1 per emitent, budzety (par. 19)"
    if sat.get("no_d"):
        return _no_d_section("sec-s3", title, sat["no_d"])
    s3 = sat.get("s3")
    why = _risk_unavailable(s3)
    if why:
        return _no_d_section("sec-s3", title, why)
    rd = s3["risk_date"]
    c_pct = conv(CONV_KS_SHARE, "cena split_adj (close_d); NAV satelity z gotowka (B-17, M78)", "risk_daily", rd)
    lim = s3["limits"]
    stan = f"stan oceny ryzyka {iso_or_text(rd)}"
    closed = s3.get("closed_since_risk")
    closed_html = (brak_html("brak licznika pozycji zamknietych") if closed is None
                   else num_html(str(closed), conv(CONV_COUNT, "bez warstwy", "risk_daily vs positions_as_of(D)", s3["d"])))
    parts: list[str] = [
        f'<div class="note">Sklad na D: {_e(iso_or_text(s3["d"]))}; ryzyko z ostatniej oceny &lt;= D: '
        f'{_e(iso_or_text(rd))}. Agregaty ponizej ({_e(stan)}) czytane z oceny, nie przeliczane.</div>',
        f'<div class="note" data-role="agg-scope">Agregaty: {_e(stan)}; pozycje budzetowe zamkniete od tej daty '
        f'(w agregatach, poza wierszami): {closed_html}</div>',
    ]
    # poziom 3 + tematy
    kpis: list[str] = []
    if s3.get("sum_open_risk_pct") is None:
        kpis.append(_kpi(f"Poziom 3: suma otwartych ryzyk ({stan})", brak_html("brak sumy w ocenie ryzyka")))
    else:
        over = Decimal(str(s3["sum_open_risk_pct"])) > Decimal(str(lim["l3"]))
        inner = num_html(f'{fmt_points(s3["sum_open_risk_pct"])} / {fmt_points(lim["l3"], 0)}', c_pct)
        if over:
            inner += " " + _tag("przekroczony limit poziomu 3", "bad")
        kpis.append(_kpi(f"Poziom 3: suma otwartych ryzyk ({stan})", inner, "Suma risk_pct_satellite_capital wierszy budzetowych"))
    parts.append(f'<div class="grid grid-3">{"".join(kpis)}</div>')
    # per nazwa (emitent)
    rows: list[str] = []
    for n in s3["names"]:
        if n["pct"] is None:
            pct_cell = brak_html("ryzyko nazwy niepelne (brak risk_pln wiersza), B-19")
            status = brak_html("brak pomiaru: nazwa niepelna")
        else:
            pct_cell = num_html(fmt_points(n["pct"], 3), c_pct)
            status = _tag("NARUSZENIE L1", "bad") if n["breach"] else _tag("w limicie", "ok")
        rows.append("<tr>" + _txt(n["label"]) + _td(pct_cell) + _td(num_html(fmt_points(lim["l1"], 0), c_pct)) + _td(status) +
                    _td(num_html(str(n["n_members"]), conv(CONV_COUNT, "bez warstwy", "risk_daily", rd))) + "</tr>")
    parts.append("<h3>Per emitent (nazwa, B-30/B-32) - " + _e(stan) + "</h3>")
    parts.append(_table(["Emitent", "Ryzyko nazwy % KS", "Limit L1", "Status", "Wiersze w nazwie"], rows) if rows else brak_html("brak wierszy budzetowych w ocenie"))
    # tematy
    parts.append("<h3>Poziom 2 - ryzyko wg tematu (" + _e(stan) + ")</h3>")
    if not s3.get("themes"):
        parts.append(brak_html("brak wierszy budzetowych z policzalnym ryzykiem"))
    else:
        trs = []
        for theme, pct in sorted(s3["themes"].items(), key=lambda kv: -Decimal(str(kv[1]))):
            over = Decimal(str(pct)) > Decimal(str(lim["l2"]))
            cell = num_html(fmt_points(pct), c_pct) + (" " + _tag("przekroczony limit poziomu 2", "bad") if over else "")
            trs.append("<tr>" + _txt(theme) + _td(cell) + _td(num_html(fmt_points(lim["l2"], 0), c_pct)) + "</tr>")
        parts.append(_table(["Temat (biezacy, B-20)", "Ryzyko % KS", "Limit L2"], trs))
    # per pozycja (Q1)
    rows = []
    for p in s3["positions"]:
        if p["state"] == "nowsza":
            cells = brak_html(p["reason"])
            rows.append("<tr>" + _txt(p["name"]) + _txt(p["rachunek"]) + _td(cells) + _td(cells) + _td(cells) + "</tr>")
            continue
        if p["state"] != "ok":
            cells = brak_html(p["reason"])
            rows.append("<tr>" + _txt(p["name"]) + _txt(p["rachunek"]) + _td(cells) + _td(cells) + _td(cells) + "</tr>")
            continue
        rp = brak_html("ryzyko wiersza niepoliczone w ocenie (NULL lub multiplier_missing)") if p["risk_pct"] is None else num_html(fmt_points(p["risk_pct"], 3), c_pct)
        npc = brak_html("ryzyko nazwy niepelne") if p["name_pct"] is None else num_html(fmt_points(p["name_pct"], 3), c_pct)
        br = _tag("L1", "bad") if p["breach"] else ("" if p["breach"] is False else brak_html("brak pomiaru: L1 niepewne"))
        rows.append("<tr>" + _txt(p["name"]) + _txt(p["rachunek"]) + _td(rp) + _td(npc) + _td(br) + "</tr>")
    parts.append("<h3>Per pozycja na D (wiersze z oceny ryzyka " + _e(iso_or_text(rd)) + ")</h3>")
    parts.append(_table(["Pozycja", "Rachunek", "Ryzyko pozycji % KS", "Ryzyko nazwy % KS", "L1"], rows) if rows else brak_html("brak pozycji budzetowych na D"))
    parts.append(
        '<div class="note">Pozycja otwarta lub zmieniona po dacie oceny: brak pomiaru. Pozycja zamknieta po dacie oceny: '
        "nie pokazana w wierszach (wlicza sie w agregaty oceny). Naruszenie limitu = alert, bez sugestii ruchu kapitalu.</div>"
    )
    return _section(
        "sec-s3", title, "".join(parts),
        f"% kapitalu satelity (mianownik: NAV satelity z gotowka, B-17/M78) w walucie PLN; warstwa cena split_adj (close_d); "
        f"zrodlo risk_daily + mannaz.risk.level1_by_name; data oceny {iso_or_text(rd)}; "
        f"agregaty = stan oceny ryzyka {iso_or_text(rd)}, pozycje zamkniete od tej daty: {iso_or_text(closed)}",
    )


# --- S4 stopy efektywne z zapadka -------------------------------------------


def render_s4_html(sat: dict[str, Any]) -> str:
    title = "S4. Stopy efektywne z zapadka (B-52)"
    if sat.get("no_d"):
        return _no_d_section("sec-s4", title, sat["no_d"])
    s4 = sat.get("s4")
    why = _risk_unavailable(s4)
    if why:
        return _no_d_section("sec-s4", title, why)
    rd = s4["risk_date"]
    prev = s4.get("prev")
    parts: list[str] = []
    if prev is None:
        window = brak_html("brak poprzedniej oceny ryzyka (okno dokupien niedostepne)")
    else:
        window = f'okno dokupien: ({_e(iso_or_text(prev))}, {_e(iso_or_text(rd))}]'
    parts.append(f'<div class="note">Stan oceny ryzyka {_e(iso_or_text(rd))}; {window}</div>')
    events = s4.get("events")
    rows: list[str] = []
    for p in s4["positions"]:
        if p["state"] != "ok":
            cell = brak_html(p["reason"])
            rows.append("<tr>" + _txt(p["name"]) + _txt(p["rachunek"]) + _td(cell) + _td(cell) + _td(cell) + "</tr>")
            continue
        if p["stop_effective"] is None:
            stop = brak_html("stop niepoliczony w ocenie (NULL)")
        else:
            stop = num_html(format_pl_number(p["stop_effective"], 4), conv(p["quote_currency"] or "brak waluty notowania", "cena split_adj", "risk_daily.stop_effective", rd))
        src = p["stop_source"]
        src_cell = _e(src) if src else brak_html("brak zrodla stopu")
        if src == "two_n_held":
            src_cell += " " + _tag("zapadka 2N", "neutral", "stop_source=two_n_held (B-52)")
        if prev is None:
            flag = brak_html("brak poprzedniej oceny ryzyka")
        elif p.get("buy_below_stop_date"):
            flag = _tag("dokupienie ponizej stopu", "warn", "load_buy_below_stop_events") + f' <span class="date">{_e(iso_or_text(p["buy_below_stop_date"]))}</span>'
        else:
            flag = "-"
        rows.append("<tr>" + _txt(p["name"]) + _txt(p["rachunek"]) + _td(stop) + _td(src_cell, "txt") + _td(flag, "txt") + "</tr>")
    parts.append(_table(["Pozycja", "Rachunek", "Stop efektywny (waluta notowania)", "Zrodlo stopu", "Dokupienie ponizej stopu"], rows)
                 if rows else brak_html("brak pozycji budzetowych na D"))
    if prev is not None:
        parts.append("<h3>Zdarzenia dokupienia ponizej stopu w oknie</h3>")
        if events is None:
            parts.append(brak_html(f'odczyt zdarzen dokupienia nieudany ({s4.get("events_error") or "?"})'))
        elif not events:
            parts.append('<div class="note">Brak zdarzen w oknie (funkcja zwrocila pusta liste).</div>')
        else:
            trs = []
            for ev in events:
                qc = ev["quote_currency"] or "brak waluty notowania"
                stop_before = num_html(format_pl_number(ev["stop_before"], 4), conv(qc, "cena split_adj", "cycle.load_buy_below_stop_events (stop z oceny)", ev["stop_before_date"]))
                price = brak_html("brak kursu FX") if ev["price_quote"] is None else num_html(format_pl_number(ev["price_quote"], 4), conv(qc, "cena transakcji przeliczona na walute notowania", "transactions + NBP A", ev["txn_date"]))
                trs.append("<tr>" + _txt(ev["name"]) + _txt(ev["rachunek"]) + _td(f'<span class="date">{_e(iso_or_text(ev["txn_date"]))}</span>', "txt") + _td(price) + _td(stop_before) + "</tr>")
            parts.append(_table(["Pozycja", "Rachunek", "Data transakcji", "Cena kupna (waluta notowania)", "Stop przed kupnem"], trs))
    parts.append('<div class="note">Zdarzenie informacyjne; bez sugestii ruchu kapitalu. Pozycja otwarta lub zmieniona po dacie oceny: brak pomiaru.</div>')
    return _section(
        "sec-s4", title, "".join(parts),
        f"stop w walucie notowania instrumentu, warstwa cena split_adj, zrodlo risk_daily.stop_effective / stop_source "
        f"i mannaz.cycle.load_buy_below_stop_events, data oceny {iso_or_text(rd)}",
    )


# --- S5a mandat: rejestr 1 --------------------------------------------------

_COMPONENT_LABELS = {"abs": "abs (obsuniecie bezwzgledne)", "rel_sp500": "rel_sp500 (wzgl. S&P 500 TR)", "rel_spyi": "rel_spyi (wzgl. SPYI)"}


def render_s5a_html(sat: dict[str, Any]) -> str:
    title = "S5a. Mandat: rejestr naruszen 1 (M88/M89)"
    if sat.get("no_d"):
        return _no_d_section("sec-s5a", title, sat["no_d"])
    s5a = sat.get("s5a")
    if s5a is None or s5a.get("status") != "ok":
        return _no_d_section("sec-s5a", title, (s5a or {}).get("reason") or "brak danych rejestru")
    d = s5a["d"]
    c_n = conv(CONV_COUNT, "TWR z NAV satelity (ceny split_adj)", "satellite_mandate_violations / _events, registry=1", d)
    c_pct = conv(CONV_SHARE, "TWR z NAV satelity (ceny split_adj)", "satellite_mandate_violations, registry=1", d)
    parts: list[str] = []
    viol = s5a["violations"]
    total_windows = 0
    rows: list[str] = []
    for comp in ("abs", "rel_sp500", "rel_spyi"):
        limit = s5a["limits"]["abs" if comp == "abs" else "rel"]
        lim_cell = num_html(fmt_fraction(limit, 0), c_pct)
        v = viol["by_component"].get(comp)
        if viol["table_rows_total"] == 0:
            rows.append("<tr>" + _txt(_COMPONENT_LABELS[comp]) + _td(lim_cell) + _td(brak_html("brak pomiaru: rejestr 1 pusty"), "txt") + _td("") + _td("") + _td("") + "</tr>")
        elif v is None:
            rows.append("<tr>" + _txt(_COMPONENT_LABELS[comp]) + _td(lim_cell) + _td("brak naruszen w rejestrze 1", "txt") + _td("") + _td("") + _td("") + "</tr>")
        else:
            total_windows += v["n"]
            n_cell = num_html(str(v["n"]), c_n) + " " + _tag("NARUSZENIE MANDATU", "bad", "alert; bez sugestii ruchu kapitalu")
            rng = f'<span class="date">{_e(iso_or_text(v["window_start_min"]))}</span> .. <span class="date">{_e(iso_or_text(v["window_end_max"]))}</span>'
            rows.append("<tr>" + _txt(_COMPONENT_LABELS[comp]) + _td(lim_cell) + _td(n_cell, "txt") + _td(rng, "txt") + _txt(v["data_status"]) +
                        _td(f'<span class="date">{_e(iso_or_text(v["first_seen_max"]))[:19]}</span>', "txt") + "</tr>")
    ends = [v["window_end_max"] for v in viol["by_component"].values() if v.get("window_end_max") is not None]
    scope = (f"pelne tygodnie do {iso_or_text(max(ends))}" if ends else "brak okien z naruszeniem w rejestrze 1")
    parts.append(f'<div class="note">Stan rejestru na D = {_e(iso_or_text(d))} (wiersze z window_end / event_date &lt;= D); rejestr tylko do dopisywania.</div>')
    parts.append(f'<div class="note" data-role="violations-scope">Zakres liczenia naruszen: {_e(scope)} '
                 "(rejestr 1 zapisuje okna konczace sie na pelnych tygodniach; biezacy tydzien poza rejestrem - zob. S5b).</div>")
    parts.append("<h3>Naruszenia mandatu (okna 24 m. konczace sie w window_end)</h3>")
    parts.append(_table(["Komponent", "Limit", "Okna z naruszeniem", "Zakres okien (window_start min .. window_end max)", "data_status", "max(first_seen_run_at)"], rows))
    # zdarzenia
    ev = s5a["events"]
    rows = []
    for comp in ("abs", "rel_sp500", "rel_spyi"):
        e = ev["by_component"].get(comp)
        if ev["table_rows_total"] == 0:
            rows.append("<tr>" + _txt(_COMPONENT_LABELS[comp]) + _td(brak_html("brak pomiaru: rejestr 1 pusty"), "txt") + _td("") + _td("") + _td("") + "</tr>")
        elif e is None:
            rows.append("<tr>" + _txt(_COMPONENT_LABELS[comp]) + _td("brak naruszen w rejestrze 1", "txt") + _td("") + _td("") + _td("") + "</tr>")
        else:
            cells = f'ostrzezenie: {num_html(str(e["n_warn"]), c_n)}, alert: {num_html(str(e["n_alert"]), c_n)}'
            rng = f'<span class="date">{_e(iso_or_text(e["date_min"]))}</span> .. <span class="date">{_e(iso_or_text(e["date_max"]))}</span>'
            rows.append("<tr>" + _txt(_COMPONENT_LABELS[comp]) + _td(cells, "txt") + _td(rng, "txt") + _txt(e["data_status"]) +
                        _td(f'<span class="date">{_e(iso_or_text(e["first_seen_max"]))[:19]}</span>', "txt") + "</tr>")
    parts.append("<h3>Zdarzenia alertow (ostrzezenie 75%, alert 100%)</h3>")
    parts.append(_table(["Komponent", "Zdarzenia", "Zakres dat", "data_status", "max(first_seen_run_at)"], rows))
    # okna
    wins = viol.get("windows") or []
    if wins:
        trs = []
        for w in wins:
            trs.append("<tr>" + _txt(w["component"]) + _td(f'<span class="date">{_e(iso_or_text(w["window_start"]))}</span> .. <span class="date">{_e(iso_or_text(w["window_end"]))}</span>', "txt") +
                       _td(num_html(fmt_fraction(w["max_dd"]), c_pct)) + _td(num_html(fmt_fraction(w["limit_value"], 0), c_pct)) + _txt(w["data_status"]) + "</tr>")
        parts.append(f'<details><summary>Okna z naruszeniem ({len(wins)})</summary>' +
                     _table(["Komponent", "Okno", "Max obsuniecie", "Limit", "data_status"], trs) + "</details>")
    return _section(
        "sec-s5a", title, "".join(parts),
        f"obsuniecia jako udzial (%), bez waluty; warstwa TWR z NAV satelity (ceny split_adj); zrodlo tabele registry=1 "
        f"(satellite_mandate_violations, satellite_mandate_events); limity L_MAX/L_REL z mannaz.satellite; data D {iso_or_text(d)}; "
        f"zakres liczenia naruszen: {scope}",
    )


# --- S5b wynik satelity z modulu --------------------------------------------


def _status_tag(s5b: dict[str, Any]) -> str:
    if s5b["data_verified"]:
        return _tag("zweryfikowane", "ok", "report.data_verified")
    return _tag("niezweryfikowane", "warn", "report.data_verified = False")


def render_s5b_html(sat: dict[str, Any]) -> str:
    title = "S5b. Wynik satelity z modulu (mannaz.satellite)"
    if sat.get("no_d"):
        return _no_d_section("sec-s5b", title, sat["no_d"])
    s5b = sat.get("s5b")
    if s5b is None or s5b.get("status") != "ok":
        return _no_d_section("sec-s5b", title, (s5b or {}).get("reason") or "brak danych modulu satelity")
    d = s5b["d"]
    layer = "TWR z NAV satelity (ceny split_adj x layer_factor_after)"
    src = "mannaz.satellite.run_satellite_stage (report)"
    c_twr = conv(CONV_SHARE, layer, src, d)
    c_pln = conv("PLN", layer, src, d)
    st = _status_tag(s5b)
    reasons = s5b.get("reasons") or []
    gate = s5b.get("gate")
    gate_txt = brak_text("brak bramki M78 (nie podano account_values)") if gate is None else f'bramka M78: wynik {gate.get("result")}, ok={gate.get("ok")}'
    banner = f'<div class="risk-banner {"ok" if s5b["data_verified"] else "bad"}" data-role="data-status">Status danych: {st} - {_e(gate_txt)}'
    if reasons:
        banner += "<ul class=\"plain\">" + "".join(f"<li>{_e(r)}</li>" for r in reasons) + "</ul>"
    banner += "</div>"
    parts: list[str] = [banner]
    s_date = iso_or_text(s5b.get("S"))
    parts.append(f'<div class="note">D = {_e(iso_or_text(d))} (to samo D co sklad). '
                 f'<span data-role="twr-label">TWR od S = {_e(s_date)}</span> (okno od_S: {_e(s_date)} .. {_e(iso_or_text(d))}).</div>')
    parts.append(f'<div class="note" data-role="violations-scope">Zakres liczenia naruszen: z biezacym tygodniem do D = '
                 f'{_e(iso_or_text(d))} (okna od_S i 24m konczace sie w D; modul liczy na sucho, write=False).</div>')
    rows: list[str] = []
    for wname in ("od_S", "24m"):
        w = s5b["windows"].get(wname)
        if w is None:
            rows.append("<tr>" + _txt(wname) + _td(brak_html("brak okna w raporcie modulu"), "txt") + "</tr>")
            continue
        cells = [_txt(f'{wname}: {w["from"]} .. {w["to"]}')]
        cells.append(_td(brak_html("brak TWR (okno niepelne)") if w["twr"] is None else num_html(fmt_fraction(w["twr"]), c_twr)))
        for key in ("sp500", "spyi"):
            b = w["bench"].get(key)
            if b is None or b["delta_w_pln"] is None:
                cells.append(_td(brak_html(f"brak delta W ({key})")))
            else:
                inner = num_html(format_pl_number(b["delta_w_pln"], 2), c_pln)
                if b.get("infeasible"):
                    inner += " " + _tag("benchmark niewykonalny bez finansowania", "neutral")
                cells.append(_td(inner))
        cells.append(_td(brak_html("brak max_dd") if w["max_dd_abs"] is None else num_html(fmt_fraction(w["max_dd_abs"]) + " / " + fmt_fraction(s5b["limits"]["abs"], 0), c_twr)))
        for key in ("sp500", "spyi"):
            b = w["bench"].get(key)
            if b is None or b["max_dd_rel"] is None:
                cells.append(_td(brak_html(f"brak max_dd rel ({key})")))
            else:
                cells.append(_td(num_html(fmt_fraction(b["max_dd_rel"]) + " / " + fmt_fraction(s5b["limits"]["rel"], 0), c_twr)))
        m = w["mandate"]
        viol = ", ".join(m["violations"]) if m["violations"] else "brak"
        mt = _tag(str(m["status"]), "bad" if m["violations"] else "neutral") + f' <span class="nd">naruszenia: {_e(viol)}</span>'
        cells.append(_td(mt, "txt"))
        rows.append("<tr>" + "".join(cells) + "</tr>")
    cap = f'Status danych: {st}'
    parts.append(_table(
        ["Okno", "TWR", "delta W vs S&P 500 TR (PLN)", "delta W vs SPYI (PLN)", "Max DD abs / limit",
         "Max DD rel S&P 500 / limit", "Max DD rel SPYI / limit", "Mandat (M88)"], rows, caption=cap))
    # alerty
    al_rows = []
    for comp, a in s5b["alerts"].items():
        cd = a.get("current_drawdown")
        cell = brak_html("brak biezacego obsuniecia") if cd is None else num_html(fmt_fraction(cd) + " / " + fmt_fraction(a["limit"], 0), c_twr)
        al_rows.append("<tr>" + _txt(_COMPONENT_LABELS.get(comp, comp)) + _td(cell) + _txt(a.get("state")) + "</tr>")
    parts.append("<h3>Biezace obsuniecie od maksimum od S (alerty M89)</h3>")
    parts.append(_table(["Skladnik", "Obsuniecie biezace / limit", "Stan alertu"], al_rows, caption=f"Status danych: {st}") if al_rows else brak_html("brak alertow w raporcie modulu"))
    conv_footer = (
        f"{s5b['conventions_currency']}; warstwa {layer}; zrodlo {src}, progi: "
        + "; ".join(f"{k}: {v}" for k, v in s5b["benchmarks"].items())
        + f"; data D {iso_or_text(d)}; limity L_MAX={fmt_fraction(s5b['limits']['abs'], 0)}, L_REL={fmt_fraction(s5b['limits']['rel'], 0)}"
        + f"; TWR od S = {iso_or_text(s5b.get('S'))}; zakres liczenia naruszen: z biezacym tygodniem do D = {iso_or_text(d)}"
    )
    return _section("sec-s5b", title, "".join(parts), conv_footer)


# --- S6 / S8 brak pomiaru ----------------------------------------------------


def render_s6_html(sat: dict[str, Any]) -> str:
    title = "S6. P/E NTM"
    reason = sat.get("no_d") or "brak zrodla w bazie (P/E NTM ze statusem P-17 nie jest zapisywany)"
    return _no_d_section("sec-s6", title, reason, "B-89: cykl zapisuje P/E NTM ze statusem P-17 do tabeli, dashboard czyta")


def render_s8_html(sat: dict[str, Any]) -> str:
    title = "S8. Silnik stanu TREND"
    reason = sat.get("no_d") or "brak zrodla w bazie (silnik stanu TREND nie zapisuje wynikow)"
    return _no_d_section("sec-s8", title, reason, "B-90: silnik stanu TREND z zapisem w bazie")


# --- S7 archetyp i temat -----------------------------------------------------


def render_s7_html(sat: dict[str, Any]) -> str:
    title = "S7. Archetyp i temat"
    if sat.get("no_d"):
        return _no_d_section("sec-s7", title, sat["no_d"])
    s7 = sat.get("s7")
    if s7 is None or s7.get("status") != "ok":
        return _no_d_section("sec-s7", title, (s7 or {}).get("reason") or "brak danych archetypow")
    rows = []
    for r in s7["rows"]:
        if r["state"] == "ok":
            arch = _e(r["archetype"])
            key = _e(r["archetype_key"]) if r["archetype_key"] else brak_html("brak archetype_key")
            ver = _e(r["key_version"]) if r["key_version"] else brak_html("brak key_version")
        else:
            arch = key = ver = brak_html(r["reason"])
        theme = _e(r["theme"]) if r["theme"] else brak_html("instruments.theme NULL")
        rows.append("<tr>" + _txt(r["name"]) + _td(arch, "txt") + _td(key, "txt") + _td(ver, "txt") + _td(theme, "txt") + "</tr>")
    body = _table(["Pozycja (instrument)", "Archetyp", "archetype_key", "key_version", "Temat (biezacy, B-20)"], rows) if rows else brak_html("brak pozycji na D")
    return _section(
        "sec-s7", title, body,
        f"sekcja bez liczb; archetype_assignments: wiersz aktywny na D {iso_or_text(s7['d'])} (valid_from <= D, valid_to NULL lub > D); "
        "instruments.theme bez historii (B-20)",
    )


# --- S9 rozklad akcyjny vs walutowy -----------------------------------------


def render_s9_html(sat: dict[str, Any]) -> str:
    title = "S9. Rozklad akcyjny vs walutowy (atrybucja M85)"
    if sat.get("no_d"):
        return _no_d_section("sec-s9", title, sat["no_d"])
    s9 = sat.get("s9")
    if s9 is None or s9.get("status") != "ok":
        return _no_d_section("sec-s9", title, (s9 or {}).get("reason") or "modul nie zwrocil atrybucji")
    d = s9["d"]
    c = conv("PLN", "atrybucja M85 (NAV, ceny split_adj; FX NBP A)", "mannaz.satellite report.attribution", d)
    parts: list[str] = []
    rows = []
    for wname in ("od_S", "24m"):
        w = s9["windows"].get(wname)
        if w is None:
            rows.append("<tr>" + _txt(wname) + _td(brak_html("brak okna w atrybucji"), "txt") + "</tr>")
            continue
        flag = _tag("kontrola pelna", "ok") if w["complete"] else _tag("okno niepelne", "warn", "control.complete = False")
        rows.append("<tr>" + _txt(f'{wname}: {w["from"]} .. {w["to"]}') + _td(num_html(format_pl_number(w["sum_lokalny"], 2), c)) +
                    _td(num_html(format_pl_number(w["sum_fx"], 2), c)) + _td(num_html(format_pl_number(w["sum_total"], 2), c)) + _td(flag, "txt") + "</tr>")
    parts.append("<h3>Suma wierszy by_account: komponent lokalny (akcyjny) vs walutowy (fx)</h3>")
    parts.append(_table(["Okno", "Lokalny (akcyjny) PLN", "FX (walutowy) PLN", "Total PLN", "control.complete"], rows))
    for wname in ("od_S", "24m"):
        w = s9["windows"].get(wname)
        if not w:
            continue
        trs = []
        for r in w["by_currency"]:
            trs.append("<tr>" + _txt(r["key"]) + _td(num_html(format_pl_number(r["lokalny"], 2), c)) + _td(num_html(format_pl_number(r["fx"], 2), c)) +
                       _td(num_html(format_pl_number(r["total"], 2), c)) + "</tr>")
        parts.append(f"<h3>by_currency, okno {_e(wname)}</h3>")
        parts.append(_table(["Waluta", "Lokalny PLN", "FX PLN", "Total PLN"], trs) if trs else brak_html("brak wierszy by_currency"))
    return _section(
        "sec-s9", title, "".join(parts),
        f"PLN; warstwa NAV (ceny split_adj, FX NBP A); zrodlo mannaz.satellite report.attribution; data D {iso_or_text(d)}; {s9['conventions']}",
    )


def render_sections_html(payload: dict[str, Any]) -> str:
    sat = payload["satellites"]
    return "\n".join([
        render_s3_html(sat), render_s4_html(sat), render_s5a_html(sat), render_s5b_html(sat),
        render_s6_html(sat), render_s7_html(sat), render_s8_html(sat), render_s9_html(sat),
    ])

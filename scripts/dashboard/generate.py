"""Generator lokalnego dashboardu portfela satelity (brief CC-D, krok D4').

Czyta WYŁĄCZNIE z bazy Postgres, w trybie read-only, bez sieci. Renderuje
JEDEN samodzielny plik HTML (CSS+JS inline, dane osadzone jako JSON).

Kontrakt danych — AUTORYTATYWNY, rozstrzyga wątpliwości:
    tmp/dashboard/kontrakt-danych.md (poza gitem, katalog roboczy Mannaz)

Cena na D (kontrakt §3, D2'): WYŁĄCZNIE przez `mannaz.risk.open_positions_as_of`
+ `mannaz.risk.resolve_position_price_coverage` (reguła T27 — forward-fill
maks. 2 sesje, tylko rynek zamknięty, zawsze flaga `stale`; w przeciwnym razie
`incomplete` z powodem). Stara logika "ostatnia cena ≤ D bez limitu" jest
USUNIĘTA — pozycja bez kompletu wg T27 zostaje w tabeli (ilość + flaga), ale
jej wartość PLN to `brak pomiaru: <powód>` i NIE wchodzi cicho do żadnej sumy
(BW/KS/nominał FUT: wszystko-albo-nic z licznikiem n/N, patrz `model.py`).

Użycie (z katalogu worktree, interpreter z .venv — globalny python nie ma
psycopg):
    C:\\Users\\MariuszBrysik\\projects\\Mannaz\\.venv\\Scripts\\python.exe ^
        scripts\\dashboard\\generate.py --date 2026-09-25 --risk-date 2026-09-25 ^
        --out <plik.html>

WYJŚCIA (kody):
    0 — sukces, plik zapisany
    2 — STOP: waluta nieobsłużona (kontrakt §3) — bez zapisu pliku
    3 — STOP: dane starsze niż 5 sesji (kontrakt §8) — bez zapisu pliku
    1 — inny błąd (wyjątek nieprzewidziany)

Bezpieczeństwo (repo PUBLICZNE — patrz brief D4):
    - stdout/logi: WYŁĄCZNIE liczności, daty, sha256, PASS/FAIL. Żadnych
      kwot/wag/cen/tickerów/numerów rachunków.
    - `conn.read_only = True` ustawione natychmiast po połączeniu, zero
      zapisów do bazy.
    - `rachunek` (z `transactions`/`positions_fifo`) zawiera numer rachunku —
      ten string NIGDY nie opuszcza tego pliku poza `model.account_label()`
      (pierwsze słowo); nie trafia do payloadu ani do żadnego printu.

Konwencja liczb w JSON osadzonym w HTML: KAŻDA wartość pochodząca z
`Decimal` jest serializowana jako STRING (zachowanie precyzji, zero
zaokrągleń niejawnych przez float) — patrz `_json_default`. JS w
template.html parsuje przez `Number()`/`parseFloat()` do formatowania i
wykresów. Daty/znaczniki czasu -> ISO 8601 string.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# --- import z src/ WORKTREE (ścieżka względem TEGO pliku, nie CWD) ---
_SCRIPT_DIR = Path(__file__).resolve().parent
_SRC = _SCRIPT_DIR.parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import model  # scripts/dashboard/model.py — czyste funkcje, bez DB

from mannaz.db import get_connection
from mannaz.fifo import positions_as_of
from mannaz.risk import (
    read_kontraktowy_rows,  # kontrakt §2: import i wywołanie wprost, bez zmian w kodzie
    open_positions_as_of,
    resolve_position_price_coverage,
    check_kontraktowy_coverage,
    is_risk_budget_eligible,
    kontraktowy_account_value,
)

TEMPLATE_PATH = _SCRIPT_DIR / "template.html"
JSON_PLACEHOLDER = "__DASHBOARD_DATA_JSON__"
# Etykieta "nie dotyczy" (nie luka) — szablon rozpoznaje ją po prefiksie "nie dotyczy".
RESULT_A_FUT_NIE_DOTYCZY = "nie dotyczy: wynik rozliczany dziennie w środkach rachunku (§19.4)"


class DashboardStop(Exception):
    """STOP kontrolowany (kontrakt §3/§8) — kod wyjścia + komunikat BEZ
    żadnej kwoty/wagi/ceny/tickera."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Dostęp do bazy — same proste SELECT-y, read-only. Żadnej logiki domenowej
# tutaj (to zadanie model.py) poza złożeniem surowych wierszy. Cena/FX na D
# wg T27 pochodzi WYŁĄCZNIE z `mannaz.risk.resolve_position_price_coverage`
# (patrz moduł docstring) — tu tylko instrumenty (isin/nazwa) i FX waluty
# ROZLICZENIA (osobne od FX waluty notowania, którą niesie coverage).
# ---------------------------------------------------------------------------


def _fetch_instruments(cur, ids: list[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    cur.execute(
        """
        SELECT id, broker_ticker, name, isin, yahoo_symbol, currency, instrument_type,
               multiplier, contract_expiry, base_symbol, theme, is_core
        FROM instruments WHERE id = ANY(%s)
        """,
        (list(ids),),
    )
    cols = (
        "id", "broker_ticker", "name", "isin", "yahoo_symbol", "currency", "instrument_type",
        "multiplier", "contract_expiry", "base_symbol", "theme", "is_core",
    )
    out: dict[int, dict[str, Any]] = {}
    for row in cur.fetchall():
        d = dict(zip(cols, row))
        out[d["id"]] = d
    return out


def _fetch_close_raw_exact(cur, instrument_id: int, price_date: date) -> Decimal | None:
    """Cena na D wg T27 (kontrakt §3): `prices_daily.close_raw` DOKŁADNIE na
    `coverage.price_date_used` (nigdy "ostatnia ≤ D bez limitu" — ta reguła
    jest USUNIĘTA, patrz docstring modułu)."""
    cur.execute(
        "SELECT close_raw FROM prices_daily WHERE instrument_id = %s AND price_date = %s",
        (instrument_id, price_date),
    )
    row = cur.fetchone()
    return row[0] if row else None


def _fetch_last_fx(cur, currency: str, d: date) -> tuple[Decimal | None, date | None]:
    if currency == "PLN":
        return Decimal(1), d
    cur.execute(
        "SELECT rate, rate_date FROM fx_nbp WHERE pair = %s AND rate_date <= %s ORDER BY rate_date DESC LIMIT 1",
        (f"{currency}/PLN", d),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


def _currency_has_any_fx_data(cur, currency: str) -> bool:
    if currency == "PLN":
        return True
    cur.execute("SELECT 1 FROM fx_nbp WHERE pair = %s LIMIT 1", (f"{currency}/PLN",))
    return cur.fetchone() is not None


class _Caches:
    """Pamięć podręczna zapytań w obrębie jednego przebiegu — 56 pozycji, ale
    kilka wspólnych walut/dat cenowych, nie ma sensu pytać bazy wielokrotnie
    o to samo (waluta, D) / (instrument_id, price_date_used)."""

    def __init__(self, cur, d: date):
        self.cur = cur
        self.d = d
        self.fx: dict[str, tuple[Decimal | None, date | None]] = {}
        self.fx_supported: dict[str, bool] = {}
        self.close_raw: dict[tuple[int, date], Decimal | None] = {}

    def fx_for(self, currency: str) -> tuple[Decimal | None, date | None]:
        if currency not in self.fx:
            self.fx[currency] = _fetch_last_fx(self.cur, currency, self.d)
        return self.fx[currency]

    def fx_supported_for(self, currency: str) -> bool:
        if currency not in self.fx_supported:
            self.fx_supported[currency] = _currency_has_any_fx_data(self.cur, currency)
        return self.fx_supported[currency]

    def close_raw_at(self, instrument_id: int, price_date: date) -> Decimal | None:
        key = (instrument_id, price_date)
        if key not in self.close_raw:
            self.close_raw[key] = _fetch_close_raw_exact(self.cur, instrument_id, price_date)
        return self.close_raw[key]


def _resolve_fx(caches: _Caches, currency: str) -> tuple[Decimal | None, date | None]:
    """Zwraca (współczynnik_FX_albo_None, data_kursu_albo_None) dla waluty
    ROZLICZENIA (wynik (a), kontrakt §3) — NIEZALEŻNE od FX waluty notowania,
    który niesie `PositionPriceCoverage.fx_rate`. Rzuca
    `model.UnhandledCurrencyError` (STOP) dla waluty strukturalnie
    nieobsługiwanej — łapane przez wywołującego najwyższego poziomu."""
    rate, rate_date = caches.fx_for(currency)
    supported = caches.fx_supported_for(currency)
    factor = model.resolve_fx_factor(currency, rate, supported)
    return factor, rate_date


def _check_currency_stop(caches: _Caches, currency: str) -> None:
    """Efekt uboczny WYŁĄCZNIE: STOP (kontrakt §3) dla waluty pence
    (GBp/GBX) albo bez JAKIEJKOLWIEK pary w `fx_nbp`. Niezależne od
    kompletności ceny wg T27 — `resolve_position_price_coverage` traktuje
    brak kursu FX jako `incomplete` (miękko, licznik n/N), a NIE jako STOP;
    to dwa różne pojęcia (kontrakt §3, wiersze "FX" i "waluta nieobsłużona").
    Zwrócona wartość jest ignorowana — realny kurs waluty notowania pochodzi
    z `coverage.fx_rate` (T27), ten wywołanie służy tylko wykryciu STOP."""
    supported = caches.fx_supported_for(currency)
    model.resolve_fx_factor(currency, None, supported)


def _build_raw_position(
    cur,
    caches: _Caches,
    pos: dict[str, Any],
    instr_extra: dict[str, Any],
    extra: dict[str, Any] | None,
    d: date,
) -> dict[str, Any]:
    """`pos`: wiersz `open_positions_as_of` (rachunek/instrument_id/qty/typ/
    is_core/base_symbol/multiplier/yahoo_symbol/quote_currency/theme/exchange
    + settlement_currency). `instr_extra`: wiersz `instruments` (isin, name).
    `extra`: wiersz `positions_as_of` dla tego samego (rachunek, instrument_id,
    currency) — residual_cost/entry daty (`open_positions_as_of` ich nie
    niesie, kontrakt §3 wymaga jawnego join-a z powrotem)."""
    rachunek_label = model.account_label(pos["rachunek"])
    instrument_type = pos["instrument_type"]
    is_core = bool(pos["is_core"])
    settlement_currency = pos["settlement_currency"]
    quote_currency = pos["quote_currency"]
    is_future = instrument_type == "future"
    qty = pos["qty"]

    residual_cost = extra["residual_cost"] if extra else None
    first_entry_date = extra["first_entry_date"] if extra else None
    last_entry_date = extra["last_entry_date"] if extra else None

    # STOP (kontrakt §3): waluta nieobsłużona — sprawdzane NIEZALEŻNIE od
    # kompletności ceny T27 (patrz docstring `_check_currency_stop`).
    _check_currency_stop(caches, quote_currency)
    _check_currency_stop(caches, settlement_currency)

    coverage, incomplete = resolve_position_price_coverage(cur, pos, d)

    close_raw: Decimal | None = None
    fx_rate: Decimal | None = None
    fx_rate_date: date | None = None
    price_date_used: date | None = None
    incomplete_reason: str | None = None

    if incomplete is not None:
        price_status = "incomplete"
        incomplete_reason = incomplete.reason
    else:
        # Instrument bazowy (FUT) może mieć inną walutę niż sam kontrakt —
        # znana dopiero TERAZ (po `resolve_position_price_coverage`).
        # OGRANICZENIE (patrz raport końcowy): jeśli baza jest pence i
        # dlatego FX bazy jest None, coverage zwróci `incomplete`
        # (fx_rate_missing) zamiast STOP — ten wariant nie jest tu
        # wykrywany jako STOP, tylko jako "brak pomiaru" (pytanie otwarte,
        # w praktyce KONTRAKTOWY jest PLN wg census D2').
        _check_currency_stop(caches, coverage.quote_currency)
        price_status = "stale" if coverage.price_is_stale else "ok"
        price_date_used = coverage.price_date_used
        fx_rate = coverage.fx_rate
        fx_rate_date = coverage.fx_rate_date
        close_raw = caches.close_raw_at(coverage.price_instrument_id, price_date_used)

    value_pln: Decimal | None = None
    value_pln_brak: str | None = None
    result_a: model.ResultA

    if is_future:
        # Decyzja (pytanie otwarte, patrz raport końcowy): "wartość PLN"
        # generyczna (§3) nie jest liczona dla FUT — wielkość analogiczna to
        # "nominał" (§7), z mnożnikiem, osobna sekcja Kontrakty. "Wynik (a)"
        # dla KONTRAKTOWY nie dotyczy (decyzja planisty po raporcie D-r1):
        # broker rozlicza wynik zmienny dziennie w środkach rachunku (§19.4),
        # więc to nie jest luka danych — poza licznikami luk w stopce.
        value_pln_brak = "nie dotyczy FUT — patrz nominał w sekcji Kontrakty (§7)"
        result_a = model.ResultA(None, None, False, RESULT_A_FUT_NIE_DOTYCZY)
    elif price_status == "incomplete":
        # Kontrakt §3: pozycja ZOSTAJE w tabeli z ilością i flagą; wartość =
        # "brak pomiaru: <powód T27>"; NIE wypada z mianownika BW.
        value_pln_brak = f"brak pomiaru: {incomplete_reason}"
        result_a = model.ResultA(None, None, False, f"brak pomiaru: {incomplete_reason} (T27)")
    else:
        value_pln = qty * close_raw * fx_rate
        fx_settlement_factor: Decimal | None
        try:
            fx_settlement_factor, _ = _resolve_fx(caches, settlement_currency)
        except model.UnhandledCurrencyError:
            fx_settlement_factor = None
        result_a = model.compute_result_a(
            qty=qty,
            close_raw=close_raw,
            residual_cost=residual_cost,
            quote_currency=quote_currency,
            settlement_currency=settlement_currency,
            fx_quote=fx_rate,
            fx_settlement=fx_settlement_factor,
        )

    market = model.market_from_yahoo_symbol(pos["yahoo_symbol"])
    price_age_days = (d - price_date_used).days if price_date_used else None

    return {
        "instrument_id": pos["instrument_id"],
        "rachunek": rachunek_label,
        "broker_ticker": pos["broker_ticker"],
        "name": instr_extra["name"] or pos["broker_ticker"] if instr_extra else pos["broker_ticker"],
        "isin": instr_extra["isin"] if instr_extra else None,
        "yahoo_symbol": pos["yahoo_symbol"],
        "quote_currency": quote_currency,
        "settlement_currency": settlement_currency,
        "instrument_type": instrument_type,
        "is_core": is_core,
        "theme": pos["theme"],
        "market": market,
        "qty": qty,
        "residual_cost": residual_cost,
        "first_entry_date": first_entry_date,
        "last_entry_date": last_entry_date,
        "close_raw": close_raw,
        "price_status": price_status,  # "ok" | "stale" | "incomplete" (T27, kontrakt §3/§4)
        "price_is_stale": price_status == "stale",
        "price_date_used": price_date_used,
        "incomplete_reason": incomplete_reason,
        "price_age_days": price_age_days,
        "fx_rate": fx_rate,
        "fx_rate_date": fx_rate_date,
        "value_pln": value_pln,
        "value_pln_brak": value_pln_brak,
        "result_a": asdict(result_a),
        "multiplier": pos["multiplier"],
        "base_symbol": pos["base_symbol"],
        "base_close_raw": close_raw if is_future else None,
        "base_fx_rate": fx_rate if is_future else None,
        "base_price_date": price_date_used if is_future else None,
    }


# ---------------------------------------------------------------------------
# Ryzyko — 3 testy pochodzenia (kontrakt §5 rew. 3, tri-state test 3) +
# agregaty, jeśli PASS
# ---------------------------------------------------------------------------


def _evaluate_provenance(cur, d_risk: date) -> model.ProvenanceResult:
    cur.execute("SELECT computed_at FROM risk_daily WHERE risk_date = %s", (d_risk,))
    computed_at_values = [row[0] for row in cur.fetchall()]
    test1_ok = model.evaluate_test1_single_run(computed_at_values)

    cur.execute(
        "SELECT rachunek, instrument_id, settlement_currency FROM risk_daily WHERE risk_date = %s",
        (d_risk,),
    )
    risk_daily_keys = {tuple(row) for row in cur.fetchall()}
    positions_risk_date = positions_as_of(cur, d_risk)
    positions_keys = {(p["rachunek"], p["instrument_id"], p["currency"]) for p in positions_risk_date}
    test2_ok = model.evaluate_test2_position_set_match(risk_daily_keys, positions_keys)

    cur.execute("SELECT rachunek, instrument_id, currency FROM positions_fifo WHERE qty <> 0")
    current_open_keys = {tuple(row) for row in cur.fetchall()}
    closed_since_keys = positions_keys - current_open_keys

    # Decyzja ownera (nadpisuje kontrakt §5 pkt 4 brief-u): test 3 = "nie
    # dotyczy" gdy w `transactions` w ogóle nie ma wiersza po D ryzyka
    # (dowolny rachunek/instrument/typ) — kontrola dodatnia nie ma czego
    # sprawdzić Z DEFINICJI, nie dopiero gdy nie znaleziono pozycji zamkniętej.
    cur.execute("SELECT 1 FROM transactions WHERE transaction_date > %s LIMIT 1", (d_risk,))
    has_transactions_after_risk_date = cur.fetchone() is not None

    closed_positions_provenance: list[dict[str, Any]] = []
    for rachunek, instrument_id, currency in closed_since_keys:
        cur.execute(
            "SELECT computed_at FROM risk_daily WHERE rachunek = %s AND instrument_id = %s "
            "AND settlement_currency = %s AND risk_date = %s",
            (rachunek, instrument_id, currency, d_risk),
        )
        row = cur.fetchone()
        computed_at = row[0] if row else None
        cur.execute(
            "SELECT max(created_at) FROM transactions WHERE rachunek = %s AND instrument_id = %s "
            "AND currency = %s AND row_type = 'sprzedaz' AND transaction_date > %s",
            (rachunek, instrument_id, currency, d_risk),
        )
        (max_sell_created_at,) = cur.fetchone()
        closed_positions_provenance.append(
            {
                "has_risk_daily_row": row is not None,
                "computed_at": computed_at,
                "max_sell_created_at": max_sell_created_at,
            }
        )
    test3_status = model.evaluate_test3_closed_positions_provenance(
        has_transactions_after_risk_date, closed_positions_provenance
    )

    return model.evaluate_provenance(test1_ok, test2_ok, test3_status)


def _fetch_risk_rows(cur, d_risk: date) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT r.rachunek, r.instrument_id, r.settlement_currency, r.quote_currency,
               r.risk_state, r.stop_effective, r.stop_source, r.risk_pct_satellite_capital,
               r.level1_breach, r.multiplier_missing, r.close_d, r.regime, r.warning,
               r.risk_pln, r.note, r.computed_at, r.price_is_stale, r.price_date_used,
               i.instrument_type, i.is_core, i.theme,
               r.name_key, r.name_risk_pct, n.broker_ticker AS name_label, i.broker_ticker
        FROM risk_daily r
        JOIN instruments i ON i.id = r.instrument_id
        LEFT JOIN instruments n ON n.id = r.name_key
        WHERE r.risk_date = %s
        """,
        (d_risk,),
    )
    cols = (
        "rachunek", "instrument_id", "settlement_currency", "quote_currency",
        "risk_state", "stop_effective", "stop_source", "risk_pct_satellite_capital",
        "level1_breach", "multiplier_missing", "close_d", "regime", "warning",
        "risk_pln", "note", "computed_at", "price_is_stale", "price_date_used",
        "instrument_type", "is_core", "theme",
        "name_key", "name_risk_pct", "name_label", "broker_ticker",
    )
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Orkiestracja
# ---------------------------------------------------------------------------


def build_dashboard_payload(conn, d: date, d_risk: date) -> tuple[dict[str, Any], dict[str, Any]]:
    with conn.cursor() as cur:
        open_positions = open_positions_as_of(cur, d)
        instrument_ids = sorted({p["instrument_id"] for p in open_positions})
        instruments = _fetch_instruments(cur, instrument_ids)
        # Kontrakt §3: `open_positions_as_of` nie niesie residual_cost/daty
        # wejścia — join z powrotem do `positions_as_of` po (rachunek,
        # instrument_id, currency).
        extra_rows = positions_as_of(cur, d)
        extra_by_key = {(e["rachunek"], e["instrument_id"], e["currency"]): e for e in extra_rows}
        caches = _Caches(cur, d)

        rows: list[dict[str, Any]] = []
        for p in open_positions:
            key = (p["rachunek"], p["instrument_id"], p["settlement_currency"])
            extra = extra_by_key.get(key)
            try:
                row = _build_raw_position(cur, caches, p, instruments.get(p["instrument_id"]), extra, d)
            except model.UnhandledCurrencyError as exc:
                raise DashboardStop(2, str(exc)) from exc
            rows.append(row)

        # --- Ryzyko (sekcja 5/§5): 3 testy pochodzenia, potem agregaty. Join
        # per-pozycja MUSI nastąpić TERAZ (rachunek surowy z `open_positions`,
        # ten sam indeks co `rows` — pętla wyżej dodaje dokładnie jeden wiersz
        # na jedną pozycję wejściową, bez pomijania), PRZED konsolidacją:
        # `consolidate_positions` tworzy NOWE dicty (kopie), więc klucz "risk"
        # dopisany później do `rows` nie trafiłby do wierszy skonsolidowanych. ---
        provenance = _evaluate_provenance(cur, d_risk)
        risk_rows: list[dict[str, Any]] = []
        risk_by_key: dict[tuple, dict[str, Any]] = {}
        sum_open_risk_pct = None
        level1_breach_count = None
        level1_incomplete_count = None
        price_cov_n = price_cov_N = None
        risk_stale_n = risk_stale_N = None
        theme_budgets: dict[str, Decimal] | None = None
        if provenance.passed:
            risk_rows = _fetch_risk_rows(cur, d_risk)
            for rr in risk_rows:
                risk_by_key[(rr["rachunek"], rr["instrument_id"], rr["settlement_currency"])] = rr
            sum_open_risk_pct = model.sum_open_risk_pct(risk_rows, is_risk_budget_eligible)
            level1_breach_count = model.count_level1_name_breaches(risk_rows, is_risk_budget_eligible)
            level1_incomplete_count = model.count_level1_incomplete_names(risk_rows, is_risk_budget_eligible)
            price_cov_n, price_cov_N = model.price_coverage(risk_rows, is_risk_budget_eligible)
            risk_stale_n, risk_stale_N = model.count_stale_risk_rows(risk_rows, is_risk_budget_eligible)
            theme_budgets = model.group_risk_pct_by_theme(risk_rows, is_risk_budget_eligible)

        for p, r in zip(open_positions, rows):
            raw_rachunek = p["rachunek"]  # SUROWY (z numerem) — WYŁĄCZNIE klucz joina w pamięci, nigdy do wyjścia
            risk_row = risk_by_key.get((raw_rachunek, r["instrument_id"], r["settlement_currency"])) if provenance.passed else None
            if not provenance.passed:
                r["risk"] = {"brak": "sekcja ryzyka: " + "; ".join(provenance.failed_tests)}
            elif risk_row is None:
                r["risk"] = {"brak": "brak wiersza na D ryzyka"}
            else:
                r["risk"] = {
                    "risk_state": risk_row["risk_state"],
                    "stop_effective": risk_row["stop_effective"],
                    "stop_source": risk_row["stop_source"],
                    "quote_currency": risk_row["quote_currency"],
                    "risk_pct_satellite_capital": risk_row["risk_pct_satellite_capital"],
                    "level1_breach": risk_row["level1_breach"],
                    "regime": risk_row["regime"],
                    "warning": risk_row["warning"],
                    "note": risk_row["note"],
                    "price_is_stale": risk_row["price_is_stale"],
                    "price_date_used": risk_row["price_date_used"],
                    "brak": None,
                }

        buckets: dict[str, list[dict[str, Any]]] = {"SAT": [], "CORE": [], "FUT": [], "INNE": []}
        for r in rows:
            cls = model.classify_position(r["rachunek"], r["instrument_type"], r["is_core"])
            buckets[cls].append(r)

        consolidated = {name: model.consolidate_positions(items) for name, items in buckets.items()}

        # Pozycja scalona z mieszaną walutą rozliczenia: "risk" po scaleniu
        # niósłby TYLKO pierwszy komponent (dict(group[0]) w model.py nie zna
        # semantyki klucza "risk") — jawnie nadpisujemy notatką odsyłającą do
        # `components` (każdy komponent zachowuje WŁASNY wiersz "risk").
        for c in consolidated.values():
            for r in c.positions:
                if r.get("consolidated") and r.get("mixed_currency"):
                    r["risk"] = {"brak": "pozycja scalona z mieszaną walutą rozliczenia — ryzyko per komponent (components)"}

        # --- BW/CORE (kontrakt §2/§3): suma "wszystko-albo-nic" wg T27 —
        # jeśli choć jedna pozycja zbioru ma cenę `incomplete`, cała suma jest
        # `brak pomiaru` z licznikiem n/N (nigdy suma częściowa, pozycja
        # NIGDY nie wypada z mianownika N). ---
        sat_n, sat_N = model.count_complete(consolidated["SAT"].positions, "value_pln")
        core_n, core_N = model.count_complete(consolidated["CORE"].positions, "value_pln")
        bw = model.compute_sum_if_complete(consolidated["SAT"].positions, "value_pln")
        core_value = model.compute_sum_if_complete(consolidated["CORE"].positions, "value_pln")
        calosc = (bw + core_value) if (bw is not None and core_value is not None) else None
        sat_share = (bw / calosc) if calosc else None

        weights = model.compute_weights(consolidated["SAT"].positions, bw)
        for r, w in zip(consolidated["SAT"].positions, weights):
            r["weight"] = w
        for name in ("CORE", "FUT", "INNE"):
            for r in consolidated[name].positions:
                r["weight"] = None  # poza mianownikiem wag BW (kontrakt §2/§7)

        # --- KS = BW + kontraktowy_account_value(...) po check_kontraktowy_coverage
        # (kontrakt §2, import i wywołanie wprost, bez zmian w risk.py). BW
        # `brak pomiaru` (SAT niepełne wg T27) -> KS też `brak pomiaru`
        # (kontrakt §2, wiersz KS: "wyjątek z funkcji KONTRAKTOWY -> KS =
        # brak pomiaru", tu analogicznie dla wejścia BW). ---
        ks: Decimal | None
        ks_error: str | None
        kontraktowy_value: Decimal | None
        if bw is None:
            ks = None
            kontraktowy_value = None
            ks_error = f"brak pomiaru: BW niekompletne wg T27 ({sat_n}/{sat_N} pozycji SAT z cena)"
        else:
            try:
                kontraktowy_rows = read_kontraktowy_rows(cur, d)
                has_open_futures = len(buckets["FUT"]) > 0
                max_kontraktowy_date = max((r["transaction_date"] for r in kontraktowy_rows), default=None)
                check_kontraktowy_coverage(len(kontraktowy_rows), max_kontraktowy_date, has_open_futures, d)
                kontraktowy_value = kontraktowy_account_value(kontraktowy_rows, d)
                ks = bw + kontraktowy_value
                ks_error = None
            except (RuntimeError, ValueError) as exc:
                ks = None
                kontraktowy_value = None
                ks_error = str(exc)

        # --- Kontrakty (sekcja 6/§7): nominał "wszystko-albo-nic" per pozycja
        # (bazowa cena `incomplete` -> nominał `brak pomiaru`, licznik n/N),
        # ekspozycja `brak pomiaru` gdy BW `brak pomiaru` (kontrakt §7). ---
        fut_rows = consolidated["FUT"].positions
        for r in fut_rows:
            r["nominal_pln"] = model.compute_futures_nominal_pln(
                r["qty"], r["multiplier"], r["base_close_raw"], r["base_fx_rate"]
            )
            r["exposure_pct"] = (r["nominal_pln"] / bw) if (r["nominal_pln"] is not None and bw) else None
        fut_n, fut_N = model.count_complete(fut_rows, "nominal_pln")
        net_nominal = sum((r["nominal_pln"] for r in fut_rows if r["nominal_pln"] is not None), Decimal(0))
        net_exposure_pct = (net_nominal / bw) if bw else None

        # --- Świeżość (kontrakt §8): pokrycie ceny wg T27 (WSZYSTKIE pozycje,
        # przed konsolidacją — census jak w kontrakcie §1), najstarsza
        # price_date_used. ---
        t27_ok = sum(1 for r in rows if r["price_status"] == "ok")
        t27_stale = sum(1 for r in rows if r["price_status"] == "stale")
        t27_incomplete = sum(1 for r in rows if r["price_status"] == "incomplete")
        price_dates_used = [r["price_date_used"] for r in rows if r["price_date_used"] is not None]
        oldest_price_date_used = min(price_dates_used) if price_dates_used else None
        sat_prices_ages = [r["price_age_days"] for r in consolidated["SAT"].positions if r["price_age_days"] is not None]
        oldest_price_age = max(sat_prices_ages) if sat_prices_ages else None
        fx_dates = [r["fx_rate_date"] for r in rows if r["fx_rate_date"] is not None]
        fx_date_max = max(fx_dates) if fx_dates else None
        cur.execute("SELECT max(fetched_at) FROM source_runs")
        (max_source_run_fetched_at,) = cur.fetchone()
        cur.execute("SELECT max(id) FROM source_runs")
        (max_source_run_id,) = cur.fetchone()

        sessions_since_d = model.count_weekday_sessions_between(d, date.today())
        if sessions_since_d > 5:
            raise DashboardStop(3, f"STOP: dane starsze niz 5 sesji (sesje od D: {sessions_since_d})")

        # --- Stopka jakości (kontrakt §9) ---
        incomplete_by_reason: dict[str, int] = {}
        for r in rows:
            if r["price_status"] == "incomplete" and r["incomplete_reason"]:
                incomplete_by_reason[r["incomplete_reason"]] = incomplete_by_reason.get(r["incomplete_reason"], 0) + 1
        # "Brak FX" (kontrakt §9) = brak kursu KRZYŻOWEGO waluty ROZLICZENIA
        # (wynik (a)) — odrębne od `fx_rate_missing` w T27 (to dotyczy waluty
        # NOTOWANIA, już policzone w `incomplete_by_reason`).
        brak_fx_rozliczenia = sum(
            1 for r in rows if r["result_a"]["brak_opis"] == "brak kursu krzyżowego NBP A ≤ D"
        )
        brak_isin = sum(1 for r in rows if not r["isin"])
        rynek_nieustalony = sum(1 for r in rows if r["market"] == "nieustalony")
        multiplier_missing = sum(1 for r in fut_rows if r["multiplier"] is None)
        theme_null = sum(1 for r in rows if r["instrument_type"] in ("equity", "etf") and not r["theme"])
        brak_wiersza_ryzyka = sum(1 for r in rows if r["risk"].get("brak") == "brak wiersza na D ryzyka")

        scalenia_ok = sum(c.merge_count for c in consolidated.values())
        scalenia_niejednoznaczne = sum(c.ambiguous_count for c in consolidated.values())
        scalenia_mieszana_waluta = sum(c.mixed_currency_merge_count for c in consolidated.values())

        payload: dict[str, Any] = {
            "meta": {
                "d_composition": d,
                "d_risk": d_risk,
                "generated_at": datetime.now().astimezone(),
                "pin": {
                    "max_source_run_id": max_source_run_id,
                    "risk_computed_at": risk_rows[0]["computed_at"] if risk_rows else None,
                },
                "nbp": {"fixing_id": "nbp_a", "rate_date_max": fx_date_max},
                "table_number_note": "brak pomiaru: nie zapisywany przy imporcie FX (backlog)",
                "freshness": {
                    "oldest_sat_price_age_days": oldest_price_age,
                    "oldest_price_date_used": oldest_price_date_used,
                    "fx_date_max": fx_date_max,
                    "max_source_run_fetched_at": max_source_run_fetched_at,
                    "sessions_since_d": sessions_since_d,
                    "t27_coverage": {"ok": t27_ok, "stale": t27_stale, "incomplete": t27_incomplete, "total": len(rows)},
                },
                "counts": {
                    name: {"before": c.count_before, "after": c.count_after} for name, c in consolidated.items()
                },
                "bw": bw,
                "bw_coverage": {"n": sat_n, "N": sat_N},
                "core_value_coverage": {"n": core_n, "N": core_N},
                "ks": ks,
                "ks_error": ks_error,
                "calosc": calosc,
                "sat_share_of_calosc": sat_share,
            },
            "positions": {name: c.positions for name, c in consolidated.items()},
            "risk": {
                "provenance": asdict(provenance),
                "sum_open_risk_pct": sum_open_risk_pct,
                "level1_breach_count": level1_breach_count,
                "level1_incomplete_count": level1_incomplete_count,
                "price_coverage": {"n": price_cov_n, "N": price_cov_N} if provenance.passed else None,
                "stale_coverage": {"n": risk_stale_n, "N": risk_stale_N} if provenance.passed else None,
                "theme_budgets": theme_budgets,
            },
            "contracts": {
                "rows": fut_rows,
                "net_exposure_pct": net_exposure_pct,
                "nominal_coverage": {"n": fut_n, "N": fut_N},
                "account_value_pln": kontraktowy_value if ks_error is None else None,
                "account_value_error": ks_error,
                "core_vs_satellite": {"bw": bw, "core_value": core_value, "calosc": calosc, "sat_share": sat_share},
            },
            "quality_footer": {
                "brak_fx_rozliczenia": brak_fx_rozliczenia,
                # poza licznikami luk (renderowane osobno jako "nie dotyczy")
                "nie_dotyczy_result_a_fut": sum(
                    1 for r in fut_rows if r["result_a"]["brak_opis"] == RESULT_A_FUT_NIE_DOTYCZY
                ),
                "t27_incomplete_by_reason": incomplete_by_reason,
                "t27_stale_count": t27_stale,
                "brak_isin": brak_isin,
                "scalenia_wykonane": scalenia_ok,
                "scalenia_niejednoznaczne": scalenia_niejednoznaczne,
                "scalenia_mieszana_waluta": scalenia_mieszana_waluta,
                "rynek_nieustalony": rynek_nieustalony,
                "brak_wiersza_ryzyka": brak_wiersza_ryzyka,
                "multiplier_missing": multiplier_missing,
                "theme_null": theme_null,
                "risk_price_stale": {"n": risk_stale_n, "N": risk_stale_N} if provenance.passed else None,
                # = wiersze tabeli pozycji (po konsolidacji, wszystkie zbiory), nie wiersze surowe
                "value_quality_trend_brak_count": sum(len(v.positions) for v in consolidated.values()),
                "conviction_nieprzypisana_count": sum(len(v.positions) for v in consolidated.values()),
                "provenance_tests": [asdict(t) for t in provenance.tests],
                "broker_reconciliation_note": "czeka: owner (B-16)",
                "attrs_without_history_note": "atrybuty instrumentów (np. theme) są BIEŻĄCE, bez historii (B-20)",
            },
            "static_notes": {
                "wynik_b": "wymaga lotów FIFO z datą i ceną wejścia (fifo.positions_as_of zwraca tylko agregat)",
                "broker_pl": "migawka brokera: czeka na ownera (B-16)",
                "value": "VALUE: §16.1 (archetyp + peer/reverse DCF) — silnik stanów niezbudowany",
                "quality": "QUALITY: §16.2 — silnik stanów niezbudowany",
                "trend": "TREND: §16.3 — silnik stanów niezbudowany",
                "conviction": "klasa konwikcji §20 nie jest jeszcze przypisywana w bazie",
            },
        }

        counts_for_stdout = {
            "before": sum(c.count_before for c in consolidated.values()),
            "after": sum(c.count_after for c in consolidated.values()),
            "sat": consolidated["SAT"].count_after,
            "core": consolidated["CORE"].count_after,
            "fut": consolidated["FUT"].count_after,
            "provenance_pass": provenance.passed,
            "provenance_overall": provenance.overall_status,
            "provenance_tests": [(t.name, t.status) for t in provenance.tests],
            "t27_ok": t27_ok,
            "t27_stale": t27_stale,
            "t27_incomplete": t27_incomplete,
        }
        return payload, counts_for_stdout


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Nie umiem zserializowac typu {type(obj)!r} do JSON dashboardu")


def render_template(payload: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if JSON_PLACEHOLDER not in template:
        raise RuntimeError(f"template.html nie zawiera punktu wstrzyknięcia {JSON_PLACEHOLDER!r}")
    json_str = json.dumps(payload, default=_json_default, ensure_ascii=False)
    json_str_safe = json_str.replace("</", "<\\/")
    # Pin w statycznym HTML (nie tylko w JS) — D6/D7 czytają go bez uruchamiania skryptu.
    pin = payload["meta"]["pin"]
    risk_ca = pin["risk_computed_at"]
    pin_text = (
        f"source_runs.id<={pin['max_source_run_id']}; "
        f"risk_daily.computed_at={_json_default(risk_ca) if risk_ca is not None else 'brak'}"
    )
    empty_meta = '<meta name="mannaz-pin" content="">'
    if empty_meta not in template:
        raise RuntimeError("template.html nie zawiera pustego meta mannaz-pin")
    template = template.replace(empty_meta, f'<meta name="mannaz-pin" content="{html.escape(pin_text)}">')
    return template.replace(JSON_PLACEHOLDER, json_str_safe)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generator dashboardu portfela satelity (kontrakt: tmp/dashboard/kontrakt-danych.md)"
    )
    p.add_argument("--date", required=True, help="D składu (YYYY-MM-DD), nigdy domyślny")
    p.add_argument("--risk-date", required=True, help="D ryzyka (YYYY-MM-DD), nigdy domyślny")
    p.add_argument("--out", required=True, help="Ścieżka pliku wyjściowego .html")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    d = date.fromisoformat(args.date)
    d_risk = date.fromisoformat(args.risk_date)
    out_path = Path(args.out)

    conn = get_connection()
    conn.read_only = True
    try:
        payload, counts = build_dashboard_payload(conn, d, d_risk)
    finally:
        conn.close()

    html_out = render_template(payload)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Bajty zapisywane wprost (bez translacji \n -> \r\n na Windows), żeby
    # wypisany sha256 był sha256 pliku na dysku.
    data = html_out.encode("utf-8")
    out_path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    print(f"D skladu: {d.isoformat()}")
    print(f"D ryzyka: {d_risk.isoformat()}")
    print(f"Pozycje przed konsolidacja (wszystkie zbiory): {counts['before']}")
    print(f"Pozycje po konsolidacji (wszystkie zbiory): {counts['after']}")
    print(f"SAT po konsolidacji: {counts['sat']}  CORE: {counts['core']}  FUT: {counts['fut']}")
    print(
        "T27 pokrycie ceny (wszystkie pozycje, przed konsolidacja): "
        f"ok={counts['t27_ok']} stale={counts['t27_stale']} incomplete={counts['t27_incomplete']}"
    )
    print(f"Pochodzenie ryzyka (zbiorczo): {counts['provenance_overall']}")
    for name, status in counts["provenance_tests"]:
        print(f"  {name}: {status}")
    print(f"Plik zapisany: {out_path}")
    print(f"sha256: {sha}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DashboardStop as exc:
        print(exc.message)
        sys.exit(exc.code)

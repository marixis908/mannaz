"""Generator lokalnego dashboardu portfela satelity (brief CC-D, krok D4).

Czyta WYŁĄCZNIE z bazy Postgres, w trybie read-only, bez sieci. Renderuje
JEDEN samodzielny plik HTML (CSS+JS inline, dane osadzone jako JSON).

Kontrakt danych — AUTORYTATYWNY, rozstrzyga wątpliwości:
    tmp/dashboard/kontrakt-danych.md (poza gitem, katalog roboczy Mannaz)

Użycie (z katalogu worktree, interpreter z .venv — globalny python nie ma
psycopg):
    C:\\Users\\MariuszBrysik\\projects\\Mannaz\\.venv\\Scripts\\python.exe ^
        scripts\\dashboard\\generate.py --date 2026-09-25 --risk-date 2026-09-24 ^
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
    _kontraktowy_rows,  # kontrakt §2: import i wywołanie wprost, bez zmian w kodzie
    check_kontraktowy_coverage,
    is_risk_budget_eligible,
    kontraktowy_account_value,
)

TEMPLATE_PATH = _SCRIPT_DIR / "template.html"
JSON_PLACEHOLDER = "__DASHBOARD_DATA_JSON__"


class DashboardStop(Exception):
    """STOP kontrolowany (kontrakt §3/§8) — kod wyjścia + komunikat BEZ
    żadnej kwoty/wagi/ceny/tickera."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Dostęp do bazy — same proste SELECT-y, read-only. Żadnej logiki domenowej
# tutaj (to zadanie model.py) poza złożeniem surowych wierszy.
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


def _fetch_base_instrument(cur, base_symbol: str | None) -> dict[str, Any] | None:
    if not base_symbol:
        return None
    cur.execute("SELECT id, currency, yahoo_symbol FROM instruments WHERE yahoo_symbol = %s", (base_symbol,))
    row = cur.fetchone()
    if row is None:
        return None
    return {"id": row[0], "currency": row[1], "yahoo_symbol": row[2]}


def _fetch_last_price(cur, instrument_id: int, d: date) -> tuple[Decimal | None, date | None]:
    cur.execute(
        """
        SELECT close_raw, price_date FROM prices_daily
        WHERE instrument_id = %s AND price_date <= %s AND close_raw IS NOT NULL
        ORDER BY price_date DESC LIMIT 1
        """,
        (instrument_id, d),
    )
    row = cur.fetchone()
    return (row[0], row[1]) if row else (None, None)


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
    kilka wspólnych walut/instrumentów bazowych, nie ma sensu pytać bazy
    wielokrotnie o to samo (instrument_id, D) / (waluta, D)."""

    def __init__(self, cur, d: date):
        self.cur = cur
        self.d = d
        self.price: dict[int, tuple[Decimal | None, date | None]] = {}
        self.fx: dict[str, tuple[Decimal | None, date | None]] = {}
        self.fx_supported: dict[str, bool] = {}
        self.base_instrument: dict[str, dict[str, Any] | None] = {}

    def price_for(self, instrument_id: int) -> tuple[Decimal | None, date | None]:
        if instrument_id not in self.price:
            self.price[instrument_id] = _fetch_last_price(self.cur, instrument_id, self.d)
        return self.price[instrument_id]

    def fx_for(self, currency: str) -> tuple[Decimal | None, date | None]:
        if currency not in self.fx:
            self.fx[currency] = _fetch_last_fx(self.cur, currency, self.d)
        return self.fx[currency]

    def fx_supported_for(self, currency: str) -> bool:
        if currency not in self.fx_supported:
            self.fx_supported[currency] = _currency_has_any_fx_data(self.cur, currency)
        return self.fx_supported[currency]

    def base_instrument_for(self, base_symbol: str | None) -> dict[str, Any] | None:
        if base_symbol not in self.base_instrument:
            self.base_instrument[base_symbol] = _fetch_base_instrument(self.cur, base_symbol)
        return self.base_instrument[base_symbol]


def _resolve_fx(caches: _Caches, currency: str) -> tuple[Decimal | None, date | None]:
    """Zwraca (współczynnik_FX_albo_None, data_kursu_albo_None). Rzuca
    `model.UnhandledCurrencyError` (STOP) dla waluty strukturalnie
    nieobsługiwanej — łapane przez wywołującego najwyższego poziomu."""
    rate, rate_date = caches.fx_for(currency)
    supported = caches.fx_supported_for(currency)
    factor = model.resolve_fx_factor(currency, rate, supported)
    return factor, rate_date


def _build_raw_position(cur, caches: _Caches, p: dict[str, Any], instruments: dict[int, dict[str, Any]], d: date) -> dict[str, Any]:
    instr = instruments[p["instrument_id"]]
    rachunek_label = model.account_label(p["rachunek"])
    instrument_type = instr["instrument_type"]
    is_core = bool(instr["is_core"])
    quote_currency = instr["currency"]
    settlement_currency = p["currency"]
    is_future = instrument_type == "future"

    # Cena: dla FUT instrument BAZOWY (kontrakt §3); dla reszty sam instrument.
    price_instrument_id = p["instrument_id"]
    price_quote_currency = quote_currency
    base_instr = None
    if is_future and instr["base_symbol"]:
        base_instr = caches.base_instrument_for(instr["base_symbol"])
        if base_instr is not None:
            price_instrument_id = base_instr["id"]
            price_quote_currency = base_instr["currency"]

    close_raw, price_date = caches.price_for(price_instrument_id)
    fx_factor, fx_rate_date = _resolve_fx(caches, price_quote_currency)  # może STOPować (UnhandledCurrencyError)

    value_pln = None
    value_pln_brak: str | None = None
    result_a: model.ResultA
    if is_future:
        # Decyzja (pytanie otwarte, patrz raport końcowy): "wartość PLN"
        # generyczna (§3) nie jest liczona dla FUT — wielkość analogiczna to
        # "nominał" (§7), z mnożnikiem, osobna sekcja Kontrakty. Podobnie
        # "wynik (a)" nie jest liczony dla KONTRAKTOWY: `residual_cost` z
        # `positions_as_of` dla futures pochodzi z `transactions.amount`,
        # które dla wierszy kupno/sprzedaz KONTRAKTOWY nie jest ceną*ilość
        # (patrz `risk.kontraktowy_account_value` — tam `amount` niesie
        # notional zawierający już mnożnik, nie samą cenę) — zastosowanie
        # wprost wzoru §3 wymagałoby decyzji metodologicznej, której nie
        # podejmujemy tu samodzielnie.
        value_pln_brak = "nie dotyczy FUT — patrz nominał w sekcji Kontrakty (§7)"
        result_a = model.ResultA(
            None, None, False,
            "niezdefiniowane dla KONTRAKTOWY — residual_cost FIFO miesza jednostki notional/mnożnik "
            "(patrz risk.kontraktowy_account_value); wymaga decyzji metodologicznej — pytanie otwarte",
        )
    else:
        if close_raw is None:
            value_pln_brak = "brak ceny \u2264 D"
        elif fx_factor is None:
            value_pln_brak = "brak kursu FX \u2264 D"
        else:
            value_pln = p["qty"] * close_raw * fx_factor

        fx_settlement_factor: Decimal | None
        try:
            fx_settlement_factor, _ = _resolve_fx(caches, settlement_currency)
        except model.UnhandledCurrencyError:
            fx_settlement_factor = None

        result_a = model.compute_result_a(
            qty=p["qty"],
            close_raw=close_raw,
            residual_cost=p["residual_cost"],
            quote_currency=quote_currency,
            settlement_currency=settlement_currency,
            fx_quote=fx_factor,
            fx_settlement=fx_settlement_factor,
        )

    market = model.market_from_yahoo_symbol(instr["yahoo_symbol"])
    price_age_days = (d - price_date).days if price_date else None

    return {
        "instrument_id": p["instrument_id"],
        "rachunek": rachunek_label,
        "broker_ticker": instr["broker_ticker"],
        "name": instr["name"] or instr["broker_ticker"],
        "isin": instr["isin"],
        "yahoo_symbol": instr["yahoo_symbol"],
        "quote_currency": quote_currency,
        "settlement_currency": settlement_currency,
        "instrument_type": instrument_type,
        "is_core": is_core,
        "theme": instr["theme"],
        "market": market,
        "qty": p["qty"],
        "residual_cost": p["residual_cost"],
        "first_entry_date": p["first_entry_date"],
        "last_entry_date": p["last_entry_date"],
        "close_raw": close_raw,
        "price_date": price_date,
        "price_age_days": price_age_days,
        "fx_rate": fx_factor,
        "fx_rate_date": fx_rate_date,
        "value_pln": value_pln,
        "value_pln_brak": value_pln_brak,
        "result_a": asdict(result_a),
        "multiplier": instr["multiplier"],
        "base_symbol": instr["base_symbol"],
        "base_close_raw": close_raw if is_future else None,
        "base_fx_rate": fx_factor if is_future else None,
        "base_price_date": price_date if is_future else None,
    }


# ---------------------------------------------------------------------------
# Ryzyko — 3 testy pochodzenia (kontrakt §5) + agregaty, jeśli PASS
# ---------------------------------------------------------------------------


def _evaluate_provenance(cur, d_risk: date) -> model.ProvenanceResult:
    cur.execute("SELECT count(DISTINCT computed_at) FROM risk_daily WHERE risk_date = %s", (d_risk,))
    (distinct_count,) = cur.fetchone()
    test1_ok = model.evaluate_test1_single_run(distinct_count)

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
    test3_ok = model.evaluate_test3_closed_positions_provenance(closed_positions_provenance)

    return model.evaluate_provenance(test1_ok, test2_ok, test3_ok)


def _fetch_risk_rows(cur, d_risk: date) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT r.rachunek, r.instrument_id, r.settlement_currency, r.quote_currency,
               r.risk_state, r.stop_effective, r.stop_source, r.risk_pct_satellite_capital,
               r.level1_breach, r.multiplier_missing, r.close_d, r.regime, r.warning,
               r.risk_pln, r.note, r.computed_at,
               i.instrument_type, i.is_core, i.theme
        FROM risk_daily r
        JOIN instruments i ON i.id = r.instrument_id
        WHERE r.risk_date = %s
        """,
        (d_risk,),
    )
    cols = (
        "rachunek", "instrument_id", "settlement_currency", "quote_currency",
        "risk_state", "stop_effective", "stop_source", "risk_pct_satellite_capital",
        "level1_breach", "multiplier_missing", "close_d", "regime", "warning",
        "risk_pln", "note", "computed_at",
        "instrument_type", "is_core", "theme",
    )
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Orkiestracja
# ---------------------------------------------------------------------------


def build_dashboard_payload(conn, d: date, d_risk: date) -> tuple[dict[str, Any], dict[str, Any]]:
    with conn.cursor() as cur:
        raw_positions = positions_as_of(cur, d)
        instrument_ids = sorted({p["instrument_id"] for p in raw_positions})
        instruments = _fetch_instruments(cur, instrument_ids)
        caches = _Caches(cur, d)

        rows: list[dict[str, Any]] = []
        for p in raw_positions:
            try:
                row = _build_raw_position(cur, caches, p, instruments, d)
            except model.UnhandledCurrencyError as exc:
                raise DashboardStop(2, str(exc)) from exc
            rows.append(row)

        # --- Ryzyko (sekcja 5/§5): 3 testy pochodzenia, potem agregaty. Join
        # per-pozycja MUSI nastąpić TERAZ (rachunek surowy z `raw_positions`,
        # ten sam indeks co `rows` — pętla wyżej dodaje dokładnie jeden wiersz
        # na jedną pozycję wejściową, bez pomijania), PRZED konsolidacją:
        # `consolidate_positions` tworzy NOWE dicty (kopie), więc klucz "risk"
        # dopisany później do `rows` nie trafiłby do wierszy skonsolidowanych. ---
        provenance = _evaluate_provenance(cur, d_risk)
        risk_rows: list[dict[str, Any]] = []
        risk_by_key: dict[tuple, dict[str, Any]] = {}
        sum_open_risk_pct = None
        level1_breach_count = None
        price_cov_n = price_cov_N = None
        theme_budgets: dict[str, Decimal] | None = None
        if provenance.passed:
            risk_rows = _fetch_risk_rows(cur, d_risk)
            for rr in risk_rows:
                risk_by_key[(rr["rachunek"], rr["instrument_id"], rr["settlement_currency"])] = rr
            sum_open_risk_pct = model.sum_open_risk_pct(risk_rows, is_risk_budget_eligible)
            level1_breach_count = model.count_level1_breaches(risk_rows)
            price_cov_n, price_cov_N = model.price_coverage(risk_rows, is_risk_budget_eligible)
            theme_budgets = model.group_risk_pct_by_theme(risk_rows, is_risk_budget_eligible)

        for p, r in zip(raw_positions, rows):
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

        bw = sum(
            (r["value_pln"] for r in consolidated["SAT"].positions if r["value_pln"] is not None),
            Decimal(0),
        )
        core_value = sum(
            (r["value_pln"] for r in consolidated["CORE"].positions if r["value_pln"] is not None),
            Decimal(0),
        )
        calosc = bw + core_value
        sat_share = (bw / calosc) if calosc else None

        weights = model.compute_weights(consolidated["SAT"].positions, bw)
        for r, w in zip(consolidated["SAT"].positions, weights):
            r["weight"] = w
        for name in ("CORE", "FUT", "INNE"):
            for r in consolidated[name].positions:
                r["weight"] = None  # poza mianownikiem wag BW (kontrakt §2/§7)

        # --- KS = BW + kontraktowy_account_value(...) po check_kontraktowy_coverage
        # (kontrakt §2, import i wywołanie wprost, bez zmian w risk.py) ---
        ks: Decimal | None
        ks_error: str | None
        try:
            kontraktowy_rows = _kontraktowy_rows(cur, d)
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

        # --- Kontrakty (sekcja 6/§7): nominał, ekspozycja ---
        fut_rows = consolidated["FUT"].positions
        for r in fut_rows:
            r["nominal_pln"] = model.compute_futures_nominal_pln(
                r["qty"], r["multiplier"], r["base_close_raw"], r["base_fx_rate"]
            )
            r["exposure_pct"] = (r["nominal_pln"] / bw) if (r["nominal_pln"] is not None and bw) else None
        net_nominal = sum((r["nominal_pln"] for r in fut_rows if r["nominal_pln"] is not None), Decimal(0))
        net_exposure_pct = (net_nominal / bw) if bw else None

        # --- Świeżość (kontrakt §8) ---
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
        brak_fx = sum(1 for r in rows if r["value_pln_brak"] == "brak kursu FX \u2264 D")
        brak_cena = sum(1 for r in rows if r["value_pln_brak"] == "brak ceny \u2264 D")
        wiek_gt_3 = sum(1 for r in rows if r["price_age_days"] is not None and r["price_age_days"] > 3)
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
                    "fx_date_max": fx_date_max,
                    "max_source_run_fetched_at": max_source_run_fetched_at,
                    "sessions_since_d": sessions_since_d,
                },
                "counts": {
                    name: {"before": c.count_before, "after": c.count_after} for name, c in consolidated.items()
                },
                "bw": bw,
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
                "price_coverage": {"n": price_cov_n, "N": price_cov_N} if provenance.passed else None,
                "theme_budgets": theme_budgets,
            },
            "contracts": {
                "rows": fut_rows,
                "net_exposure_pct": net_exposure_pct,
                "account_value_pln": kontraktowy_value if ks_error is None else None,
                "account_value_error": ks_error,
                "core_vs_satellite": {"bw": bw, "core_value": core_value, "calosc": calosc, "sat_share": sat_share},
            },
            "quality_footer": {
                "brak_fx": brak_fx,
                "brak_cena": brak_cena,
                "wiek_ceny_gt_3": wiek_gt_3,
                "brak_isin": brak_isin,
                "scalenia_wykonane": scalenia_ok,
                "scalenia_niejednoznaczne": scalenia_niejednoznaczne,
                "scalenia_mieszana_waluta": scalenia_mieszana_waluta,
                "rynek_nieustalony": rynek_nieustalony,
                "brak_wiersza_ryzyka": brak_wiersza_ryzyka,
                "multiplier_missing": multiplier_missing,
                "theme_null": theme_null,
                # = wiersze tabeli pozycji (po konsolidacji, wszystkie zbiory), nie wiersze surowe
                "value_quality_trend_brak_count": sum(len(v.positions) for v in consolidated.values()),
                "conviction_nieprzypisana_count": sum(len(v.positions) for v in consolidated.values()),
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
    print(f"Pochodzenie ryzyka: {'PASS' if counts['provenance_pass'] else 'FAIL'}")
    print(f"Plik zapisany: {out_path}")
    print(f"sha256: {sha}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except DashboardStop as exc:
        print(exc.message)
        sys.exit(exc.code)

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

Rewizja 2 (brief D2): `--date` i `--risk-date` opcjonalne. Domyslnie D =
`mannaz.risk.resolve_default_risk_date` (B-36), D ryzyka = max(risk_date) <= D.
Nowe sekcje (S1, S3-S9) renderuje statycznie `model.render_*_html` (kazda liczba
z atrybutem `data-conv`), dane czytane z bazy/modulow mannaz, bez liczenia metryk.
Kod wyjscia 0 takze przy braku daty D (HTML z `brak pomiaru`).

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

from mannaz import fifo as fifo_mod  # _FIFO_ROW_TYPES: row_type zmieniajace ilosc (Q1)
from mannaz.cycle import DEFAULT_MANUAL_DIR, load_buy_below_stop_events
from mannaz.db import get_connection
from mannaz.fifo import positions_as_of
from mannaz.provenance import code_sha
from mannaz.satellite import L_MAX, L_REL, load_account_values_dir, run_satellite_stage
from mannaz.risk import (
    LEVEL1_PCT,
    LEVEL2_PCT,
    LEVEL3_PCT,
    level1_by_name,
    resolve_default_risk_date,
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


# ---------------------------------------------------------------------------
# Rewizja 2 (brief D2) - odczyty dla sekcji S1, S3-S9. Zasada nadrzedna:
# dashboard CZYTA z tabel/widokow/funkcji mannaz.*, nie liczy metryk;
# generator robi wylacznie agregacje (sumy) i przewalutowanie przy agregacji.
# ---------------------------------------------------------------------------

BRAK_NO_D = "brak pomiaru: brak daty D wg B-36 (brak kompletnej daty w cenach i KONTRAKTOWY)"
BRAK_NO_ASSESSMENT = "brak pomiaru: brak oceny ryzyka <= D"
S5B_WINDOWS = ("od_S", "24m")


def _default_risk_date(cur, d: date) -> date | None:
    cur.execute("SELECT max(risk_date) FROM risk_daily WHERE risk_date <= %s", (d,))
    return cur.fetchone()[0]


def _prev_risk_date(cur, d_risk: date) -> date | None:
    """Okno zdarzen B-52: jak cycle.py (prev = max(risk_date) < data oceny)."""
    cur.execute("SELECT max(risk_date) FROM risk_daily WHERE risk_date < %s", (d_risk,))
    return cur.fetchone()[0]


def _positions_newer_than_risk(cur, d: date, d_risk: date | None, open_keys: set[tuple]) -> set[tuple]:
    """Q1: (rachunek, instrument_id) pozycji otwartych na D, ktore sa NOWSZE niz
    ocena ryzyka: nieobecne w `positions_as_of(d_risk)` ALBO z wierszem
    `transactions` w (d_risk, D] o typie zmieniajacym ilosc wg mannaz.fifo
    (`_FIFO_ROW_TYPES`). Splity (corporate_events) nie sa wierszami transactions."""
    if d_risk is None or not d_risk < d:
        return set()
    keys_risk = {(p["rachunek"], p["instrument_id"]) for p in positions_as_of(cur, d_risk)}
    cur.execute(
        "SELECT DISTINCT rachunek, instrument_id FROM transactions "
        "WHERE transaction_date > %s AND transaction_date <= %s AND row_type = ANY(%s)",
        (d_risk, d, list(fifo_mod._FIFO_ROW_TYPES)),
    )
    changed = {tuple(r) for r in cur.fetchall()}
    return {k for k in open_keys if k not in keys_risk or k in changed}


def _kontraktowy_status(cur, d: date, has_open_futures: bool) -> dict[str, Any]:
    """Kompletnosc rozliczenia KONTRAKTOWY (M77): to samo wywolanie co w KS."""
    try:
        krows = read_kontraktowy_rows(cur, d)
        max_date = max((r["transaction_date"] for r in krows), default=None)
        check_kontraktowy_coverage(len(krows), max_date, has_open_futures, d)
        return {"ok": True, "message": None}
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}


def _risk_unavailable_reason(d_risk: date | None, provenance: model.ProvenanceResult) -> str | None:
    if d_risk is None:
        return BRAK_NO_ASSESSMENT
    if not provenance.passed:
        text = f"brak pomiaru: sekcja ryzyka, pochodzenie {provenance.overall_status}"
        if provenance.failed_tests:
            text += ": " + "; ".join(provenance.failed_tests)
        if provenance.note:
            text += f" ({provenance.note})"
        return text
    return None


def _build_s3_s4(
    cur, d: date, d_risk: date | None, provenance: model.ProvenanceResult,
    risk_rows: list[dict[str, Any]], risk_by_key: dict[tuple, dict[str, Any]],
    open_positions: list[dict[str, Any]], instruments: dict[int, dict[str, Any]],
    newer_keys: set[tuple], sum_open: Decimal | None, themes: dict[str, Decimal] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    reason = _risk_unavailable_reason(d_risk, provenance)
    if reason:
        return {"status": "brak", "reason": reason}, {"status": "brak", "reason": reason}
    assert d_risk is not None
    labels = {rr["name_key"]: rr["name_label"] for rr in risk_rows if rr["name_key"] is not None and rr["name_label"]}
    items = [
        (rr["name_key"], is_risk_budget_eligible(rr["instrument_type"], rr["is_core"]), rr["broker_ticker"],
         rr["settlement_currency"], rr["risk_pct_satellite_capital"], rr["name_risk_pct"], rr["level1_breach"])
        for rr in risk_rows
    ]
    names = [
        {"label": n.label, "pct": n.pct, "breach": n.breach, "incomplete": n.incomplete, "n_members": len(n.members)}
        for n in level1_by_name(items, labels)
    ]
    prev = _prev_risk_date(cur, d_risk)
    events: list[Any] | None = None
    events_error: str | None = None
    try:
        events = load_buy_below_stop_events(cur, d_risk, prev)
    except Exception as exc:  # noqa: BLE001 - sekcja ma byc "brak pomiaru", nie wyjatek
        events_error = type(exc).__name__
        cur.connection.rollback()
    event_date_by_key: dict[tuple, date] = {}
    for ev in events or []:
        k = (ev.rachunek, ev.broker_ticker, ev.settlement_currency)
        if k not in event_date_by_key or ev.txn_date > event_date_by_key[k]:
            event_date_by_key[k] = ev.txn_date
    newer_reason = f"brak pomiaru: pozycja nowsza niz ocena ryzyka {d_risk.isoformat()}"
    pos_rows: list[dict[str, Any]] = []
    for p in open_positions:
        if not is_risk_budget_eligible(p["instrument_type"], p["is_core"]):
            continue
        instr = instruments.get(p["instrument_id"]) or {}
        name = instr.get("name") or p["broker_ticker"]
        label = f"{model.account_label(p['rachunek'])} ({p['settlement_currency']})"
        row: dict[str, Any] = {"name": name, "rachunek": label}
        rr = risk_by_key.get((p["rachunek"], p["instrument_id"], p["settlement_currency"]))
        if (p["rachunek"], p["instrument_id"]) in newer_keys:
            row.update(state="nowsza", reason=newer_reason)
        elif rr is None:
            row.update(state="brak_wiersza", reason=f"brak pomiaru: brak wiersza w ocenie ryzyka {d_risk.isoformat()}")
        else:
            row.update(
                state="ok", reason=None,
                risk_pct=rr["risk_pct_satellite_capital"], name_pct=rr["name_risk_pct"], breach=rr["level1_breach"],
                stop_effective=rr["stop_effective"], stop_source=rr["stop_source"], quote_currency=rr["quote_currency"],
                buy_below_stop_date=event_date_by_key.get((p["rachunek"], p["broker_ticker"], p["settlement_currency"])),
            )
        pos_rows.append(row)
    pos_rows.sort(key=lambda r: (str(r["name"]), r["rachunek"]))
    s3 = {
        "status": "ok", "d": d, "risk_date": d_risk, "names": names,
        "sum_open_risk_pct": sum_open, "themes": themes or {},
        "limits": {"l1": LEVEL1_PCT, "l2": LEVEL2_PCT, "l3": LEVEL3_PCT},
        "positions": pos_rows,
    }
    event_rows = None
    if events is not None:
        event_rows = [
            {
                "name": ev.broker_ticker, "rachunek": f"{model.account_label(ev.rachunek)} ({ev.settlement_currency})",
                "txn_date": ev.txn_date, "price_quote": ev.price_quote, "quote_currency": ev.quote_currency,
                "stop_before": ev.stop_before, "stop_before_date": ev.stop_before_date,
            }
            for ev in events
        ]
    s4 = {
        "status": "ok", "d": d, "risk_date": d_risk, "prev": prev, "positions": pos_rows,
        "events": event_rows, "events_error": events_error,
    }
    return s3, s4


def _registry_block(cur, table: str, date_col: str, d: date, select_expr: str, cols: tuple[str, ...]) -> dict[str, Any]:
    cur.execute(f"SELECT count(*) FROM {table}")
    total = cur.fetchone()[0]
    cur.execute(
        f"SELECT component, {select_expr} FROM {table} WHERE registry = 1 AND {date_col} <= %s GROUP BY component",
        (d,),
    )
    by_component = {row[0]: dict(zip(cols, row[1:])) for row in cur.fetchall()}
    return {"table_rows_total": total, "by_component": by_component}


def _fetch_registry1(cur, d: date) -> dict[str, Any]:
    """S5a: rejestr 1 z bazy (tabele M89, tylko dopisywane), stan na D."""
    try:
        viol = _registry_block(
            cur, "satellite_mandate_violations", "window_end", d,
            "count(*), min(window_start), max(window_end), string_agg(DISTINCT data_status, ' / ' ORDER BY data_status), "
            "max(first_seen_run_at)",
            ("n", "window_start_min", "window_end_max", "data_status", "first_seen_max"),
        )
        ev = _registry_block(
            cur, "satellite_mandate_events", "event_date", d,
            "count(*) FILTER (WHERE level = 'ostrzezenie'), count(*) FILTER (WHERE level = 'alert'), "
            "min(event_date), max(event_date), string_agg(DISTINCT data_status, ' / ' ORDER BY data_status), "
            "max(first_seen_run_at)",
            ("n_warn", "n_alert", "date_min", "date_max", "data_status", "first_seen_max"),
        )
        cur.execute(
            "SELECT component, window_start, window_end, max_dd, limit_value, data_status "
            "FROM satellite_mandate_violations WHERE registry = 1 AND window_end <= %s "
            "ORDER BY window_end DESC, component",
            (d,),
        )
        viol["windows"] = [
            dict(zip(("component", "window_start", "window_end", "max_dd", "limit_value", "data_status"), row))
            for row in cur.fetchall()
        ]
    except Exception as exc:  # noqa: BLE001
        cur.connection.rollback()
        return {"status": "brak", "reason": f"brak pomiaru: odczyt rejestru 1 nieudany ({type(exc).__name__})"}
    return {"status": "ok", "d": d, "limits": {"abs": L_MAX, "rel": L_REL}, "violations": viol, "events": ev}


def _build_s5b_s9(conn, d: date, account_values_dir: Path | str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """S5b + S9 (Q2 = A): modul satelity na to samo D co S2; write=False, out_dir=None."""
    try:
        snapshots = load_account_values_dir(account_values_dir if account_values_dir is not None else DEFAULT_MANUAL_DIR)
        report = run_satellite_stage(conn, d, out_dir=None, write=False, snapshots=snapshots).report
    except Exception as exc:  # noqa: BLE001 - tresc wyjatku moze niesc dane; tylko nazwa klasy
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        reason = f"brak pomiaru: modul satelity: {type(exc).__name__}"
        return {"status": "brak", "reason": reason}, {"status": "brak", "reason": reason}
    windows: dict[str, Any] = {}
    for wname in S5B_WINDOWS:
        w = report["windows"].get(wname)
        if w is None:
            continue
        windows[wname] = {
            "from": w["from"], "to": w["to"], "twr": w["twr"], "max_dd_abs": w["abs"]["max_dd"],
            "mandate": {"status": w["mandate"]["status"], "violations": list(w["mandate"]["violations"])},
            "bench": {
                k: {"delta_w_pln": b["delta_w_pln"], "infeasible": b["infeasible"], "max_dd_rel": b["max_dd_rel"]}
                for k, b in w["bench"].items()
            },
        }
    gate = report.get("gate")
    s5b = {
        "status": "ok", "d": d, "S": report.get("S"),
        "data_verified": bool(report["data_verified"]),
        "reasons": list(report.get("data_unverified_reasons") or []),
        "gate": None if gate is None else {"result": gate.get("result"), "ok": gate.get("ok")},
        "windows": windows,
        "alerts": {
            c: {"limit": a["limit"], "current_drawdown": a["current_drawdown"], "state": a["state"]}
            for c, a in (report.get("alerts") or {}).items()
        },
        "limits": {"abs": L_MAX, "rel": L_REL},
        "conventions_currency": report["conventions"]["currency"],
        "benchmarks": {k: f"{v['symbol']} ({v['source']})" for k, v in report["conventions"]["benchmarks"].items()},
    }
    att = report.get("attribution")
    if not att or not att.get("windows"):
        return s5b, {"status": "brak", "reason": "brak pomiaru: modul nie zwrocil atrybucji"}
    s9_windows: dict[str, Any] = {}
    for wname in S5B_WINDOWS:
        w = att["windows"].get(wname)
        if w is None:
            continue
        acc = w["by_account"]
        s9_windows[wname] = {
            "from": w["from"], "to": w["to"], "complete": bool(w["control"]["complete"]),
            "sum_lokalny": sum(r["lokalny"] for r in acc),
            "sum_fx": sum(r["fx"] for r in acc),
            "sum_total": sum(r["total"] for r in acc),
            "by_currency": [
                {"key": r["key"], "lokalny": r["lokalny"], "fx": r["fx"], "total": r["total"]}
                for r in w["by_currency"]
            ],
        }
    return s5b, {"status": "ok", "d": d, "windows": s9_windows, "conventions": att["conventions"]}


def _build_s7(cur, d: date, open_positions: list[dict[str, Any]], instruments: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """S7: archetype_assignments aktywny na D (valid_from <= D, valid_to NULL lub > D)."""
    ids = sorted({p["instrument_id"] for p in open_positions})
    active: dict[int, list[tuple]] = {}
    if ids:
        cur.execute(
            "SELECT instrument_id, archetype, archetype_key, key_version FROM archetype_assignments "
            "WHERE instrument_id = ANY(%s) AND valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)",
            (ids, d, d),
        )
        for iid, arch, key, ver in cur.fetchall():
            active.setdefault(iid, []).append((arch, key, ver))
    rows: list[dict[str, Any]] = []
    for iid in ids:
        instr = instruments.get(iid) or {}
        base = {"name": instr.get("name") or instr.get("broker_ticker") or str(iid), "theme": instr.get("theme")}
        found = active.get(iid, [])
        if not found:
            rows.append({**base, "state": "brak", "reason": "brak pomiaru: brak przypisania archetypu",
                         "archetype": None, "archetype_key": None, "key_version": None})
        elif len(found) > 1:
            rows.append({**base, "state": "brak", "reason": f"brak pomiaru: niejednoznaczne przypisanie archetypu ({len(found)} aktywnych wierszy)",
                         "archetype": None, "archetype_key": None, "key_version": None})
        else:
            rows.append({**base, "state": "ok", "reason": None, "archetype": found[0][0],
                         "archetype_key": found[0][1], "key_version": found[0][2]})
    rows.sort(key=lambda r: str(r["name"]))
    return {"status": "ok", "d": d, "rows": rows}


def build_no_date_payload(conn, d_source: str = "brak") -> tuple[dict[str, Any], dict[str, Any]]:
    """Brak D wg B-36 (i brak --date): HTML renderowany, kazda sekcja `brak pomiaru`."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), max(fetched_at), max(id) FROM source_runs")
        n_runs, max_fetched, max_id = cur.fetchone()
    satellites: dict[str, Any] = {
        "no_d": BRAK_NO_D,
        "s1": {
            "d": None, "d_source": d_source, "d_risk": None, "risk_source": None, "price_n": None, "price_N": None,
            "kontraktowy": None, "code_sha": code_sha(), "source_runs_n": n_runs, "source_runs_max_fetched_at": max_fetched,
        },
    }
    payload: dict[str, Any] = {
        "no_d": BRAK_NO_D,
        "meta": {
            "d_composition": None, "d_risk": None, "generated_at": datetime.now().astimezone(),
            "pin": {"max_source_run_id": max_id, "risk_computed_at": None},
        },
        "satellites": satellites,
    }
    return payload, {"no_d": True, "brak_sections": model.brak_sections(satellites)}


def build_dashboard_payload(
    conn,
    d: date,
    d_risk: date | None,
    d_source: str = "podane",
    risk_source: str = "podane",
    account_values_dir: Path | str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
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
        if d_risk is None:
            provenance = model.ProvenanceResult(
                passed=False, overall_status="BRAK OCENY", tests=[],
                failed_tests=["brak oceny ryzyka <= D"], note="brak oceny ryzyka <= D",
            )
        else:
            provenance = _evaluate_provenance(cur, d_risk)
        # Q1 (decyzja planisty 2026-10-19): sklad na D, ryzyko z oceny <= D; pozycja
        # nowsza niz ocena -> `brak pomiaru` (nie stary stop/ryzyko, nie 0).
        newer_keys = _positions_newer_than_risk(
            cur, d, d_risk, {(p["rachunek"], p["instrument_id"]) for p in open_positions}
        )
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
            elif (raw_rachunek, r["instrument_id"]) in newer_keys:
                r["risk"] = {"brak": f"brak pomiaru: pozycja nowsza niz ocena ryzyka {d_risk.isoformat()}"}
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
        nowsze_niz_ocena = sum(
            1 for r in rows if str(r["risk"].get("brak") or "").startswith("brak pomiaru: pozycja nowsza niz ocena")
        )

        scalenia_ok = sum(c.merge_count for c in consolidated.values())
        scalenia_niejednoznaczne = sum(c.ambiguous_count for c in consolidated.values())
        scalenia_mieszana_waluta = sum(c.mixed_currency_merge_count for c in consolidated.values())

        # --- Rewizja 2 (D2): S1, S3-S9 (czytane z bazy / modulow mannaz) ---
        cur.execute("SELECT count(*) FROM source_runs")
        (source_runs_n,) = cur.fetchone()
        s3, s4 = _build_s3_s4(
            cur, d, d_risk, provenance, risk_rows, risk_by_key, open_positions, instruments,
            newer_keys, sum_open_risk_pct, theme_budgets,
        )
        s5a = _fetch_registry1(cur, d)
        s7 = _build_s7(cur, d, open_positions, instruments)
        s5b, s9 = _build_s5b_s9(conn, d, account_values_dir)
        satellites: dict[str, Any] = {
            "no_d": None,
            "s1": {
                "d": d, "d_source": d_source, "d_risk": d_risk, "risk_source": risk_source if d_risk is not None else None,
                "price_n": t27_ok + t27_stale, "price_N": len(rows),
                "kontraktowy": _kontraktowy_status(cur, d, len(buckets["FUT"]) > 0),
                "code_sha": code_sha(), "source_runs_n": source_runs_n,
                "source_runs_max_fetched_at": max_source_run_fetched_at,
            },
            "s3": s3, "s4": s4, "s5a": s5a, "s5b": s5b, "s7": s7, "s9": s9,
        }

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
            "satellites": satellites,
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
                "pozycje_nowsze_niz_ocena": nowsze_niz_ocena,
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
                "wynik_b": "loty FIFO dostępne w `PositionResult.lots`; wynik B w dashboardzie nie wdrożony",
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
            "brak_sections": model.brak_sections(satellites),
            "s5b_status": s5b.get("status"),
            "s5b_data_verified": s5b.get("data_verified") if s5b.get("status") == "ok" else None,
            "newer_positions": nowsze_niz_ocena,
        }
        return payload, counts_for_stdout


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"Nie umiem zserializowac typu {type(obj)!r} do JSON dashboardu")


def _finite(obj: Any) -> Any:
    """NaN/Infinity nie sa poprawnym JSON (JSON.parse w przegladarce padnie) -> None."""
    if isinstance(obj, float):
        return obj if obj == obj and obj not in (float("inf"), float("-inf")) else None
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


S1_PLACEHOLDER = "<!--MANNAZ-S1-->"
SECTIONS_PLACEHOLDER = "<!--MANNAZ-SECTIONS-->"


def render_template(payload: dict[str, Any]) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if JSON_PLACEHOLDER not in template:
        raise RuntimeError(f"template.html nie zawiera punktu wstrzyknięcia {JSON_PLACEHOLDER!r}")
    for ph in (S1_PLACEHOLDER, SECTIONS_PLACEHOLDER):
        if ph not in template:
            raise RuntimeError(f"template.html nie zawiera punktu wstrzyknięcia {ph!r}")
    # Sekcje rew. 2 (statyczny HTML z data-conv) - PRZED wstrzyknieciem JSON.
    template = template.replace(S1_PLACEHOLDER, model.render_s1_html(payload["satellites"]))
    template = template.replace(SECTIONS_PLACEHOLDER, model.render_sections_html(payload))
    json_str = json.dumps(_finite(payload), default=_json_default, ensure_ascii=False)
    json_str_safe = json_str.replace("</", "<\\/").replace("<!--", "<" + chr(92) + "u0021--")
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
    p.add_argument("--date", default=None, help="D składu (YYYY-MM-DD); domyślnie mannaz.risk.resolve_default_risk_date (B-36)")
    p.add_argument("--risk-date", default=None, help="D ryzyka (YYYY-MM-DD); domyślnie max(risk_date) <= D")
    p.add_argument("--out", required=True, help="Ścieżka pliku wyjściowego .html")
    p.add_argument(
        "--account-values-dir", default=str(DEFAULT_MANUAL_DIR),
        help="Katalog kotwic M78 account_values_RRRR-MM-DD.csv (domyślnie mannaz.cycle.DEFAULT_MANUAL_DIR)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_path = Path(args.out)

    conn = get_connection()
    conn.read_only = True
    try:
        if args.date:
            d: date | None = date.fromisoformat(args.date)
            d_source = "podane"
        else:
            d = resolve_default_risk_date(conn)
            d_source = "domyslne B-36"
        if d is None:
            payload, counts = build_no_date_payload(conn)
            d_risk = None
        else:
            if args.risk_date:
                d_risk = date.fromisoformat(args.risk_date)
                risk_source = "podane"
            else:
                with conn.cursor() as cur:
                    d_risk = _default_risk_date(cur, d)
                risk_source = "domyslne: max(risk_date) <= D"
            payload, counts = build_dashboard_payload(
                conn, d, d_risk, d_source, risk_source, args.account_values_dir
            )
    finally:
        conn.close()

    html_out = render_template(payload)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Bajty zapisywane wprost (bez translacji \n -> \r\n na Windows), żeby
    # wypisany sha256 był sha256 pliku na dysku.
    data = html_out.encode("utf-8")
    out_path.write_bytes(data)
    sha = hashlib.sha256(data).hexdigest()

    print(f"D skladu: {d.isoformat() if d else 'brak (B-36)'} ({d_source})")
    print(f"D ryzyka: {d_risk.isoformat() if d_risk else 'brak'}")
    if counts.get("no_d"):
        print("Brak daty D wg B-36: wszystkie sekcje brak pomiaru")
        print(f"Sekcje brak pomiaru: {len(counts['brak_sections'])}")
        print(f"Plik zapisany: {out_path}")
        print(f"sha256: {sha}")
        return 0
    print(f"Pozycje przed konsolidacja (wszystkie zbiory): {counts['before']}")
    print(f"Pozycje po konsolidacji (wszystkie zbiory): {counts['after']}")
    print(f"SAT po konsolidacji: {counts['sat']}  CORE: {counts['core']}  FUT: {counts['fut']}")
    print(
        "T27 pokrycie ceny (wszystkie pozycje, przed konsolidacja): "
        f"ok={counts['t27_ok']} stale={counts['t27_stale']} incomplete={counts['t27_incomplete']}"
    )
    print(f"Pochodzenie ryzyka (zbiorczo): {counts['provenance_overall']}")
    print(f"Pozycje nowsze niz ocena ryzyka: {counts['newer_positions']}")
    print(f"S5b modul satelity: {counts['s5b_status']}; dane zweryfikowane: {counts['s5b_data_verified']}")
    print(f"Sekcje brak pomiaru (cale): {len(counts['brak_sections'])} ({', '.join(counts['brak_sections'])})")
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

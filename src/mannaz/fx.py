"""FX — NBP tabela A + kontrola niezależna Frankfurter — brief CC-P, P3.3.

Zakres walut (brief): USD, EUR, CAD, CHF, GBP, HKD, JPY, SEK, DKK,
2023-10-01 -> dziś, w chunkach <=93 dni (limit API NBP na zapytanie zakresowe).

Retry z backoffem: w P1 CHF dał `ReadTimeout` przy timeout=20s — tu timeout=60s,
3 próby, backoff liniowy (5s, 10s).

Kontrola niezależna: Frankfurter (`api.frankfurter.dev/v1`, bez klucza, bez
limitu — §9.3 dokumentu projektowego) na 20 (najnowszych wspólnych) sesjach per
waluta, mediana `|frankfurter/nbp - 1|` w %.

P-03 (dokument projektowy, §28): czy HKD jest w tabeli A czy B NBP — jeśli B,
kurs jest tygodniowy, nie dzienny. `is_currency_in_table_a` robi jedno
zapytanie punktowe do sprawdzenia (404 = tabela B / brak waluty w A)."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Any

import psycopg
import requests

from mannaz.prices import (
    NETWORK_ERRORS,
    _ensure_ingest_errors_table,
    _log_ingest_error,
    call_with_network_retry,
    describe_network_error,
)
from mannaz.provenance import code_sha, current_trigger

NBP_CURRENCIES: tuple[str, ...] = ("USD", "EUR", "CAD", "CHF", "GBP", "HKD", "JPY", "SEK", "DKK")
NBP_CHUNK_DAYS = 93  # limit NBP API na jedno zapytanie zakresowe
NBP_TIMEOUT_SECONDS = 60  # P1: CHF ReadTimeout przy 20s (empiria)
NBP_MAX_RETRIES = 3
NBP_BACKOFF_BASE_SECONDS = 5
DEFAULT_START = date(2023, 10, 1)

FRANKFURTER_BASE_URL = "https://api.frankfurter.dev/v1"
FRANKFURTER_CONTROL_SESSIONS = 20


# ---------------------------------------------------------------------------
# NBP tabela A
# ---------------------------------------------------------------------------


def _chunk_date_ranges(start: date, end: date, chunk_days: int = NBP_CHUNK_DAYS) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
        ranges.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return ranges


def is_currency_in_table_a(currency: str, probe_date: date | None = None) -> bool:
    """P-03: jedno zapytanie punktowe. 404 -> waluta poza tabelą A (albo B,
    albo w ogóle nienotowana przez NBP) -> False."""
    if probe_date is None:
        probe_date = date.today() - timedelta(days=1)
    url = (
        f"https://api.nbp.pl/api/exchangerates/rates/a/{currency.lower()}/"
        f"{probe_date.isoformat()}/?format=json"
    )
    resp = requests.get(url, timeout=NBP_TIMEOUT_SECONDS)
    return resp.status_code == 200


def fetch_nbp_table_a_chunk(currency: str, start: date, end: date) -> list[tuple[date, Decimal]]:
    """Jeden chunk (<=93 dni). Retry z backoffem liniowym (P1 empiria: CHF
    ReadTimeout przy timeout=20s)."""
    url = (
        f"https://api.nbp.pl/api/exchangerates/rates/a/{currency.lower()}/"
        f"{start.isoformat()}/{end.isoformat()}/?format=json"
    )
    last_exc: Exception | None = None
    for attempt in range(1, NBP_MAX_RETRIES + 1):
        try:
            resp = requests.get(url, timeout=NBP_TIMEOUT_SECONDS)
            if resp.status_code == 404:
                # brak notowań w całym zakresie (np. same święta, albo waluta
                # poza tabelą A dla tego okresu) — nie retry'ujemy 404.
                return []
            resp.raise_for_status()
            data = resp.json()
            out: list[tuple[date, Decimal]] = []
            for rate_row in data.get("rates", []):
                d = date.fromisoformat(rate_row["effectiveDate"])
                out.append((d, Decimal(str(rate_row["mid"]))))
            return out
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < NBP_MAX_RETRIES:
                time.sleep(NBP_BACKOFF_BASE_SECONDS * attempt)
    raise RuntimeError(
        f"NBP fetch failed after {NBP_MAX_RETRIES} attempts for {currency} {start}..{end}"
    ) from last_exc


def fetch_nbp_table_a(currency: str, start: date, end: date) -> list[tuple[date, Decimal]]:
    """Pełny zakres, podzielony na chunki <=93 dni (limit API NBP)."""
    out: list[tuple[date, Decimal]] = []
    for chunk_start, chunk_end in _chunk_date_ranges(start, end):
        out.extend(fetch_nbp_table_a_chunk(currency, chunk_start, chunk_end))
    return out


# ---------------------------------------------------------------------------
# Kontrola niezależna — Frankfurter
# ---------------------------------------------------------------------------


def fetch_frankfurter_rate(currency: str, on_date: date) -> Decimal | None:
    """PLN za 1 jednostkę `currency`, na konkretną datę (Frankfurter zwraca
    najbliższy wcześniejszy dzień roboczy jeśli `on_date` nie jest sesją)."""
    url = f"{FRANKFURTER_BASE_URL}/{on_date.isoformat()}?base={currency}&symbols=PLN"
    resp = requests.get(url, timeout=NBP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    data = resp.json()
    rate = data.get("rates", {}).get("PLN")
    if rate is None:
        return None
    return Decimal(str(rate))


@dataclass
class FxControlResult:
    currency: str
    n_sessions_compared: int
    median_abs_pct_diff: Decimal | None
    note: str = ""


def compute_fx_control(
    nbp_rates: list[tuple[date, Decimal]],
    frankfurter_rates: list[tuple[date, Decimal]],
    currency: str,
    n_sessions: int = FRANKFURTER_CONTROL_SESSIONS,
) -> FxControlResult:
    """Mediana `|frankfurter/nbp - 1|` w % na `n_sessions` najnowszych
    WSPÓLNYCH datach (brief P3.3: 20 sesji per waluta). Czysta funkcja —
    testowalna bez sieci (patrz tests/test_fx.py)."""
    nbp_by_date = dict(nbp_rates)
    ff_by_date = dict(frankfurter_rates)
    common_dates = sorted(set(nbp_by_date) & set(ff_by_date))
    if not common_dates:
        return FxControlResult(
            currency=currency,
            n_sessions_compared=0,
            median_abs_pct_diff=None,
            note="brak wspolnych dat do porownania",
        )
    sample = common_dates[-n_sessions:]
    diffs: list[Decimal] = []
    for d in sample:
        nbp_rate = nbp_by_date[d]
        ff_rate = ff_by_date[d]
        if nbp_rate == 0:
            continue
        diffs.append(abs(ff_rate / nbp_rate - Decimal(1)) * Decimal(100))
    if not diffs:
        return FxControlResult(
            currency=currency,
            n_sessions_compared=0,
            median_abs_pct_diff=None,
            note="wszystkie wspolne daty mialy nbp_rate == 0 (nieoczekiwane)",
        )
    return FxControlResult(
        currency=currency,
        n_sessions_compared=len(diffs),
        median_abs_pct_diff=Decimal(str(median(diffs))),
    )


# ---------------------------------------------------------------------------
# Orkiestracja / DB
# ---------------------------------------------------------------------------


@dataclass
class FxCurrencyResult:
    currency: str
    rows_fetched: int = 0
    rows_inserted: int = 0
    control: FxControlResult | None = None
    error: str = ""


@dataclass
class FxSummary:
    results: list[FxCurrencyResult] = field(default_factory=list)


def run_fx_fetch(
    conn: psycopg.Connection,
    currencies: tuple[str, ...] = NBP_CURRENCIES,
    start: date = DEFAULT_START,
    end: date | None = None,
    commit: bool = True,
    sleep: Callable[[float], None] = time.sleep,
) -> FxSummary:
    """NBP tabela A -> `fx_nbp` + kontrola Frankfurter (P3.3).

    B-49: wywołanie `fetch_frankfurter_rate` przy błędzie sieci
    (`NETWORK_ERRORS`) jest ponawiane raz po 5 s (`sleep` wstrzykiwalne). Druga
    porażka = wpis `ingest_errors` (source='frankfurter', instrument_id NULL,
    price_date = data próbki, `connection_error`); próbki Frankfurter dla tej
    waluty są przerwane, a CZĘŚCIOWA próbka odrzucona (mediana z kilku sesji
    byłaby wprowadzająca w błąd) — kontrola dostaje n=0 i notatkę; kursy NBP są
    zapisane, etap idzie do następnej waluty. Wyjątek spoza `NETWORK_ERRORS`
    przerywa etap. `commit=False` — do testów DB z rollbackiem."""
    if end is None:
        end = date.today()

    run_started_at = datetime.now(timezone.utc)
    summary = FxSummary()

    with conn.cursor() as cur:
        _ensure_ingest_errors_table(cur)
        for currency in currencies:
            result = FxCurrencyResult(currency=currency)
            try:
                nbp_rates = fetch_nbp_table_a(currency, start, end)
            except RuntimeError as exc:
                result.error = str(exc)
                summary.results.append(result)
                continue

            result.rows_fetched = len(nbp_rates)
            pair = f"{currency}/PLN"
            inserted = 0
            for rate_date, rate in nbp_rates:
                cur.execute(
                    """
                    INSERT INTO fx_nbp (pair, rate_date, fixing_id, rate, source)
                    VALUES (%s, %s, 'nbp_a', %s, 'nbp')
                    ON CONFLICT (pair, rate_date, fixing_id) DO UPDATE SET
                        rate = EXCLUDED.rate,
                        fetched_at = now()
                    """,
                    (pair, rate_date, rate),
                )
                inserted += 1
            result.rows_inserted = inserted

            sample_dates = [d for d, _ in nbp_rates[-FRANKFURTER_CONTROL_SESSIONS:]]
            frankfurter_rates: list[tuple[date, Decimal]] = []
            frankfurter_failed = False
            for d in sample_dates:
                try:
                    rate = call_with_network_retry(
                        lambda: fetch_frankfurter_rate(currency, d), sleep
                    )
                except NETWORK_ERRORS as exc:
                    _log_ingest_error(
                        cur,
                        source="frankfurter",
                        instrument_id=None,
                        price_date=d,
                        error_type="connection_error",
                        detail=f"waluta={currency} " + describe_network_error(exc),
                        run_started_at=run_started_at,
                    )
                    frankfurter_failed = True
                    break
                if rate is not None:
                    frankfurter_rates.append((d, rate))
            if frankfurter_failed:
                result.control = FxControlResult(
                    currency=currency,
                    n_sessions_compared=0,
                    median_abs_pct_diff=None,
                    note="Frankfurter niedostepny (connection_error), kontrola pominieta",
                )
            else:
                result.control = compute_fx_control(nbp_rates, frankfurter_rates, currency)

            run_key_source = f"fx_nbp:{currency}:{start.isoformat()}:{end.isoformat()}"
            run_key = hashlib.sha256(run_key_source.encode("utf-8")).hexdigest()
            cur.execute(
                """
                INSERT INTO source_runs (source, row_count, sha256_input, triggered_by, code_sha)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (source, sha256_input) DO NOTHING
                """,
                (f"nbp:{currency}", inserted, run_key, current_trigger(), code_sha()),
            )

            summary.results.append(result)

        if commit:
            conn.commit()

    return summary

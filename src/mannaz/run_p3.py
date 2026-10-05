"""Runner CLI dla fazy P3 (ceny i FX) — brief CC-P. Podkomendy osobne, żeby
dało się uruchomić każdy krok niezależnie (np. przez innego agenta):

    python -m mannaz.run_p3 map
    python -m mannaz.run_p3 prices [--instrument-ids ID [ID ...]] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
    python -m mannaz.run_p3 fx [--currencies USD EUR ...] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
    python -m mannaz.run_p3 calendar
    python -m mannaz.run_p3 corp-actions
    python -m mannaz.run_p3 risk [--date YYYY-MM-DD]
    python -m mannaz.run_p3 satellite --from YYYY-MM-DD --to YYYY-MM-DD --out DIR [--account-values CSV] [--symbol-map CSV]

Bez argumentów `--instrument-ids`/`--currencies`/`--start`/`--end`, `prices` i
`fx` robią PEŁNE pobranie (wszystkie zmapowane instrumenty / wszystkie 9 walut,
2023-10-01 -> dziś) — brief P3 zastrzega, że pełne pobranie wykonuje inny
agent; do próby na małym podzbiorze użyj `--instrument-ids` / `--currencies`.

`risk` (brief CC-P, P4.1/P4.2, §19 dokumentu projektowego) bez `--date`
liczy D = ostatnia sesja, dla której WSZYSTKIE instrumenty satelity z cenami
NA TĘ DATĘ mają niepusty close_split_adj (patrz `risk.resolve_default_risk_date`
i uwaga [Z] w brief P4.1 — dziś to zazwyczaj D-1 względem najnowszego wiersza
`prices_daily`, bo Yahoo bywa zwraca świece bez zamknięcia). Wynik: agregaty
WYŁĄCZNIE (liczby pozycji, listy tickerów, procenty kapitału satelity) —
NIGDY per-pozycji ilości/koszty/kwoty (zasada projektu)."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import psycopg

from mannaz.calendar_check import EXCHANGE_TO_CALENDAR_CODE, run_calendar_check
from mannaz.corp_actions import run_corp_actions
from mannaz.cycle import (
    DEFAULT_INCOMING_DIR,
    DEFAULT_RAW_ARCHIVE_DIR,
    DEFAULT_REPORTS_DIR,
    render_level1_by_name,
    run_cycle,
    safe_print,
)
from mannaz.db import get_connection
from mannaz.fx import DEFAULT_START as FX_DEFAULT_START
from mannaz.fx import NBP_CURRENCIES, run_fx_fetch
from mannaz.instruments_map import run_instrument_mapping, run_instrument_mapping_from_file
from mannaz.prices import DEFAULT_START as PRICES_DEFAULT_START
from mannaz.prices import run_prices_fetch, session_closed
from mannaz.risk import level1_name_labels, level1_names_for_summary, run_risk
from mannaz.satellite import run_satellite


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def cmd_map_from_file(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        plan = run_instrument_mapping_from_file(
            conn, args.from_file, dry_run=args.dry_run, commit=True
        )
    finally:
        conn.close()
    counts = plan.counts_per_column()
    mode = "DRY-RUN" if args.dry_run else "WRITE"
    print(f"mode: {mode}")
    print(
        f"changes: {len(plan.changes)} (yahoo_symbol={counts['yahoo_symbol']} "
        f"exchange={counts['exchange']} currency={counts['currency']})"
    )
    print(f"unchanged: {len(plan.unchanged)}")
    print(f"conflicts: {len(plan.conflicts)}")
    for c in plan.conflicts:
        print(f"  CONFLICT {c}")
    print(f"errors: {len(plan.errors)}")
    for e in plan.errors:
        print(f"  ERROR {e}")
    for ch in plan.changes:
        print(f"  CHANGE id={ch.instrument_id} ticker={ch.broker_ticker} {ch.column}: {ch.before} -> {ch.after}")
    if (plan.errors or plan.conflicts) and not args.dry_run:
        print("plan ma bledy/konflikty -> nic nie zapisano")
        sys.exit(1)


def cmd_map(args: argparse.Namespace) -> None:
    if getattr(args, "from_file", None) is not None:
        cmd_map_from_file(args)
        return
    conn = get_connection()
    try:
        summary = run_instrument_mapping(conn)
    finally:
        conn.close()
    print(f"mapped: {len(summary.mapped)}")
    print(f"unmapped: {len(summary.unmapped)}")
    for o in summary.unmapped:
        print(f"  UNMAPPED id={o.instrument_id} ticker={o.broker_ticker} note={o.note}")
    print(f"flagged_for_owner: {len(summary.flagged_for_owner)}")
    for o in summary.flagged_for_owner:
        print(
            f"  FLAG id={o.instrument_id} ticker={o.broker_ticker} "
            f"yahoo_symbol={o.yahoo_symbol} base_symbol={o.base_symbol} "
            f"multiplier={o.multiplier} note={o.note}"
        )


def cmd_prices(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        summary = run_prices_fetch(
            conn,
            instrument_ids=args.instrument_ids,
            start=args.start or PRICES_DEFAULT_START,
            end=args.end,
        )
    finally:
        conn.close()
    print(f"T7: {summary.t7_pass}/{summary.t7_total} currency match")
    # I3 (brief CC-I, B-21): liczniki instrumentów wg statusu bezpiecznika wiersza/odpowiedzi.
    print(
        f"T12: instruments_ok={summary.instruments_ok} "
        f"instruments_with_rejected_rows={summary.instruments_with_rejected_rows} "
        f"instruments_empty_response={summary.instruments_empty_response} "
        f"rows_rejected_total={summary.rows_rejected_total}"
    )
    for r in summary.results:
        print(
            f"  id={r.instrument_id} symbol={r.yahoo_symbol} rows_fetched={r.rows_fetched} "
            f"rows_inserted={r.rows_inserted} rows_rejected={r.rows_rejected} status={r.status} "
            f"currency_check={r.currency_check} "
            f"adjustment_convention={r.adjustment_convention} "
            f"t8_outliers={len(r.log_return_outliers)} note={r.note}"
        )
        for d, lr in r.log_return_outliers:
            print(f"    T8 outlier: {d} log_return={lr}")


def cmd_fx(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        summary = run_fx_fetch(
            conn,
            currencies=tuple(args.currencies) if args.currencies else NBP_CURRENCIES,
            start=args.start or FX_DEFAULT_START,
            end=args.end,
        )
    finally:
        conn.close()
    for r in summary.results:
        if r.error:
            print(f"  {r.currency}: ERROR {r.error}")
            continue
        control = r.control
        control_str = (
            f"median_abs_pct_diff={control.median_abs_pct_diff} n={control.n_sessions_compared} "
            f"note={control.note}"
            if control
            else "brak kontroli"
        )
        print(f"  {r.currency}: fetched={r.rows_fetched} inserted={r.rows_inserted} {control_str}")


def cmd_calendar(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        summary = run_calendar_check(conn)
    finally:
        conn.close()
    print(f"distribution: {summary.distribution}")
    for r in summary.per_instrument:
        if r.symmetric_diff_count > 0:
            print(
                f"  id={r.instrument_id} ticker={r.broker_ticker} calendar={r.calendar_code} "
                f"diff={r.symmetric_diff_count} "
                f"missing_examples={r.missing_prices_for_sessions} "
                f"extra_examples={r.prices_on_non_sessions}"
            )
        elif r.note:
            print(f"  id={r.instrument_id} ticker={r.broker_ticker} note={r.note}")


def cmd_risk(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        summary = run_risk(conn, as_of=args.date)
        with conn.cursor() as cur:
            name_labels = level1_name_labels(cur, summary)
    finally:
        conn.close()

    # Wyłącznie agregaty — bez ilości/kosztów/kwot per pozycja (zasada projektu).
    # Brief CC-U (U2): pozycja bez ceny na D nigdy nie wypada po cichu — D albo
    # jest w pełni policzone (n_positions == wszystkie pozycje), albo run_risk
    # rzuca IncompleteRiskDateError przed dojściem tutaj.
    print(f"D: {summary.risk_date}")
    print(f"n_positions: {len(summary.rows)}")
    print(f"kapital_satelity_pozycje: {summary.capital_satelite_positions_total} "
          f"(per rachunek: {summary.capital_satelite_positions_by_rachunek})")
    print(f"kapital_satelity_pozycje_PLN (equity/etf spoza core, BEZ futures): "
          f"{summary.capital_satelite_positions_pln}")
    print(f"kapital_satelity_gotowka_AKCYJNY+ZAGRANICZNY_PLN: {summary.capital_satelite_cash_pln}")
    print(f"wartosc_rachunku_KONTRAKTOWY_PLN (srodki ogolem + depozyt zablokowany): "
          f"{summary.kontraktowy_account_value_pln}")
    print(f"kapital_satelity_PLN (NAV §21.6, M78; B-17): {summary.capital_satelite_pln}")
    print(f"below_stop (RISK=HIGH) ogolem: {len(summary.below_stop_tickers)} {summary.below_stop_tickers}")
    print(f"below_stop ZAGRANICZNY: {len(summary.below_stop_zagraniczny_tickers)} {summary.below_stop_zagraniczny_tickers}")
    print(f"pct_wartosci_ZAGRANICZNY_satelity_pod_stop_effective: {summary.zagraniczny_satellite_value_pct_below_stop}")
    print(f"stop_source_counts: {summary.stop_source_counts}")
    print(f"heat_pct_kapital_satelity_ogolem: {summary.total_risk_pct_satellite_capital}")
    print(f"heat_pct_kapital_satelity_ZAGRANICZNY: {summary.total_risk_pct_zagraniczny_satellite_capital}")
    print(f"level3_breach (>15%): {summary.level3_breach}")
    # B-34: poziom 1 per nazwa, ta sama linia co w raporcie cyklu.
    level1_line = render_level1_by_name(level1_names_for_summary(summary, name_labels))
    print(level1_line.removeprefix("- "))
    print(f"REGIME: {summary.regime_tickers}")
    print(f"OSTRZEZENIE: {summary.warning_tickers}")
    print(f"liczba_pod_chandelier_from_entry: {len(summary.below_chandelier_from_entry_tickers)} {summary.below_chandelier_from_entry_tickers}")
    print(f"liczba_pod_chandelier_hold (wariant B): {len(summary.variant_b_tickers)} {summary.variant_b_tickers}")
    print(f"wariant_B_pct_wartosci_ZAGRANICZNY_satelity: {summary.variant_b_zagraniczny_satellite_value_pct}")
    print("tematy_pct_kapital_satelity (poziom 2, prog >3%):")
    for theme, result in sorted(summary.theme_budgets.items()):
        print(f"  {theme}: {result.risk_pct} breach={result.breach}")
    print(f"multiplier_missing: {summary.multiplier_missing_tickers}")
    print(f"futures_nominal_sanity (dodatni i rzedu 1e4-1e6): {summary.futures_nominal_sanity}")
    # U4 (brief CC-U, T27): pozycje z cena forward-filled (rynek zamkniety w D,
    # ostatnia cena <=D w granicach 2 sesji) — tylko suma i udzial, bez kwot per pozycja.
    print(f"stale_positions_count (forward-fill T27): {summary.stale_positions_count} {summary.stale_tickers}")
    print(f"stale_capital_PLN (w kapitale satelity): {summary.stale_capital_pln}")
    print(f"stale_capital_pct_kapital_satelity: {summary.stale_capital_pct}")


def cmd_satellite(args: argparse.Namespace) -> None:
    """E3 (brief CC-OS2, §21.6): ocena satelity jako calosci. Tylko SELECT
    (`conn.read_only = True`); wyniki do `--out` (satellite.json, satellite.md,
    benchmarks_cache.csv, ewentualnie prices_cache.csv). Wyłącznie agregaty."""
    conn = get_connection()
    try:
        report = run_satellite(
            conn,
            args.date_from,
            args.date_to,
            args.out,
            account_values=args.account_values,
            symbol_map=args.symbol_map,
        )
    finally:
        conn.close()
    print(f"zakres: {report['d_from']} .. {report['d_to']} (S={report['S']}, T={report['T']})")
    print(f"dni osi: {report['n_axis_days']}, niepelne: {report['n_incomplete_days']}")
    print(f"dane zweryfikowane: {report['data_verified']} {report['data_unverified_reasons']}")
    for name, win in report["windows"].items():
        print(
            f"okno {name}: {win['from']}..{win['to']} kompletne={win['complete']} "
            f"mandat={win['mandate']['status']} werdykt={win['verdict']['label']}"
        )
    print(f"wyniki: {args.out}")


def cmd_corp_actions(args: argparse.Namespace) -> None:
    """B-29/B-45: potwierdzenie ownera — ZAPIS zdarzeń korporacyjnych wykrytych
    przez detektor do corporate_events (tożsamość: `classify_against_existing`).
    `--dry-run` — ta sama klasyfikacja, zero zapisu. Po zapisie ponowny `cycle`
    (FIFO przelicza pozycje od zera z corporate_events). Wydruk bez numerów
    rachunków."""
    conn = get_connection()
    try:
        result = run_corp_actions(conn, dry_run=bool(args.dry_run))
    finally:
        conn.close()

    def _line(ev) -> str:
        return (
            f"  ticker={ev.broker_ticker} data={ev.event_date} typ={ev.event_type} "
            f"ratio={ev.ratio} zrodlo={ev.source}/{ev.date_source}"
        )

    print(f"tryb: {'dry-run (bez zapisu)' if args.dry_run else 'zapis'}")
    print(f"zapisane: {len(result.saved)}")
    for ev in result.saved:
        print(_line(ev))
    print(f"znane: {len(result.known)}")
    for ev in result.known:
        print(_line(ev))
    print(f"date_drift: {len(result.date_drift)}")
    for dr in result.date_drift:
        ev = dr.event
        print(
            f"  ticker={ev.broker_ticker} typ={ev.event_type} ratio={ev.ratio} "
            f"zapisane={dr.stored_date} wykryte={ev.event_date} zrodlo={ev.source}/{ev.date_source}"
        )


@dataclass
class UnclosedRow:
    instrument_id: int
    yahoo_symbol: str
    price_date: date
    fetched_at: datetime
    calendar_code: str | None


@dataclass
class PruneUnclosedResult:
    rows: list[UnclosedRow] = field(default_factory=list)
    deleted: int = 0
    dry_run: bool = True


class PruneCountMismatchError(RuntimeError):
    """B-48 (C3): liczba usuniętych wierszy różna od oczekiwanej — transakcja
    wycofana, nic nie usunięto."""


def find_unclosed_rows(
    conn: psycopg.Connection, instrument_ids: list[int] | None = None
) -> list[UnclosedRow]:
    """B-48 (T46, C3): wiersze `prices_daily` (source='yahoo') zapisane przed
    zamknięciem swojej sesji: `not session_closed(kalendarz, price_date,
    fetched_at)` — ta sama funkcja co filtr importera, czyli kryterium census
    P3b (`fetched_at < zamknięcie + 30 min`; bez kalendarza i gdy kalendarza
    nie da się zastosować: `fetched_at::date <= price_date`). Upsert ustawia
    `fetched_at = now()` przy każdym zapisie, więc wiersz poprawiony po
    zamknięciu sesji tu nie wraca. Tylko SELECT."""
    sql = """
        SELECT p.instrument_id, i.yahoo_symbol, i.exchange, p.price_date, p.fetched_at
        FROM prices_daily p JOIN instruments i ON i.id = p.instrument_id
        WHERE p.source = 'yahoo'
    """
    params: tuple = ()
    if instrument_ids is not None:
        sql += " AND p.instrument_id = ANY(%s)"
        params = (instrument_ids,)
    sql += " ORDER BY p.instrument_id, p.price_date"
    out: list[UnclosedRow] = []
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for iid, ysym, exch, pdate, fetched_at in cur.fetchall():
            code = EXCHANGE_TO_CALENDAR_CODE.get(exch) if exch else None
            if not session_closed(code, pdate, fetched_at):
                out.append(UnclosedRow(iid, ysym, pdate, fetched_at, code))
    return out


def run_prune_unclosed(
    conn: psycopg.Connection,
    dry_run: bool = True,
    expect: int | None = None,
    instrument_ids: list[int] | None = None,
    commit: bool = True,
) -> PruneUnclosedResult:
    """B-48 (C3): jednorazowe (i na przyszłe przerwane przebiegi) usunięcie
    wierszy z `find_unclosed_rows`. `dry_run` — tylko lista, zero zapisu.
    Zapis: DELETE po kluczu naturalnym (instrument_id, price_date, source) w
    JEDNEJ transakcji; gdy liczba usuniętych != liczba kandydatów albo !=
    `expect` (liczba z dry-run) — rollback i `PruneCountMismatchError`.
    `ingest_errors` nie jest ruszane (ślad audytu)."""
    result = PruneUnclosedResult(dry_run=dry_run)
    result.rows = find_unclosed_rows(conn, instrument_ids)
    if dry_run:
        return result  # tylko SELECT; CLI zamyka połączenie bez commit
    if expect is not None and expect != len(result.rows):
        conn.rollback()
        raise PruneCountMismatchError(
            f"kandydatow {len(result.rows)} != oczekiwane (dry-run) {expect} — nic nie usunieto"
        )
    deleted = 0
    with conn.cursor() as cur:
        for r in result.rows:
            cur.execute(
                "DELETE FROM prices_daily WHERE instrument_id = %s AND price_date = %s AND source = 'yahoo'",
                (r.instrument_id, r.price_date),
            )
            deleted += cur.rowcount
    if deleted != len(result.rows):
        conn.rollback()
        raise PruneCountMismatchError(
            f"usunieto {deleted} != kandydatow {len(result.rows)} — rollback, nic nie usunieto"
        )
    result.deleted = deleted
    if commit:
        conn.commit()
    return result


def cmd_prune_unclosed(args: argparse.Namespace) -> None:
    """B-48 (C3): `prune-unclosed [--dry-run] [--expect N]`. Wydruk: symbol,
    data, fetched_at, kalendarz — bez numerów rachunków. Zapis wymaga
    `--expect` (liczba kandydatów z dry-run)."""
    if not args.dry_run and args.expect is None:
        print("prune-unclosed bez --dry-run wymaga --expect N (liczba z dry-run)")
        sys.exit(2)
    conn = get_connection()
    try:
        result = run_prune_unclosed(conn, dry_run=bool(args.dry_run), expect=args.expect)
    finally:
        conn.close()
    print(f"tryb: {'dry-run (bez zapisu)' if result.dry_run else 'zapis'}")
    print(f"kandydaci: {len(result.rows)}")
    for r in result.rows:
        print(
            f"  {r.instrument_id}\t{r.yahoo_symbol}\t{r.price_date}\t"
            f"{r.fetched_at.isoformat()}\t{r.calendar_code or 'BRAK'}"
        )
    if not result.dry_run:
        print(f"usuniete: {result.deleted}")


def cmd_cycle(args: argparse.Namespace) -> None:
    conn = get_connection()
    try:
        result = run_cycle(
            conn,
            incoming_dir=args.incoming_dir,
            raw_archive_dir=args.raw_archive_dir,
            reports_dir=args.reports_dir,
            today=args.today,
        )
    finally:
        conn.close()

    # Q3 (poprawka po przeglądzie, po STOP przebiegu #2): raport pod
    # `result.report_path` jest już zapisany na dysku (albo wcale, gdy
    # check-ignore odmówiło — `result.error_message`) ZANIM cokolwiek tu
    # drukujemy. `safe_print` zamiast `print`, żeby konsola w kodowaniu bez
    # pełnego pokrycia Unicode (np. cp1250) nigdy nie wywaliła się na
    # UnicodeEncodeError.
    if result.error_message is not None:
        safe_print(result.error_message)
        sys.exit(result.exit_code)

    # Wyłącznie ścieżka raportu i krótkie podsumowanie agregatów — bez
    # ilości/kwot per pozycja (zasada projektu).
    safe_print(f"raport: {result.report_path}")
    safe_print(f"D: {result.state.d}")
    safe_print(f"kod_wyjscia: {result.exit_code}")
    safe_print(f"data_failure: {result.exit_code != 0}")
    sys.exit(result.exit_code)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Brief CC-P, faza P3 (ceny i FX)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_map = sub.add_parser(
        "map", help="P3.1 — mapowanie instruments.yahoo_symbol/currency/exchange/instrument_type/name"
    )
    p_map.add_argument("--from-file", type=Path, default=None,
                       help="CSV z zatwierdzonym mapowaniem (zapis kontrolowany, tylko yahoo_symbol/exchange/currency)")
    p_map.add_argument("--dry-run", action="store_true",
                       help="z --from-file: tylko plan, zero zapisu")
    p_map.set_defaults(func=cmd_map)

    p_prices = sub.add_parser("prices", help="P3.2 — pobranie prices_daily")
    p_prices.add_argument(
        "--instrument-ids", type=int, nargs="*", default=None, help="podzbiór do próby zamiast pełnego pobrania"
    )
    p_prices.add_argument("--start", type=_parse_date, default=None)
    p_prices.add_argument("--end", type=_parse_date, default=None)
    p_prices.set_defaults(func=cmd_prices)

    p_fx = sub.add_parser("fx", help="P3.3 — pobranie fx_nbp + kontrola Frankfurter")
    p_fx.add_argument(
        "--currencies", type=str, nargs="*", default=None, help="podzbiór walut zamiast pełnej listy 9"
    )
    p_fx.add_argument("--start", type=_parse_date, default=None)
    p_fx.add_argument("--end", type=_parse_date, default=None)
    p_fx.set_defaults(func=cmd_fx)

    p_cal = sub.add_parser("calendar", help="P3.4 — porownanie dat cenowych z exchange_calendars")
    p_cal.set_defaults(func=cmd_calendar)

    p_risk = sub.add_parser("risk", help="P4.1/P4.2 — ATR/stopy/REGIME/ryzyko PLN per pozycja (risk_daily)")
    p_risk.add_argument(
        "--date", type=_parse_date, default=None,
        help="D; domyslnie ostatnia sesja z kompletnymi close_split_adj dla satelity"
    )
    p_risk.set_defaults(func=cmd_risk)

    p_sat = sub.add_parser("satellite", help="E3 — ocena satelity jako calosci (§21.6, tylko SELECT)")
    p_sat.add_argument("--from", dest="date_from", type=_parse_date, required=True, help="poczatek osi dni (YYYY-MM-DD)")
    p_sat.add_argument("--to", dest="date_to", type=_parse_date, required=True, help="koniec osi dni (YYYY-MM-DD)")
    p_sat.add_argument("--out", type=Path, required=True, help="katalog wynikow")
    p_sat.add_argument(
        "--account-values", type=Path, default=None,
        help="CSV migawek brokera (date,rachunek,currency,value) dla bramki M78"
    )
    p_sat.add_argument(
        "--symbol-map", type=Path, default=None,
        help="CSV broker_ticker,isin,yahoo_symbol,exchange_mic,currency dla instrumentow bez cen"
    )
    p_sat.set_defaults(func=cmd_satellite)

    p_corp = sub.add_parser(
        "corp-actions", help="B-29 — potwierdzenie: zapis wykrytych zdarzen korporacyjnych do corporate_events"
    )
    p_corp.add_argument("--dry-run", action="store_true",
                        help="B-45: ta sama klasyfikacja (zapisane/znane/date_drift), zero zapisu")
    p_corp.set_defaults(func=cmd_corp_actions)

    p_prune = sub.add_parser(
        "prune-unclosed",
        help="B-48 (T46) — usuniecie wierszy prices_daily zapisanych przed zamknieciem sesji",
    )
    p_prune.add_argument("--dry-run", action="store_true", help="tylko lista kandydatow, zero zapisu")
    p_prune.add_argument(
        "--expect", type=int, default=None,
        help="liczba kandydatow z dry-run; rozjazd = rollback, nic nie usuniete"
    )
    p_prune.set_defaults(func=cmd_prune_unclosed)

    p_cycle = sub.add_parser(
        "cycle", help="C2-C7 — cykl tygodniowy: import -> FIFO -> bramka rejestracji -> ceny/FX -> ryzyko -> raport"
    )
    p_cycle.add_argument("--incoming-dir", type=Path, default=DEFAULT_INCOMING_DIR)
    p_cycle.add_argument("--raw-archive-dir", type=Path, default=DEFAULT_RAW_ARCHIVE_DIR)
    p_cycle.add_argument("--reports-dir", type=Path, default=DEFAULT_REPORTS_DIR)
    p_cycle.add_argument("--today", type=_parse_date, default=None)
    p_cycle.set_defaults(func=cmd_cycle)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

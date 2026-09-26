"""Runner CLI dla fazy P3 (ceny i FX) — brief CC-P. Podkomendy osobne, żeby
dało się uruchomić każdy krok niezależnie (np. przez innego agenta):

    python -m mannaz.run_p3 map
    python -m mannaz.run_p3 prices [--instrument-ids ID [ID ...]] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
    python -m mannaz.run_p3 fx [--currencies USD EUR ...] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
    python -m mannaz.run_p3 calendar
    python -m mannaz.run_p3 risk [--date YYYY-MM-DD]

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
from datetime import date

from mannaz.calendar_check import run_calendar_check
from mannaz.db import get_connection
from mannaz.fx import DEFAULT_START as FX_DEFAULT_START
from mannaz.fx import NBP_CURRENCIES, run_fx_fetch
from mannaz.instruments_map import run_instrument_mapping
from mannaz.prices import DEFAULT_START as PRICES_DEFAULT_START
from mannaz.prices import run_prices_fetch
from mannaz.risk import run_risk


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def cmd_map(args: argparse.Namespace) -> None:
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
    for r in summary.results:
        print(
            f"  id={r.instrument_id} symbol={r.yahoo_symbol} rows_fetched={r.rows_fetched} "
            f"rows_inserted={r.rows_inserted} currency_check={r.currency_check} "
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
    finally:
        conn.close()

    # Wyłącznie agregaty — bez ilości/kosztów/kwot per pozycja (zasada projektu).
    print(f"D: {summary.risk_date}")
    print(f"n_positions: {len(summary.rows)} (+{len(summary.excluded_no_price_tickers)} bez ceny/bazy na D)")
    print(f"kapital_satelity_pozycje: {summary.capital_satelite_positions_total} "
          f"(per rachunek: {summary.capital_satelite_positions_by_rachunek})")
    print(f"below_stop (RISK=HIGH) ogolem: {len(summary.below_stop_tickers)} {summary.below_stop_tickers}")
    print(f"below_stop ZAGRANICZNY: {len(summary.below_stop_zagraniczny_tickers)} {summary.below_stop_zagraniczny_tickers}")
    print(f"pct_wartosci_ZAGRANICZNY_satelity_pod_stop_effective: {summary.zagraniczny_satellite_value_pct_below_stop}")
    print(f"stop_source_counts: {summary.stop_source_counts}")
    print(f"heat_pct_kapital_satelity_ogolem: {summary.total_risk_pct_satellite_capital}")
    print(f"heat_pct_kapital_satelity_ZAGRANICZNY: {summary.total_risk_pct_zagraniczny_satellite_capital}")
    print(f"level3_breach (>15%): {summary.level3_breach}")
    print(f"level1_breach_tickers (>1%): {summary.level1_breach_tickers}")
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
    if summary.excluded_no_price_tickers:
        print(f"wykluczone_brak_ceny_na_D: {summary.excluded_no_price_tickers}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Brief CC-P, faza P3 (ceny i FX)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_map = sub.add_parser(
        "map", help="P3.1 — mapowanie instruments.yahoo_symbol/currency/exchange/instrument_type/name"
    )
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

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

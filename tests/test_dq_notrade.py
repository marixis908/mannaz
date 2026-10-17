"""B-78: korekta reczna `apply_manual_corrections(notrade=True)` (brak obrotu
potwierdzony w TV) i flaga CLI `--notrade`. Testy bez bazy: falszywe polaczenie
(SQL rozpoznawany po tresci), dane syntetyczne."""

from __future__ import annotations

import argparse
from datetime import date
from decimal import Decimal

import pytest

from mannaz import prices, run_p3
from mannaz.prices import NOTRADE_CONVENTION, apply_manual_corrections

D = Decimal
D1 = date(1990, 1, 2)
FILLER = (D("7"), D("7"), D("7"), D("7"), D("0"))


class _Cur:
    def __init__(self, stored):
        self.stored = stored
        self.updates: list[tuple] = []
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        if sql.lstrip().startswith("UPDATE prices_daily"):
            self.updates.append(params)

    def fetchall(self):
        return [(1,)] if "FROM instruments" in self._last else []

    def fetchone(self):
        return self.stored


class _Conn:
    def __init__(self, stored=FILLER):
        self.cur = _Cur(stored)
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    monkeypatch.setattr(prices, "_ensure_ingest_errors_table", lambda cur: None)
    monkeypatch.setattr(prices, "_instrument_split_events", lambda cur, iid: [])
    logged: list[dict] = []
    monkeypatch.setattr(prices, "_log_ingest_error", lambda cur, **kw: logged.append(kw))
    return logged


def test_notrade_valid_row_written_with_notrade_convention(_patch):
    conn = _Conn()
    s = apply_manual_corrections(
        conn, [("SYN.WA", D1, D("7"), D("7"), D("7"), D("7"), D("0"))], dry_run=False, notrade=True
    )
    assert s.rejected == [] and s.rows_written == 1
    assert conn.commits == 1
    (params,) = conn.cur.updates
    assert params[:5] == (D("7"), D("7"), D("7"), D("7"), D("0"))  # O/H/L/C wg pliku, volume 0
    assert NOTRADE_CONVENTION in params
    assert _patch[0]["error_type"] == "manual_correction"
    assert _patch[0]["detail"].startswith("B-78:")


def test_notrade_can_set_new_flat_price_different_from_filler(_patch):
    conn = _Conn()
    s = apply_manual_corrections(
        conn, [("SYN.WA", D1, D("8"), D("8"), D("8"), D("8"), D("0"))], dry_run=False, notrade=True
    )
    assert s.rows_written == 1 and conn.cur.updates[0][:5] == (D("8"),) * 4 + (D("0"),)


def test_notrade_volume_positive_rejected_ohlc():
    conn = _Conn()
    with pytest.raises(prices.ManualCorrectionRejectedError) as ei:
        apply_manual_corrections(
            conn, [("SYN.WA", D1, D("7"), D("7"), D("7"), D("7"), D("10"))], dry_run=False, notrade=True
        )
    assert ei.value.summary.rejected == [("SYN.WA", D1, "ohlc")]
    assert conn.cur.updates == [] and conn.commits == 0


def test_notrade_open_not_close_rejected_ohlc():
    conn = _Conn()
    s = apply_manual_corrections(
        conn, [("SYN.WA", D1, D("7"), D("7.5"), D("7"), D("7.5"), D("0"))], dry_run=True, notrade=True
    )
    assert s.rejected == [("SYN.WA", D1, "ohlc")]


def test_notrade_zero_price_rejected():
    conn = _Conn()
    s = apply_manual_corrections(
        conn, [("SYN.WA", D1, D("0"), D("0"), D("0"), D("0"), D("0"))], dry_run=True, notrade=True
    )
    assert s.rejected == [("SYN.WA", D1, "ohlc")]


def test_notrade_stored_not_filler_rejected():
    conn = _Conn(stored=(D("8"), D("9"), D("7"), D("8.5"), D("100")))
    s = apply_manual_corrections(
        conn, [("SYN.WA", D1, D("7"), D("7"), D("7"), D("7"), D("0"))], dry_run=True, notrade=True
    )
    assert s.rejected == [("SYN.WA", D1, "nie filler")]


def test_normal_mode_unchanged_volume_zero_rejected_and_b76_convention(_patch):
    conn = _Conn()
    s = apply_manual_corrections(
        conn, [("SYN.WA", D1, D("7"), D("7"), D("7"), D("7"), D("0"))], dry_run=True
    )
    assert s.rejected == [("SYN.WA", D1, "ohlc")]  # tryb zwykly wymaga volume > 0
    conn = _Conn()
    s = apply_manual_corrections(
        conn, [("SYN.WA", D1, D("8"), D("9"), D("7"), D("8.5"), D("100"))], dry_run=False
    )
    assert s.rows_written == 1
    assert "tradingview_manual_b76" in conn.cur.updates[0]
    assert _patch[0]["detail"].startswith("B-76:")


def test_notrade_with_other_convention_is_error():
    with pytest.raises(ValueError):
        apply_manual_corrections(_Conn(), [], convention="tradingview_manual_b76", notrade=True)


def test_cli_notrade_with_convention_exits_2(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("yahoo_symbol,date,open,high,low,close,volume\n", encoding="utf-8")
    args = argparse.Namespace(file=str(f), dry_run=True, convention="tradingview_manual_b76", notrade=True)
    with pytest.raises(SystemExit) as ei:
        run_p3.cmd_prices_correct(args)
    assert ei.value.code == 2


def test_cli_parser_has_notrade_flag():
    parser = run_p3.build_parser() if hasattr(run_p3, "build_parser") else None
    if parser is None:
        pytest.skip("brak build_parser w run_p3")
    ns = parser.parse_args(["prices-correct", "--file", "x.csv", "--notrade"])
    assert ns.notrade is True and ns.convention is None

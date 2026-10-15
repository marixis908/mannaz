"""Testy wymiany waluty na rachunku (brief CC-FX, B-63): parser, kontrola par nog,
przeplyw zewnetrzny i atrybucja M85. WYLACZNIE dane syntetyczne (poza (g): dry-run
pliku zrodlowego, ktory jest tylko czytany)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from mannaz import satellite as sat
from mannaz.parse_history import parse_source_file, parse_title
from mannaz.satellite import TxRow

ACC = "ZAGRANICZNY 00-TEST"
HEADER = "Data;Rachunek;Waluta;Tytuł przelewu;Wartość\n"
T_FX = "Wymiana waluty PLN/USD 3.9011"
T_FEE = "Opłata za wymianę waluty PLN/USD"


def _csv(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "fx.csv"
    p.write_bytes(b"\xef\xbb\xbf" + (HEADER + "".join(lines)).encode("utf-8"))
    return p


def _line(cur, title, amount, d="05.03.2024"):
    return f"{d};{ACC};{cur};{title};{amount}\n"


# --- (a) parse_title -------------------------------------------------------------


def test_a_exchange_title():
    p = parse_title(T_FX)
    assert p.row_type == "wymiana_waluty"
    assert p.extra == {"pair": "PLN/USD", "rate": Decimal("3.9011")}
    assert isinstance(p.extra["rate"], Decimal)
    assert p.ticker is None


def test_a_exchange_fee_title():
    p = parse_title(T_FEE)
    assert p.row_type == "oplata_wymiana_waluty"
    assert p.extra == {"pair": "PLN/USD"}
    assert p.ticker is None


def test_a_exchange_title_without_rate_is_unknown():
    assert parse_title("Wymiana waluty PLN/USD").row_type == "unknown"


# --- (b)-(d) parse_source_file -----------------------------------------------------


def test_b_matching_legs_and_fee(tmp_path):
    path = _csv(tmp_path, [_line("PLN", T_FX, "-100000,00"), _line("USD", T_FX, "25633,80"),
                           _line("PLN", T_FEE, "-49,00")])
    res = parse_source_file(path)
    assert res.unknown_titles == []
    types = [r.parsed.row_type for r in res.rows]
    assert types.count("wymiana_waluty") == 2
    assert types.count("oplata_wymiana_waluty") == 1


def test_c_missing_foreign_leg_is_unknown(tmp_path):
    path = _csv(tmp_path, [_line("PLN", T_FX, "-100000,00"), _line("PLN", T_FEE, "-49,00")])
    res = parse_source_file(path)
    assert res.rows[0].parsed.row_type == "unknown"
    assert T_FX in res.unknown_titles
    assert res.rows[1].parsed.row_type == "oplata_wymiana_waluty"


def test_d_amount_mismatch_above_tolerance_marks_both_legs(tmp_path):
    path = _csv(tmp_path, [_line("PLN", T_FX, "-100000,00"), _line("USD", T_FX, "25600,00")])
    res = parse_source_file(path)
    assert [r.parsed.row_type for r in res.rows] == ["unknown", "unknown"]
    assert res.unknown_titles == [T_FX, T_FX]


def test_d_amount_within_tolerance_ok(tmp_path):
    # 25633,80 * 3.9011 = 100000,01718; tolerancja 0,01 * 3.9011 = 0,039011
    ok = _csv(tmp_path, [_line("PLN", T_FX, "-100000,05"), _line("USD", T_FX, "25633,80")])  # roznica 0,033
    assert parse_source_file(ok).unknown_titles == []
    bad = _csv(tmp_path, [_line("PLN", T_FX, "-100000,06"), _line("USD", T_FX, "25633,80")])  # roznica 0,043
    assert len(parse_source_file(bad).unknown_titles) == 2


# --- (e) przeplyw zewnetrzny -----------------------------------------------------------


D = date(2024, 3, 5)
RATE = 3.9011
USD_AMT = 25633.80


def _day_rows():
    return [
        TxRow(D, ACC, "PLN", "wymiana_waluty", -100000.0),
        TxRow(D, ACC, "USD", "wymiana_waluty", USD_AMT),
        TxRow(D, ACC, "PLN", "oplata_wymiana_waluty", -49.0),
    ]


def _fx_fn(c, d):
    return {"PLN": 1.0, "USD": RATE}.get(c)


def test_e_external_flow_zero():
    res = sat.external_flow_pln(_day_rows(), _fx_fn)
    assert res.value == 0.0
    assert res.missing == []


# --- (f) atrybucja dnia ------------------------------------------------------------------


def _nav(cash, fx):
    return sat.nav_for_day([], cash, fx, None)


def test_f_attribution_fee_line_and_spread_in_cash_lines():
    key_pln, key_usd = ("ZAGRANICZNY", "PLN"), ("ZAGRANICZNY", "USD")
    nav_p = _nav({key_pln: 150000.0, key_usd: 0.0}, {"USD": RATE})
    nav_d = _nav({key_pln: 150000.0 - 100000.0 - 49.0, key_usd: USD_AMT}, {"USD": RATE})
    rows = _day_rows()
    instruments = {}
    lines = sat.attribute_day(
        nav_p, nav_d, rows, [], _fx_fn, lambda r: None, instruments, sat.build_yahoo_map(instruments),
    )
    assert lines is not None
    flow = sat.external_flow_pln(rows, _fx_fn).value
    diff = sat.identity_diff(lines, nav_p.value, nav_d.value, flow)
    assert diff is not None and abs(diff) <= sat.ATTRIB_DAILY_TOL
    assert not [ln for ln in lines if ln.kind == "koszty_nieprzypisane"]
    fees = [ln for ln in lines if ln.kind == "oplaty"]
    assert len(fees) == 1
    assert fees[0].emitent == "oplaty ZAGRANICZNY"
    assert fees[0].total == pytest.approx(-49.0)
    cash = [ln for ln in lines if ln.kind == "gotowka"]
    assert {ln.emitent for ln in cash} == {"gotowka ZAGRANICZNY/PLN", "gotowka ZAGRANICZNY/USD"}
    # kurs NBP = kurs wymiany, brak zmiany kursu: suma gotowki = roznica wartosci nog w PLN
    assert sum(ln.fx for ln in cash) == pytest.approx(0.0, abs=1e-9)
    assert sum(ln.total for ln in cash) == pytest.approx(USD_AMT * RATE - 100000.0, abs=1e-6)


# --- (g) dry-run pliku zrodlowego (tylko odczyt) ---------------------------------------------


def test_g_dry_run_source_file():
    path = Path(r"C:\Users\MariuszBrysik\projects\Mannaz\_incoming\financeHistory (16).csv")
    if not path.exists():
        pytest.skip("brak pliku zrodlowego")
    res = parse_source_file(path)
    assert res.unknown_titles == []
    types = [r.parsed.row_type for r in res.rows]
    assert types.count("wymiana_waluty") >= 2
    assert types.count("oplata_wymiana_waluty") >= 1

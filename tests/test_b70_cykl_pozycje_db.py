"""B-70 / B70.2 / B-73 / B-74 / B-80 / B-81 (brief CC-B70, C6): testy bazodanowe.

Wzorzec `tests/test_cycle_db.py`: JEDNA transakcja na test, `commit=False`,
`conn.rollback()` w `finally`, zero trwalych zmian. Zero sieci: ceny/FX/ryzyko/
satelita to stuby, `run_fifo` i `_registration_rows` podmieniane monkeypatchem
(jak `_b05_run`). Dane syntetyczne: instrumenty `B70*`, rachunki "<TYP> SYNTH-B70"
(bez numerow), daty 1990. W testach, ktore potrzebuja pustego `risk_daily`, jest
`DELETE FROM risk_daily` — w transakcji testu, cofany rollbackiem.

Czesc czysta (render, logika bez bazy, T6 logger) jest w `test_b70_cykl_pozycje.py`."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import pytest

from mannaz import prices
from mannaz.cycle import STOP_ORDER_HIGH_MESSAGE, run_cycle
from mannaz.prices import PricesSummary
from mannaz.risk import (
    STOP_RULE_VERSION,
    IncompleteRiskDateError,
    IncompleteRiskItem,
    PositionRiskRow,
    RiskSummary,
)
from test_cycle_db import (  # noqa: F401  (db_conn = fixture)
    D_B05,
    _run_cycle_kwargs,
    _stub_risk_fn,
    db_conn,
)

ACC = "AKCYJNY SYNTH-B70"
ACC_KONTR = "KONTRAKTOWY SYNTH-B70"
D = D_B05  # 1990-03-09 (jak w testach B-05)
PREV = date(1990, 3, 5)


# ---------------------------------------------------------------------------
# helpery: dane syntetyczne w transakcji testu
# ---------------------------------------------------------------------------


def _instrument(cur, ticker, *, yahoo=None, itype="equity", base=None, multiplier=None, ccy="PLN") -> int:
    cur.execute(
        "INSERT INTO instruments (broker_ticker, name, yahoo_symbol, currency, exchange, instrument_type, "
        "base_symbol, multiplier) VALUES (%s, %s, %s, %s, 'GPW', %s, %s, %s) RETURNING id",
        (ticker, f"SYNTH {ticker}", yahoo, ccy, itype, base, multiplier),
    )
    return cur.fetchone()[0]


def _tx(cur, day: date, rachunek: str, instrument_id: int, row_type: str, qty: str = "10", price: str = "10") -> None:
    cur.execute(
        "INSERT INTO transactions (transaction_date, rachunek, currency, title_raw, amount, row_type, instrument_id, "
        "qty, price, source_file, source_sha256) VALUES (%s, %s, 'PLN', %s, %s, %s, %s, %s, %s, 'b70_test', %s)",
        (
            day, rachunek, f"B70 TEST {row_type} i{instrument_id}", Decimal(qty) * Decimal(price) * (-1 if row_type == "kupno" else 1),
            row_type, instrument_id, Decimal(qty), Decimal(price), "0" * 64,
        ),
    )


def _risk_marker(cur, instrument_id: int, day: date, rachunek: str = ACC) -> None:
    cur.execute(
        "INSERT INTO risk_daily (rachunek, instrument_id, risk_date, settlement_currency) VALUES (%s, %s, %s, 'PLN')",
        (rachunek, instrument_id, day),
    )


def _mapped_ids(cur, today=None) -> set[int]:
    if today is None:
        return {m["id"] for m in prices._mapped_instruments(cur, None)}
    return {m["id"] for m in prices._mapped_instruments(cur, None, today=today)}


# ===========================================================================
# T1 (B-70) — prices._mapped_instruments(today=...)
# ===========================================================================

TODAY_T1 = date(1990, 3, 10)


@pytest.mark.db
def test_t1_positions_open_in_window_prev_d_today_are_mapped_closed_before_are_not(db_conn):
    conn = db_conn
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")  # w transakcji testu; rollback cofa
            a = _instrument(cur, "B70A", yahoo="B70A.WA")  # kupno 03-01, sprzedaz 03-07: otwarta 03-05/06
            b = _instrument(cur, "B70B", yahoo="B70B.WA")  # kupno 03-01, sprzedaz 03-03: zamknieta PRZED prev_D
            b2 = _instrument(cur, "B70B2", yahoo="B70B2.WA")  # zamknieta 03-04 (dzien przed prev_D)
            c = _instrument(cur, "B70C", yahoo="B70C.WA")  # otwarta i zamknieta MIEDZY: 03-06..03-07
            g = _instrument(cur, "B70G", yahoo="B70G.WA")  # kupiona w dniu today, nadal otwarta
            _tx(cur, date(1990, 3, 1), ACC, a, "kupno")
            _tx(cur, date(1990, 3, 7), ACC, a, "sprzedaz")
            _tx(cur, date(1990, 3, 1), ACC, b, "kupno")
            _tx(cur, date(1990, 3, 3), ACC, b, "sprzedaz")
            _tx(cur, date(1990, 3, 1), ACC, b2, "kupno")
            _tx(cur, date(1990, 3, 4), ACC, b2, "sprzedaz")
            _tx(cur, date(1990, 3, 6), ACC, c, "kupno")
            _tx(cur, date(1990, 3, 8), ACC, c, "sprzedaz")
            _tx(cur, TODAY_T1, ACC, g, "kupno")
            _risk_marker(cur, a, PREV)  # prev_D = MAX(risk_date) = 1990-03-05

            bare = _mapped_ids(cur)  # dotychczasowe zachowanie (bez today)
            windowed = _mapped_ids(cur, TODAY_T1)

            assert {a, b, b2, c, g}.isdisjoint(bare), "kontrolka: bez today zadna syntetyczna pozycja nie jest w zbiorze"
            assert windowed - bare == {a, c, g}
            assert b not in windowed and b2 not in windowed
            assert bare <= windowed  # B-70 tylko DODAJE
    finally:
        conn.rollback()


@pytest.mark.db
def test_t1_control_empty_risk_daily_gives_set_identical_to_call_without_today(db_conn):
    conn = db_conn
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")
            a = _instrument(cur, "B70A", yahoo="B70A.WA")
            _tx(cur, date(1990, 3, 1), ACC, a, "kupno")  # otwarta w calym oknie — ale risk_daily puste
            bare = _mapped_ids(cur)
            assert _mapped_ids(cur, TODAY_T1) == bare
            assert a not in bare
            # kontrolka dodatnia tego samego wejscia: po dodaniu znacznika prev_D okno dziala
            _risk_marker(cur, a, PREV)
            assert a in _mapped_ids(cur, TODAY_T1)
    finally:
        conn.rollback()


@pytest.mark.db
def test_t1_control_prev_d_after_today_gives_set_identical_to_call_without_today(db_conn):
    conn = db_conn
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")
            a = _instrument(cur, "B70A", yahoo="B70A.WA")
            _tx(cur, date(1990, 3, 1), ACC, a, "kupno")
            _risk_marker(cur, a, PREV)
            early_today = date(1990, 3, 4)  # today < prev_D -> przedzial pusty
            assert _mapped_ids(cur, early_today) == _mapped_ids(cur)
            assert a in _mapped_ids(cur, TODAY_T1)  # kontrolka dodatnia
    finally:
        conn.rollback()


@pytest.mark.db
def test_t1_explicit_instrument_ids_ignore_today(db_conn):
    conn = db_conn
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")
            a = _instrument(cur, "B70A", yahoo="B70A.WA")
            c = _instrument(cur, "B70C", yahoo="B70C.WA")
            _tx(cur, date(1990, 3, 1), ACC, a, "kupno")
            _risk_marker(cur, a, PREV)
            got = {m["id"] for m in prices._mapped_instruments(cur, [c], today=TODAY_T1)}
            assert got == {c}
    finally:
        conn.rollback()


@pytest.mark.db
def test_t1_future_base_of_contract_open_in_window_is_mapped_base_of_closed_is_not(db_conn):
    """Wariant future: kontrakt (bez wlasnego yahoo_symbol) otwarty w przedziale -> do zbioru wchodzi
    instrument BAZOWY (yahoo_symbol == base_symbol kontraktu); baza kontraktu zamknietego przed prev_D nie."""
    conn = db_conn
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")
            base_open = _instrument(cur, "B70BASEO", yahoo="B70BASEO.WA")
            base_closed = _instrument(cur, "B70BASEC", yahoo="B70BASEC.WA")
            fut_open = _instrument(cur, "FB70O", itype="future", base="B70BASEO.WA", multiplier=Decimal("10"))
            fut_closed = _instrument(cur, "FB70C", itype="future", base="B70BASEC.WA", multiplier=Decimal("10"))
            _tx(cur, date(1990, 3, 1), ACC_KONTR, fut_open, "kupno")
            _tx(cur, date(1990, 3, 7), ACC_KONTR, fut_open, "sprzedaz")  # otwarty 03-05/06
            _tx(cur, date(1990, 3, 1), ACC_KONTR, fut_closed, "kupno")
            _tx(cur, date(1990, 3, 3), ACC_KONTR, fut_closed, "sprzedaz")  # zamkniety przed prev_D
            _risk_marker(cur, fut_open, PREV, ACC_KONTR)

            bare = _mapped_ids(cur)
            windowed = _mapped_ids(cur, TODAY_T1)
            assert base_open not in bare and base_closed not in bare  # kontrolka: bez today brak
            assert base_open in windowed
            assert base_closed not in windowed
            # sam kontrakt nie ma yahoo_symbol -> nie jest instrumentem do pobrania
            assert fut_open not in windowed and fut_closed not in windowed
    finally:
        conn.rollback()


# ===========================================================================
# helpery run_cycle
# ===========================================================================


def _first_instrument(conn) -> tuple[int, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, broker_ticker FROM instruments ORDER BY id LIMIT 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("brak instrumentu w bazie testowej")
    return row


def _cycle(tmp_path, conn, monkeypatch, *, risk_fn=None, account_values_dir=None, **overrides):
    monkeypatch.setattr("mannaz.cycle.run_fifo", lambda *a, **k: None)
    monkeypatch.setattr("mannaz.cycle._registration_rows", lambda cur: [])
    kwargs = _run_cycle_kwargs(
        tmp_path,
        conn,
        risk_fn=risk_fn or _stub_risk_fn(D),
        resolve_date_fn=lambda conn_, **k: D,
        account_values_dir=account_values_dir if account_values_dir is not None else tmp_path / "av_pusty",
    )
    kwargs.update(overrides)
    return run_cycle(**kwargs)


def _section(text: str, header: str) -> str:
    assert header in text, f"brak sekcji {header!r}"
    return text.split(header, 1)[1].split("\n## ", 1)[0]


# ===========================================================================
# T2 (B70.2) — brak ceny pozycji otwartej na D -> "D niekompletne" (run_cycle)
# Odwolanie: tests/test_risk_pit_db.py::test_u5_open_market_gap_on_real_data_raises_and_writes_nothing
# (run_risk na prawdziwych danych z usunietymi cenami GPW na D -> IncompleteRiskDateError).
# ===========================================================================


@pytest.mark.db
def test_t2_incomplete_risk_date_lists_ticker_in_report_and_still_has_position_changes(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        _first_instrument(conn)

        def risk_fn(c, as_of, commit):
            raise IncompleteRiskDateError(as_of, [IncompleteRiskItem("B70AAA", "B70AAA.WA", "GPW", "market_open_no_price")])

        result = _cycle(tmp_path, conn, monkeypatch, risk_fn=risk_fn)
        text = result.report_path.read_text(encoding="utf-8")
        assert [it.broker_ticker for it in result.state.date_incomplete_items] == ["B70AAA"]
        assert result.exit_code == 2
        sec = _section(text, "### D niekompletne").split("\n### ", 1)[0]
        assert "- B70AAA · B70AAA.WA · GPW · market_open_no_price" in sec
        # B-74: sekcja zmian pozycji powstaje takze przy IncompleteRiskDateError (liczona PRZED risk_fn)
        assert result.state.position_changes_computed is True
        assert "nie wykonano (cykl zatrzymany przed ustaleniem D)" not in _section(text, "## Zmiany pozycji")
    finally:
        conn.rollback()


# ===========================================================================
# T3 (B-73) — katalog kotwic w run_cycle
# ===========================================================================

_HDR = "date,account,currency,positions_value,cash_total\n"


class _SatSpy:
    def __init__(self):
        self.snapshots = None

    def __call__(self, conn, d, **kwargs):
        from _satstub import minimal_stage_result

        self.snapshots = kwargs.get("snapshots")
        return minimal_stage_result(d)


@pytest.mark.db
def test_t3_cycle_bad_anchor_file_is_data_failure_with_file_name(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        _first_instrument(conn)
        av = tmp_path / "av"
        av.mkdir()
        (av / "account_values_2024-01-05.csv").write_text(
            _HDR + "2024-01-05,AKCYJNY SYNTH-B70,PLN,1,1\n", encoding="utf-8"
        )
        (av / "account_values_2024-01-12.csv").write_text(
            _HDR + "2024-01-11,AKCYJNY SYNTH-B70,PLN,1,1\n", encoding="utf-8"  # data != data z nazwy
        )
        spy = _SatSpy()
        result = _cycle(tmp_path, conn, monkeypatch, account_values_dir=av, satellite_fn=spy)
        assert result.exit_code == 2
        assert result.state.satellite is None and result.state.satellite_error is True
        assert spy.snapshots is None  # etap satelity nie zostal wywolany
        failure = next(f for f in result.state.import_failures if f.startswith("błąd etapu satelity: AccountValuesError"))
        assert "account_values_2024-01-12.csv" in failure
        text = result.report_path.read_text(encoding="utf-8")
        assert "DATA FAILURE" in text
        assert "account_values_2024-01-12.csv" in _section(text, "## 🔴 DATA FAILURE")
        assert "nie wykonano (błąd etapu satelity" in _section(text, "## Satelita (§21.6)")
    finally:
        conn.rollback()


@pytest.mark.db
def test_t3_cycle_passes_merged_sorted_anchors_to_satellite_stage(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        _first_instrument(conn)
        av = tmp_path / "av"
        av.mkdir()
        (av / "account_values_2024-02-02.csv").write_text(_HDR + "2024-02-02,AKCYJNY SYNTH-B70,PLN,1,1\n", encoding="utf-8")
        (av / "account_values_2024-01-05.csv").write_text(_HDR + "2024-01-05,AKCYJNY SYNTH-B70,PLN,1,1\n", encoding="utf-8")
        spy = _SatSpy()
        result = _cycle(tmp_path, conn, monkeypatch, account_values_dir=av, satellite_fn=spy)
        assert [a.date.isoformat() for a in spy.snapshots] == ["2024-01-05", "2024-02-02"]
        assert result.state.satellite_error is False
    finally:
        conn.rollback()


@pytest.mark.db
@pytest.mark.parametrize("variant", ["pusty", "nieistniejacy"])
def test_t3_cycle_without_anchor_files_passes_empty_list_and_no_failure(db_conn, tmp_path, monkeypatch, variant):
    conn = db_conn
    try:
        _first_instrument(conn)
        av = tmp_path / "av"
        if variant == "pusty":
            av.mkdir()
        spy = _SatSpy()
        result = _cycle(tmp_path, conn, monkeypatch, account_values_dir=av, satellite_fn=spy)
        assert spy.snapshots == []
        assert not any("AccountValuesError" in f for f in result.state.import_failures)
        assert result.state.satellite_error is False
    finally:
        conn.rollback()


# ===========================================================================
# T4 (B-74) — "Zmiany pozycji" wzgledem poprzedniej oceny, przez run_cycle
# (`mannaz.cycle.open_positions_as_of` podmieniane; wymaga bazy: prev_date z risk_daily)
# ===========================================================================


def _op(ticker, instrument_id, qty, rachunek=ACC, ccy="PLN"):
    return {
        "rachunek": rachunek, "broker_ticker": ticker, "instrument_id": instrument_id,
        "settlement_currency": ccy, "qty": Decimal(qty),
    }


def _fake_open_positions(by_day: dict[date, list[dict]], default=()):
    calls: list[date] = []

    def fake(cur, day):
        calls.append(day)
        return list(by_day.get(day, default))

    fake.calls = calls  # type: ignore[attr-defined]
    return fake


@pytest.mark.db
def test_t4_cycle_changes_vs_previous_evaluation_and_between_evaluations(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        inst_id, _ = _first_instrument(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")
            _risk_marker(cur, inst_id, PREV)  # prev_date = 1990-03-05 (jedyny wiersz < D)
        before = [
            _op("BBB", 2, "5"),  # zamknieta do D
            _op("CCC", 3, "7"),  # ↑
            _op("AAA0", 9, "10"),  # bez zmiany
            _op("SSU", 5, "-8", ACC_KONTR),  # short, |qty| maleje -> ↓
        ]
        after = [
            _op("AAA", 1, "4"),  # nowa
            _op("CCC", 3, "9"),
            _op("AAA0", 9, "10"),
            _op("SSU", 5, "-5", ACC_KONTR),
        ]
        between = {
            date(1990, 3, 6): [_op("EEE", 20, "3"), _op("CCC", 3, "7")],  # EEE: otwarta i zamknieta MIEDZY
            date(1990, 3, 7): [_op("EEE", 20, "3"), _op("FFF", 21, "1")],
            date(1990, 3, 8): [_op("CCC", 3, "8"), _op("BBB", 2, "5")],  # znane klucze (B, C) nie sa "miedzy"
        }
        fake = _fake_open_positions({PREV: before, D: after, **between})
        monkeypatch.setattr("mannaz.cycle.open_positions_as_of", fake)

        result = _cycle(tmp_path, conn, monkeypatch)
        st = result.state
        assert st.position_changes_computed is True
        assert st.position_changes_prev_date == PREV
        got = {(c.broker_ticker, c.rodzaj) for c in st.position_changes}
        assert got == {
            ("AAA", "nowa"),
            ("BBB", "zamknięta"),
            ("CCC", "zmiana ilości ↑"),
            ("SSU", "zmiana ilości ↓"),
        }
        assert st.position_changes_between == ["EEE", "FFF"]
        # wolane dla prev_date, D i kazdego dnia kalendarzowego pomiedzy (ekskluzywnie)
        assert sorted(set(fake.calls)) == [PREV, date(1990, 3, 6), date(1990, 3, 7), date(1990, 3, 8), D]

        sec = _section(result.report_path.read_text(encoding="utf-8"), "## Zmiany pozycji")
        lines = [ln for ln in sec.split("\n") if ln.strip()]
        assert lines[0] == "względem poprzedniej oceny (1990-03-05)"
        assert "- AAA (AKCYJNY): nowa" in lines
        assert "- BBB (AKCYJNY): zamknięta" in lines
        assert "- CCC (AKCYJNY): zmiana ilości ↑" in lines
        assert "- SSU (KONTRAKTOWY): zmiana ilości ↓" in lines
        assert "otwarte i zamknięte między ocenami: EEE, FFF" in lines
        assert "SYNTH" not in sec
    finally:
        conn.rollback()


@pytest.mark.db
def test_t4_cycle_adjacent_evaluations_have_no_between_days(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        inst_id, _ = _first_instrument(conn)
        d_prev = date(1990, 3, 8)  # tuz przed D=03-09: zadnego dnia "pomiedzy"
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")
            _risk_marker(cur, inst_id, d_prev)
        fake = _fake_open_positions({d_prev: [_op("AAA", 1, "4")], D: [_op("AAA", 1, "4")]})
        monkeypatch.setattr("mannaz.cycle.open_positions_as_of", fake)
        result = _cycle(tmp_path, conn, monkeypatch)
        assert result.state.position_changes == []
        assert result.state.position_changes_between == []
        assert sorted(fake.calls) == [d_prev, D]
        sec = _section(result.report_path.read_text(encoding="utf-8"), "## Zmiany pozycji")
        assert "względem poprzedniej oceny (1990-03-08)" in sec
        assert "otwarte i zamknięte między ocenami: brak" in sec
    finally:
        conn.rollback()


@pytest.mark.db
def test_t4_cycle_no_previous_evaluation(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        _first_instrument(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")  # brak jakiejkolwiek poprzedniej oceny
        fake = _fake_open_positions({D: [_op("AAA", 1, "4")]})
        monkeypatch.setattr("mannaz.cycle.open_positions_as_of", fake)
        result = _cycle(tmp_path, conn, monkeypatch)
        assert result.state.position_changes_computed is True
        assert result.state.position_changes_prev_date is None
        assert result.state.position_changes == []
        assert fake.calls == []  # bez prev_date nic nie jest liczone
        sec = _section(result.report_path.read_text(encoding="utf-8"), "## Zmiany pozycji")
        assert "brak poprzedniej oceny" in sec
        assert "względem poprzedniej oceny" not in sec
    finally:
        conn.rollback()


@pytest.mark.db
def test_t4_cycle_stopped_before_d_says_section_not_computed(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        _first_instrument(conn)
        fake = _fake_open_positions({})
        monkeypatch.setattr("mannaz.cycle.open_positions_as_of", fake)
        result = _cycle(tmp_path, conn, monkeypatch, resolve_date_fn=lambda conn_, **k: None)
        assert result.state.d is None
        assert result.state.position_changes_computed is False
        assert fake.calls == []
        sec = _section(result.report_path.read_text(encoding="utf-8"), "## Zmiany pozycji")
        assert "nie wykonano (cykl zatrzymany przed ustaleniem D)" in sec
    finally:
        conn.rollback()


# ===========================================================================
# T5 (B-80) — NULL / inna stop_rule na prev_date => rule_changed
# ===========================================================================


def _risk_row(inst_id, *, stop_d, stop_prev_rule, below_stop=False, kind="long", ticker="B70AAA"):
    return PositionRiskRow(
        rachunek=ACC, instrument_id=inst_id, broker_ticker=ticker, settlement_currency="PLN", quote_currency="PLN",
        instrument_type="equity", is_core=False, qty=Decimal("10"), position_kind=kind, entry_price=Decimal("100"),
        close_d=Decimal("100"), atr20=Decimal("20"), atr22=Decimal("20"), sma200=None, stop_2n=None,
        stop_chandelier=None, stop_effective=stop_d, stop_source="chandelier", chandelier_hold=None,
        below_chandelier_hold=None, chandelier_from_entry=None, below_chandelier_from_entry=None, regime=None,
        warning=None, risk_native=Decimal("0"), below_stop=below_stop, fx_rate=Decimal("1"), fx_rate_date=D,
        risk_pln=Decimal("0"), multiplier_missing=False, price_is_stale=False, price_date_used=D,
        stop_rule=STOP_RULE_VERSION, stop_effective_prev_rule=stop_prev_rule,
    )


def _prev_risk_row(cur, inst_id, *, stop_effective, stop_rule, risk_state="NORMAL"):
    cur.execute(
        "INSERT INTO risk_daily (rachunek, instrument_id, risk_date, settlement_currency, stop_effective, "
        "stop_rule, risk_state) VALUES (%s, %s, %s, 'PLN', %s, %s, %s)",
        (ACC, inst_id, PREV, Decimal(stop_effective), stop_rule, risk_state),
    )


def _stop_decision(tmp_path, conn, monkeypatch, inst_id, *, prev_rule_in_db, stop_d, prev_rule_variant, below_stop=False):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM risk_daily")
        _prev_risk_row(cur, inst_id, stop_effective="100", stop_rule=prev_rule_in_db)
    summary = RiskSummary(risk_date=D, rows=[_risk_row(
        inst_id, stop_d=Decimal(stop_d), stop_prev_rule=Decimal(prev_rule_variant), below_stop=below_stop
    )])
    result = _cycle(tmp_path, conn, monkeypatch, risk_fn=lambda c, as_of, commit: summary)
    rows = [r for r in result.state.stop_order_rows if r.broker_ticker == "B70AAA"]
    assert len(rows) == 1, "stub risk_fn musi dac dokladnie jeden wiersz M62"
    return rows[0]


@pytest.mark.db
def test_t5_cycle_null_stop_rule_on_prev_date_is_a_changed_rule(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        inst_id, _ = _first_instrument(conn)
        # stop_d 90 < poprzedni 100 (zla strona); wariant starej reguly 95 > 90 => 'poluzowanie'
        row = _stop_decision(tmp_path, conn, monkeypatch, inst_id, prev_rule_in_db=None, stop_d="90", prev_rule_variant="95")
        assert row.rule_change == "poluzowanie"
        assert row.decision == f"tak (zmiana reguły rew. {STOP_RULE_VERSION})"
        assert row.decision == "tak (zmiana reguły rew. 4.19)"
    finally:
        conn.rollback()


@pytest.mark.db
def test_t5_cycle_other_stop_rule_version_on_prev_date_is_a_changed_rule(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        inst_id, _ = _first_instrument(conn)
        row = _stop_decision(tmp_path, conn, monkeypatch, inst_id, prev_rule_in_db="4.18", stop_d="90", prev_rule_variant="95")
        assert row.decision == "tak (zmiana reguły rew. 4.19)"
    finally:
        conn.rollback()


@pytest.mark.db
def test_t5_cycle_same_stop_rule_on_prev_date_keeps_ratchet(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        inst_id, _ = _first_instrument(conn)
        row = _stop_decision(
            tmp_path, conn, monkeypatch, inst_id, prev_rule_in_db=STOP_RULE_VERSION, stop_d="90", prev_rule_variant="95"
        )
        assert row.rule_change == "poluzowanie"  # kontrolka: kierunek ten sam, rozni sie TYLKO stop_rule z bazy
        assert row.decision == "nie (zapadka: zostaw poprzedni poziom)"
    finally:
        conn.rollback()


@pytest.mark.db
def test_t5_cycle_null_rule_but_tightening_or_zero_keeps_ratchet(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        inst_id, _ = _first_instrument(conn)
        tighter = _stop_decision(tmp_path, conn, monkeypatch, inst_id, prev_rule_in_db=None, stop_d="90", prev_rule_variant="80")
        assert tighter.rule_change == "zaostrzenie"
        assert tighter.decision == "nie (zapadka: zostaw poprzedni poziom)"
        zero = _stop_decision(tmp_path, conn, monkeypatch, inst_id, prev_rule_in_db=None, stop_d="90", prev_rule_variant="90")
        assert zero.rule_change == "0"
        assert zero.decision == "nie (zapadka: zostaw poprzedni poziom)"
    finally:
        conn.rollback()


@pytest.mark.db
def test_t5_cycle_high_state_wins_over_changed_rule(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        inst_id, _ = _first_instrument(conn)
        row = _stop_decision(
            tmp_path, conn, monkeypatch, inst_id, prev_rule_in_db=None, stop_d="90", prev_rule_variant="95", below_stop=True
        )
        assert row.decision == STOP_ORDER_HIGH_MESSAGE
    finally:
        conn.rollback()


@pytest.mark.db
def test_t5_cycle_no_prev_row_for_key_is_new_position_not_rule_change(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        inst_id, _ = _first_instrument(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM risk_daily")
            _risk_marker(cur, inst_id, PREV, "ZAGRANICZNY SYNTH-B70")  # prev_date istnieje, ale nie dla klucza z ACC
        summary = RiskSummary(risk_date=D, rows=[_risk_row(inst_id, stop_d=Decimal("90"), stop_prev_rule=Decimal("95"))])
        result = _cycle(tmp_path, conn, monkeypatch, risk_fn=lambda c, as_of, commit: summary)
        row = next(r for r in result.state.stop_order_rows if r.broker_ticker == "B70AAA")
        assert row.decision == "tak (nowa pozycja)"
    finally:
        conn.rollback()


# ===========================================================================
# T6 (B-81) — komunikat dostawcy w run_cycle: HEARTBEAT, nie DATA FAILURE
# ===========================================================================


@pytest.mark.db
def test_t6_cycle_provider_message_in_heartbeat_not_data_failure_same_exit_code(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    lg = logging.getLogger("yfinance")
    saved = (lg.propagate, lg.level, list(lg.handlers))
    try:
        _first_instrument(conn)

        def quiet_fetch(c):
            return PricesSummary()

        def noisy_fetch(c):
            logging.getLogger("yfinance").error("$SHO.WA: Data doesn't exist for startDate = 1, endDate = 2")
            return PricesSummary()

        baseline = _cycle(tmp_path, conn, monkeypatch, fetch_prices=quiet_fetch, reports_dir=tmp_path / "r0")
        noisy = _cycle(tmp_path, conn, monkeypatch, fetch_prices=noisy_fetch, reports_dir=tmp_path / "r1")

        assert "- komunikaty dostawcy: 0" in _section(baseline.report_path.read_text(encoding="utf-8"), "## 🟢 HEARTBEAT")
        text = noisy.report_path.read_text(encoding="utf-8")
        assert (
            "- komunikaty dostawcy: 1 (SHO.WA: Data doesn't exist for startDate = 1, endDate = 2)"
            in _section(text, "## 🟢 HEARTBEAT")
        )
        assert "SHO.WA" not in _section(text, "## 🔴 DATA FAILURE")
        assert noisy.exit_code == baseline.exit_code
        assert noisy.state.import_failures == baseline.state.import_failures
        assert (lg.propagate, lg.level, list(lg.handlers)) == saved  # logger przywrocony po cyklu
    finally:
        lg.propagate, lg.level = saved[0], saved[1]
        lg.handlers[:] = saved[2]
        conn.rollback()


@pytest.mark.db
def test_t6_cycle_provider_message_kept_when_prices_stage_raises(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    lg = logging.getLogger("yfinance")
    saved = (lg.propagate, lg.level, list(lg.handlers))
    try:
        _first_instrument(conn)

        def failing_fetch(c):
            logging.getLogger("yfinance").error("$SHO.WA: Data doesn't exist for startDate = 1, endDate = 2")
            raise RuntimeError("etap cen padl")

        result = _cycle(tmp_path, conn, monkeypatch, fetch_prices=failing_fetch)
        assert any(f.startswith("błąd etapu ceny/FX: RuntimeError") for f in result.state.import_failures)
        assert [m.symbol for m in result.state.provider_messages] == ["SHO.WA"]
        assert result.exit_code == 2  # kod 2 z powodu WYJATKU etapu, nie komunikatu
        assert (lg.propagate, lg.level, list(lg.handlers)) == saved
    finally:
        lg.propagate, lg.level = saved[0], saved[1]
        lg.handlers[:] = saved[2]
        conn.rollback()

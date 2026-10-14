"""Testy atrybucji M85 i migawki M86 (brief CC-B38A, krok C2; spec C1-C3, sekcje 2-6, 11).
WYLACZNIE dane syntetyczne, bez bazy i sieci. NAV budowany przez `nav_for_day`
(jedna sciezka wyceny, H1); wiersze KONTRAKTOWY jako slowniki z Decimal."""

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from mannaz import risk
from mannaz import satellite as sat
from mannaz.satellite import InstrumentInfo, PositionInput, TxRow

P = date(2024, 3, 1)
D = date(2024, 3, 4)
ACC = "AKCYJNY 111"
ZAG = "ZAGRANICZNY 222"
KON = "KONTRAKTOWY 333"


# --- pomocnicze ---------------------------------------------------------------


def _pos(iid, ticker, qty, price, qfx=1.0, qcur="PLN", cur="PLN", rach=ACC, settle_fx=None, status="ok"):
    return PositionInput(
        rachunek=rach, currency=cur, ticker=ticker, qty=qty, layer_factor=1.0, price=price,
        price_status=status, price_reason=None, quote_fx=qfx,
        settle_fx=settle_fx if settle_fx is not None else (1.0 if cur == "PLN" else qfx),
        is_core=False, instrument_id=iid, quote_currency=qcur,
    )


def _nav(positions=(), cash=None, fx=None, kon=None):
    return sat.nav_for_day(list(positions), cash or {}, fx or {}, kon)


def _fx_fn(rates=None):
    rates = rates or {}
    return lambda c, d: 1.0 if c == "PLN" else rates.get(c)


def _inst(iid, ticker, typ="equity", qcur="PLN", mult=None, yahoo=None, base=None, core=False):
    return InstrumentInfo(
        id=iid, ticker=ticker, instrument_type=typ, is_core=core, multiplier=mult,
        yahoo_symbol=yahoo, quote_currency=qcur, calendar_code=None, base_symbol=base,
    )


def _kon_row(d, row_type, amount, ticker=None, qty=None, price=None, mult=None):
    return {
        "transaction_date": d, "currency": "PLN", "row_type": row_type, "amount": Decimal(str(amount)),
        "qty": None if qty is None else Decimal(str(qty)), "price": None if price is None else Decimal(str(price)),
        "multiplier": None if mult is None else Decimal(str(mult)), "broker_ticker": ticker,
    }


def _attr(nav_p, nav_d, rows=(), kon_rows=(), instruments=None, rates=None):
    instruments = instruments or {}
    return sat.attribute_day(
        nav_p, nav_d, list(rows), list(kon_rows), _fx_fn(rates), lambda r: None,
        instruments, sat.build_yahoo_map(instruments),
    )


def _flow(rows, rates=None):
    return sat.external_flow_pln(list(rows), _fx_fn(rates)).value


def _kind(lines, kind):
    return [ln for ln in lines if ln.kind == kind]


def _assert_identity(lines, nav_p, nav_d, flow=0.0, tol=1e-9):
    assert lines is not None
    diff = sat.identity_diff(lines, nav_p.value, nav_d.value, flow)
    assert diff is not None and abs(diff) < tol
    expected = nav_d.value - nav_p.value - flow
    for key in ("account", "emitent", "currency"):  # wszystkie trzy przekroje
        cells = sat.aggregate_lines(lines, key)
        assert sum(c["total"] for c in cells.values()) == pytest.approx(expected, abs=tol)


# --- (a) pozycja PLN bez transakcji ---------------------------------------------


def test_a_pln_position_no_transactions():
    nav_p = _nav([_pos(1, "AAA", 100, 10.0)])
    nav_d = _nav([_pos(1, "AAA", 100, 11.0)])
    lines = _attr(nav_p, nav_d, instruments={1: _inst(1, "AAA")})
    _assert_identity(lines, nav_p, nav_d)
    (ln,) = _kind(lines, "pozycja")
    delta = nav_d.value - nav_p.value
    assert delta == pytest.approx(100.0)
    assert ln.fx == pytest.approx(0.0)
    assert ln.lokalny == pytest.approx(delta)
    assert ln.total == pytest.approx(delta)
    assert ln.transakcje_dnia == pytest.approx(0.0)
    assert ln.currency == "PLN"
    assert ln.account == "AKCYJNY"


# --- (b) pozycja w USD: interakcja dzielona po polowie ------------------------------


def test_b_usd_position_interaction_split_in_halves():
    nav_p = _nav([_pos(1, "USA", 10, 100.0, qfx=4.0, qcur="USD", cur="USD", rach=ZAG)], fx={"USD": 4.0})
    nav_d = _nav([_pos(1, "USA", 10, 110.0, qfx=4.2, qcur="USD", cur="USD", rach=ZAG)], fx={"USD": 4.2})
    lines = _attr(nav_p, nav_d, instruments={1: _inst(1, "USA", qcur="USD")}, rates={"USD": 4.2})
    _assert_identity(lines, nav_p, nav_d)
    (ln,) = _kind(lines, "pozycja")
    v_p, r_l, r_fx = 4000.0, 0.10, 0.05
    assert ln.lokalny + ln.fx == pytest.approx(v_p * ((1 + r_l) * (1 + r_fx) - 1))
    assert ln.lokalny == pytest.approx(v_p * (r_l + 0.5 * r_l * r_fx))
    assert ln.fx == pytest.approx(v_p * (r_fx + 0.5 * r_l * r_fx))
    assert ln.currency == "USD"
    assert ln.transakcje_dnia == pytest.approx(0.0)


# --- (c) kupno w dniu d po cenie != close ------------------------------------------------


def test_c_buy_at_price_below_close_commission_in_amount():
    # kupno 10 szt. po 10 USD, prowizja 5 USD w kwocie: amount = -(10*10 + 5); close d = 12; X = 4
    x = {"USD": 4.0}
    nav_p = _nav(cash={("ZAGRANICZNY", "USD"): 1000.0}, fx=x)
    nav_d = _nav([_pos(1, "USA", 10, 12.0, qfx=4.0, qcur="USD", cur="USD", rach=ZAG)],
                 cash={("ZAGRANICZNY", "USD"): 895.0}, fx=x)
    row = TxRow(D, ZAG, "USD", "kupno", -105.0, instrument_id=1, qty=10.0, price=10.0, instrument_type="equity")
    lines = _attr(nav_p, nav_d, [row], instruments={1: _inst(1, "USA", qcur="USD")}, rates=x)
    _assert_identity(lines, nav_p, nav_d, _flow([row], x))
    (ln,) = _kind(lines, "pozycja")
    assert ln.transakcje_dnia == pytest.approx((12.0 - 10.0) * 10 * 4.0 - 5.0 * 4.0)
    assert ln.fx == pytest.approx(0.0)
    assert ln.lokalny == pytest.approx(ln.transakcje_dnia)
    cash = _kind(lines, "gotowka")
    assert sum(c.total for c in cash) == pytest.approx(0.0)  # gotowka: ruch w pelni wyjasniony wierszem


def test_c_buy_with_instrument_linked_fee_row():
    # kupno -100 + osobny wiersz oplata_transakcyjna z instrument_id -5 -> ten sam wynik
    nav_p = _nav(cash={("AKCYJNY", "PLN"): 1000.0})
    nav_d = _nav([_pos(1, "AAA", 10, 12.0)], cash={("AKCYJNY", "PLN"): 895.0})
    rows = [
        TxRow(D, ACC, "PLN", "kupno", -100.0, instrument_id=1, qty=10.0, price=10.0),
        TxRow(D, ACC, "PLN", "oplata_transakcyjna", -5.0, instrument_id=1),
    ]
    lines = _attr(nav_p, nav_d, rows, instruments={1: _inst(1, "AAA")})
    _assert_identity(lines, nav_p, nav_d)
    (ln,) = _kind(lines, "pozycja")
    assert ln.transakcje_dnia == pytest.approx((12.0 - 10.0) * 10 - 5.0)
    assert not _kind(lines, "koszty_nieprzypisane")


# --- (d) sprzedaz do zera: pozycja zostaje w liniach i agregacji -------------------------------


def test_d_sell_to_zero_position_stays_in_lines_and_aggregation():
    inst = {1: _inst(1, "AAA")}
    cash0 = {("AKCYJNY", "PLN"): 500.0}
    nav0 = _nav([_pos(1, "AAA", 10, 10.0)], cash=cash0)
    nav1 = _nav([_pos(1, "AAA", 10, 11.0)], cash=cash0)
    sale = TxRow(D, ACC, "PLN", "sprzedaz", 120.0, instrument_id=1, qty=-10.0, price=12.0)
    nav2 = _nav(cash={("AKCYJNY", "PLN"): 620.0})
    lines1 = _attr(nav0, nav1, instruments=inst)
    lines2 = _attr(nav1, nav2, [sale], instruments=inst)
    _assert_identity(lines1, nav0, nav1)
    _assert_identity(lines2, nav1, nav2)
    (pos2,) = _kind(lines2, "pozycja")
    assert pos2.instrument == "AAA"
    assert pos2.total == pytest.approx(0.0 - 110.0 + 120.0)  # V(d)=0, wplyw 120
    # agregacja okna z dwoch dni obejmuje zamknieta pozycje
    cells = {}
    for ls in (lines1, lines2):
        sat._merge_cells(cells, sat.aggregate_lines(ls, "emitent"))
    assert "AAA" in cells
    assert cells["AAA"]["total"] == pytest.approx(10.0 + 10.0)
    rows = sat._cross_rows(cells, held_labels=set())
    aaa = next(r for r in rows if r["key"] == "AAA")
    assert aaa["closed"] is True
    rows_held = sat._cross_rows(cells, held_labels={"AAA"})
    assert next(r for r in rows_held if r["key"] == "AAA")["closed"] is False


# --- (e) dywidenda netto + podatek: w linii emitenta, nie w kosztach -----------------------------


def test_e_dividend_and_withholding_tax_land_in_emitent_line():
    nav_p = _nav([_pos(1, "AAA", 100, 10.0)], cash={("AKCYJNY", "PLN"): 0.0})
    nav_d = _nav([_pos(1, "AAA", 100, 10.0)], cash={("AKCYJNY", "PLN"): 40.5})
    rows = [
        TxRow(D, ACC, "PLN", "dywidenda_netto", 50.0, instrument_id=1),
        TxRow(D, ACC, "PLN", "podatek_dywidenda", -9.5, instrument_id=1),
    ]
    lines = _attr(nav_p, nav_d, rows, instruments={1: _inst(1, "AAA")})
    _assert_identity(lines, nav_p, nav_d)
    (ln,) = _kind(lines, "pozycja")
    assert ln.emitent == "AAA"
    assert ln.total == pytest.approx(40.5)
    assert ln.transakcje_dnia == pytest.approx(40.5)
    assert not _kind(lines, "oplaty")
    assert not _kind(lines, "koszty_nieprzypisane")


def test_e_cost_row_without_instrument_is_unassigned_never_dropped():
    nav_p = _nav(cash={("ZAGRANICZNY", "PLN"): 100.0})
    nav_d = _nav(cash={("ZAGRANICZNY", "PLN"): 98.0})
    row = TxRow(D, ZAG, "PLN", "oplata_transakcyjna", -2.0)
    lines = _attr(nav_p, nav_d, [row])
    _assert_identity(lines, nav_p, nav_d)
    (ln,) = _kind(lines, "koszty_nieprzypisane")
    assert ln.emitent == "koszty nieprzypisane ZAGRANICZNY"
    assert ln.total == pytest.approx(-2.0)
    assert ln.n_rows == 1


# --- (f) oplata_przechowanie bez instrumentu ---------------------------------------------------


def test_f_custody_fee_without_instrument_is_oplaty_line():
    nav_p = _nav(cash={("AKCYJNY", "PLN"): 100.0})
    nav_d = _nav(cash={("AKCYJNY", "PLN"): 97.0})
    row = TxRow(D, ACC, "PLN", "oplata_przechowanie", -3.0)
    lines = _attr(nav_p, nav_d, [row])
    _assert_identity(lines, nav_p, nav_d)
    (ln,) = _kind(lines, "oplaty")
    assert ln.emitent == "oplaty AKCYJNY"
    assert ln.total == pytest.approx(-3.0)
    assert not _kind(lines, "koszty_nieprzypisane")


# --- (g) gotowka EUR: tylko fx ----------------------------------------------------------------------


def test_g_eur_cash_revaluation_is_pure_fx():
    cash = {("ZAGRANICZNY", "EUR"): 1000.0}
    nav_p = _nav(cash=cash, fx={"EUR": 4.30})
    nav_d = _nav(cash=cash, fx={"EUR": 4.35})
    lines = _attr(nav_p, nav_d, rates={"EUR": 4.35})
    _assert_identity(lines, nav_p, nav_d)
    (ln,) = _kind(lines, "gotowka")
    assert ln.emitent == "gotowka ZAGRANICZNY/EUR"
    assert ln.lokalny == pytest.approx(0.0, abs=1e-9)
    assert ln.fx == pytest.approx(1000.0 * (4.35 - 4.30))
    assert ln.total == pytest.approx(ln.fx)


# --- (h) przelew wewnetrzny AKCYJNY -> KONTRAKTOWY ------------------------------------------------------


def test_h_internal_transfer_to_kontraktowy_nets_to_zero():
    k_p = float(risk.kontraktowy_account_value([_kon_row(date(2024, 2, 1), "przelew_do_domu_maklerskiego", 2000)], P))
    nav_p = _nav(cash={("AKCYJNY", "PLN"): 1000.0}, kon=k_p)
    krow = _kon_row(D, "przelew_wewnetrzny", 500)
    k_d = float(risk.kontraktowy_account_value(
        [_kon_row(date(2024, 2, 1), "przelew_do_domu_maklerskiego", 2000), krow], D))
    nav_d = _nav(cash={("AKCYJNY", "PLN"): 500.0}, kon=k_d)
    arow = TxRow(D, ACC, "PLN", "przelew_wewnetrzny", -500.0)
    lines = _attr(nav_p, nav_d, [arow], [krow])
    _assert_identity(lines, nav_p, nav_d, _flow([arow]))
    transfers = _kind(lines, "przelew")
    by_acc = sat.aggregate_lines(transfers, "account")
    assert by_acc["AKCYJNY"]["total"] == pytest.approx(-500.0)
    assert by_acc["KONTRAKTOWY"]["total"] == pytest.approx(500.0)
    assert sum(c["total"] for c in by_acc.values()) == pytest.approx(0.0)
    # kontrola A2: przelew wewnetrzny wylaczony z linii kontraktowych i z oczekiwanej zmiany
    k_lines = sum(ln.total for ln in lines if ln.account == "KONTRAKTOWY" and ln.kind != "przelew")
    assert k_lines == pytest.approx((nav_d.kontraktowy_pln - nav_p.kontraktowy_pln) - sat.kontraktowy_ext_int([krow]))


# --- (i) kontrola ujemna i dodatnia tozsamosci -------------------------------------------------------------


def _second_valuation(nav, factor=1.001):
    """Kopia NAV z druga (rozjechana) wycena pozycji; nav.value bez zmian."""
    pvs = {k: replace(pv, value_pln=pv.value_pln * factor) for k, pv in nav.position_values.items()}
    return replace(nav, position_values=pvs)


def test_i_identity_negative_and_positive_control():
    nav_p = _nav([_pos(1, "AAA", 10000, 10.0)], cash={("AKCYJNY", "PLN"): 1000.0})
    nav_d = _nav([_pos(1, "AAA", 10000, 10.5)], cash={("AKCYJNY", "PLN"): 1000.0})
    inst = {1: _inst(1, "AAA")}
    good = _attr(nav_p, nav_d, instruments=inst)
    diff_ok = sat.identity_diff(good, nav_p.value, nav_d.value, 0.0)
    assert abs(diff_ok) < 1e-9  # kontrolka dodatnia
    bad_nav = _second_valuation(nav_d)
    assert bad_nav.value == nav_d.value
    bad = _attr(nav_p, bad_nav, instruments=inst)
    diff_bad = sat.identity_diff(bad, nav_p.value, bad_nav.value, 0.0)
    assert abs(diff_bad) > 0.01  # kontrolka ujemna: druga wycena lamie tozsamosc
    assert diff_bad == pytest.approx(10000 * 10.5 * 0.001)


# --- (j) jeden emitent w dwoch walutach + kontrakt na baze; kontrakt bez bazy --------------------------------


def _instruments_j():
    return {
        1: _inst(1, "AAA", qcur="EUR", yahoo="AAA.DE"),
        2: _inst(2, "FAAAZ26", typ="future", mult=10.0, base="AAA.DE"),
        3: _inst(3, "FXXXZ26", typ="future", mult=10.0, base=None),
        4: _inst(4, "FYYYZ26", typ="future", mult=10.0, base="NIEMA.XX"),
    }


def test_j_one_emitent_across_currencies_and_futures():
    inst = _instruments_j()
    # ten sam instrument: AKCYJNY (rozl. PLN) i ZAGRANICZNY (rozl. EUR), notowany w EUR
    nav_p = _nav(
        [_pos(1, "AAA", 10, 100.0, qfx=4.30, qcur="EUR", cur="PLN", rach=ACC),
         _pos(1, "AAA", 5, 100.0, qfx=4.30, qcur="EUR", cur="EUR", rach=ZAG, settle_fx=4.30)],
        fx={"EUR": 4.30}, kon=1000.0)
    nav_d = _nav(
        [_pos(1, "AAA", 10, 102.0, qfx=4.35, qcur="EUR", cur="PLN", rach=ACC),
         _pos(1, "AAA", 5, 102.0, qfx=4.35, qcur="EUR", cur="EUR", rach=ZAG, settle_fx=4.35)],
        fx={"EUR": 4.35}, kon=1050.0)
    krows = [
        _kon_row(D, "depozyt_zwrot", 100, ticker="FAAAZ26"),
        _kon_row(D, "depozyt_doplata", -50, ticker="FXXXZ26"),
    ]
    lines = _attr(nav_p, nav_d, kon_rows=krows, instruments=inst, rates={"EUR": 4.35})
    _assert_identity(lines, nav_p, nav_d)
    aaa = [ln for ln in lines if ln.emitent == "AAA"]
    assert len(aaa) == 3
    assert {ln.kind for ln in aaa} == {"pozycja", "kontrakt"}
    assert {(ln.account, ln.currency) for ln in _kind(aaa, "pozycja")} == {("AKCYJNY", "EUR"), ("ZAGRANICZNY", "EUR")}
    assert all(ln.emitent_key == 1 for ln in aaa)
    cells = sat.aggregate_lines(lines, "emitent")
    assert [k for k in cells if k == "AAA"] == ["AAA"]  # dokladnie jeden klucz
    assert cells["AAA"]["total"] == pytest.approx(sum(ln.total for ln in aaa))
    assert cells["AAA"]["bez_bazy"] is False
    # klucz zdefiniowany tez w by_currency: kontrakt w PLN, pozycje w walucie notowania
    assert sat.aggregate_lines(aaa, "currency").keys() == {"EUR", "PLN"}


def test_j_future_without_base_is_its_own_emitent():
    inst = _instruments_j()
    ymap = sat.build_yahoo_map(inst)
    assert sat.is_future_no_base(3, inst, ymap) is True  # brak base_symbol
    assert sat.is_future_no_base(4, inst, ymap) is True  # baza nieznaleziona
    assert sat.is_future_no_base(2, inst, ymap) is False
    assert sat.emitent_key_for(3, inst, ymap) == (3, "FXXXZ26")
    assert sat.emitent_key_for(2, inst, ymap) == (1, "AAA")
    nav_p = _nav(kon=1000.0)
    nav_d = _nav(kon=950.0)
    krow = _kon_row(D, "depozyt_doplata", -50, ticker="FXXXZ26")
    lines = _attr(nav_p, nav_d, kon_rows=[krow], instruments=inst)
    _assert_identity(lines, nav_p, nav_d)
    (ln,) = _kind(lines, "kontrakt")
    assert ln.emitent == "FXXXZ26"
    assert ln.kontrakt_bez_bazy is True
    cell = sat.aggregate_lines(lines, "emitent")["FXXXZ26"]
    assert cell["bez_bazy"] is True
    assert cell["total"] == pytest.approx(-50.0)


# --- (k) kontrola A2 KONTRAKTOWY --------------------------------------------------------------------------------


def test_k_kontraktowy_ext_int_and_contract_lines_control():
    inst = {1: _inst(1, "AAA", yahoo="AAA.WA"), 2: _inst(2, "FAAAZ26", typ="future", mult=10.0, base="AAA.WA")}
    prior = [_kon_row(date(2024, 2, 1), "przelew_do_domu_maklerskiego", 500)]
    new = [
        _kon_row(D, "przelew_do_domu_maklerskiego", 1000),
        _kon_row(D, "przelew_wewnetrzny", 200),
        _kon_row(D, "depozyt_zwrot", 100, ticker="FAAAZ26"),
        # kupno: kwota = -(2*100*10 + 7) -> w wartosci KONTRAKTOWY tylko prowizja -7
        _kon_row(D, "kupno", -2007, ticker="FAAAZ26", qty=2, price=100, mult=10),
    ]
    assert sat.kontraktowy_ext_int(new) == pytest.approx(1000.0 + 200.0)
    k_p = float(risk.kontraktowy_account_value(prior, P))
    k_d = float(risk.kontraktowy_account_value(prior + new, D))
    assert k_d - k_p == pytest.approx(1000 + 200 + 100 - 7)
    nav_p = _nav(kon=k_p)
    nav_d = _nav(kon=k_d)
    lines = _attr(nav_p, nav_d, kon_rows=new, instruments=inst)
    k_lines = sum(ln.total for ln in lines if ln.account == "KONTRAKTOWY" and ln.kind != "przelew")
    assert k_lines == pytest.approx((k_d - k_p) - sat.kontraktowy_ext_int(new))
    assert k_lines == pytest.approx(100.0 - 7.0)
    (contract,) = _kind(lines, "kontrakt")
    assert contract.instrument == "FAAAZ26"
    (przelew,) = _kind(lines, "przelew")
    assert przelew.total == pytest.approx(200.0)
    # zewnetrzny przelew_do_domu nie jest linia (siedzi w F) -> tozsamosc zamyka sie z F = 1000
    _assert_identity(lines, nav_p, nav_d, flow=1000.0)


# --- (l) m86_snapshot ----------------------------------------------------------------------------------------------


def _risk_row(iid, ticker, typ, core, qty, close, mult, fx, pln, pct, theme, kind="long"):
    return {
        "risk_date": D, "account": "AKCYJNY", "instrument_id": iid, "settlement_currency": "PLN",
        "qty": Decimal(str(qty)), "close_d": Decimal(str(close)), "fx_rate": Decimal(str(fx)),
        "risk_pln": None if pln is None else Decimal(str(pln)),
        "risk_pct_satellite_capital": None if pct is None else Decimal(str(pct)),
        "name_key": str(iid), "position_kind": kind, "broker_ticker": ticker, "instrument_type": typ,
        "is_core": core, "multiplier": None if mult is None else Decimal(str(mult)), "theme": theme,
    }


def test_l_m86_snapshot_smoke():
    inst = {
        1: _inst(1, "EQ"), 2: _inst(2, "FUT", typ="future", mult=10.0, base=None),
        3: _inst(3, "ETF", typ="etf", core=True), 4: _inst(4, "EQ2"),
    }
    rows = [
        _risk_row(1, "EQ", "equity", False, 10, 100, None, 1, 500, 5.0, "T1"),
        _risk_row(2, "FUT", "future", False, 3, 200, 10, 1, 300, 3.0, "T2"),
        _risk_row(3, "ETF", "etf", True, 50, 20, None, 1, 900, 9.0, "T3"),
    ]
    nav_t = _nav(
        [_pos(1, "EQ", 10, 100.0), _pos(4, "EQ2", 30, 100.0)],
        cash={("AKCYJNY", "PLN"): 500.0}, kon=2000.0,
    )
    assert nav_t.value == pytest.approx(1000 + 3000 + 500 + 2000)
    m = sat.m86_snapshot(rows, nav_t, D, inst, sat.build_yahoo_map(inst))
    assert m["risk_date"] == D.isoformat() and m["T"] == D.isoformat()
    assert m["dates_differ"] is False
    # heat: bez core
    assert m["heat"]["n_eligible"] == 2
    assert m["heat"]["heat_pct"] == pytest.approx(8.0)
    assert m["heat"]["heat_pln"] == pytest.approx(800.0)
    assert m["heat"]["limit_pct"] == pytest.approx(float(risk.LEVEL3_PCT))
    assert m["heat"]["breach"] is False
    assert m["heat"]["incomplete"] == []
    themes = {t["theme"]: t for t in m["level2"]}
    assert set(themes) == {"T1", "T2"}  # core nie wchodzi do poziomu 2
    # kontrakty: nominal = qty * close * mnoznik * fx
    f = m["futures"]
    assert f["nominal_gross_pln"] == pytest.approx(3 * 200 * 10 * 1)
    assert f["nominal_net_pln"] == pytest.approx(3 * 200 * 10 * 1)
    assert f["gross_share_nav"] == pytest.approx(6000 / 6500)
    assert f["net_share_nav"] == pytest.approx(6000 / 6500)
    assert f["missing_multiplier"] == []
    assert m["kontraktowy_free_vs_deposit"]["status"] == "brak pomiaru"
    # koncentracja z NAV(T).position_values: wagi 0.25 / 0.75
    c = m["concentration"]
    assert c["long_equity_pln"] == pytest.approx(4000.0)
    assert c["n_emitents"] == 2
    assert c["hhi"] == pytest.approx(0.25 ** 2 + 0.75 ** 2)
    assert [t["emitent"] for t in c["top5"]] == ["EQ2", "EQ"]
    assert c["top5"][0]["weight"] == pytest.approx(0.75)
    assert c["cash_pln"] == pytest.approx(500.0)
    assert c["cash_share_nav"] == pytest.approx(500 / 6500)
    assert c["kontraktowy_pln"] == pytest.approx(2000.0)
    assert c["kontraktowy_share_nav"] == pytest.approx(2000 / 6500)
    assert c["futures_net_share_nav"] == pytest.approx(6000 / 6500)

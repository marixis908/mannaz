"""Testy czyste (bez bazy) dla cyklu tygodniowego — brief CC-C, C9.

Zakres: ciągłość (C2), bramka rejestracji (C4), diff pozycji (C3), zapadka
zleceń stop (C7 §3, M62), świeżość rynków T28 (C7), renderer raportu (C7).
Zero zależności od bazy/sieci — wyłącznie funkcje czyste z `mannaz.cycle`."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from mannaz.cycle import (
    CALENDAR_CODE_TO_EXCHANGE,
    STOP_ORDER_HIGH_MESSAGE,
    STOP_ORDER_MIN_CHANGE_ATR,
    ContinuityGap,
    Gap,
    MarketFreshnessResult,
    PositionChange,
    PositionSnapshotRow,
    ProcessedFileReportRow,
    RegistrationRow,
    ReportState,
    RiskReportAggregates,
    StopEvent,
    StopEventRow,
    StopOrderRow,
    account_label,
    build_stop_order_rows,
    check_file_continuity,
    diff_positions,
    filter_satellite_tickers,
    has_data_failures,
    is_satellite,
    market_freshness,
    prev_evaluation_maps,
    registration_gaps,
    render_report,
    stop_change_arrow,
    stop_events,
    stop_order_decision,
)
from mannaz.risk import IncompleteRiskItem

# ---------------------------------------------------------------------------
# C2 — ciągłość
# ---------------------------------------------------------------------------


def test_continuity_ok_when_file_starts_before_or_on_last_known_date():
    gaps = check_file_continuity(
        file_min_dates_by_rachunek={"AKCYJNY 000001": date(2026, 3, 1)},
        known_max_dates_by_rachunek={"AKCYJNY 000001": date(2026, 3, 5)},
    )
    assert gaps == []


def test_continuity_ok_when_file_starts_exactly_on_last_known_date():
    gaps = check_file_continuity(
        file_min_dates_by_rachunek={"AKCYJNY 000001": date(2026, 3, 5)},
        known_max_dates_by_rachunek={"AKCYJNY 000001": date(2026, 3, 5)},
    )
    assert gaps == []


def test_continuity_gap_when_file_starts_after_last_known_date():
    gaps = check_file_continuity(
        file_min_dates_by_rachunek={"AKCYJNY 000001": date(2026, 3, 10)},
        known_max_dates_by_rachunek={"AKCYJNY 000001": date(2026, 3, 5)},
    )
    assert gaps == [ContinuityGap("AKCYJNY 000001", date(2026, 3, 10), date(2026, 3, 5))]
    msg = gaps[0].as_data_failure()
    assert "dziura w historii" in msg
    # Z2 (brief CC-C fix): as_data_failure() maskuje numer rachunku — TYP
    # obecny, numer NIE.
    assert "AKCYJNY" in msg
    assert "000001" not in msg
    assert "2026-03-10" in msg
    assert "2026-03-05" in msg


def test_continuity_ok_when_rachunek_has_no_history_in_db():
    gaps = check_file_continuity(
        file_min_dates_by_rachunek={"NOWY 000009": date(2026, 3, 10)},
        known_max_dates_by_rachunek={},  # rachunek nieobecny w bazie -> OK
    )
    assert gaps == []


def test_continuity_checks_multiple_rachunki_independently():
    gaps = check_file_continuity(
        file_min_dates_by_rachunek={
            "AKCYJNY 000001": date(2026, 3, 1),  # OK
            "KONTRAKTOWY 000001": date(2026, 3, 20),  # dziura
        },
        known_max_dates_by_rachunek={
            "AKCYJNY 000001": date(2026, 3, 5),
            "KONTRAKTOWY 000001": date(2026, 3, 1),
        },
    )
    assert len(gaps) == 1
    assert gaps[0].rachunek == "KONTRAKTOWY 000001"


# ---------------------------------------------------------------------------
# C4 — bramka rejestracji
# ---------------------------------------------------------------------------


def test_registration_gaps_equity_missing_yahoo_symbol():
    row = RegistrationRow(
        rachunek="AKCYJNY 000001",
        broker_ticker="XYZ",
        instrument_type="equity",
        is_core=False,
        currency="USD",
        exchange="NASDAQ",
        theme="tech",
        yahoo_symbol=None,
    )
    gaps = registration_gaps([row])
    assert any(g.brak == "brak instruments.yahoo_symbol" for g in gaps)
    assert all(g.ticker == "XYZ" and g.rachunek == "AKCYJNY 000001" for g in gaps)


def test_registration_gaps_equity_complete_has_no_gaps():
    row = RegistrationRow(
        rachunek="AKCYJNY 000001",
        broker_ticker="XYZ",
        instrument_type="equity",
        is_core=False,
        currency="USD",
        exchange="NASDAQ",
        theme="tech",
        yahoo_symbol="XYZ",
    )
    assert registration_gaps([row]) == []


def test_registration_gaps_future_missing_multiplier_source_fcdrh27():
    """Seria FCDRH27 (syntetyczna, przykład ze speca C4) — mnożnik znany, ale
    bez `multiplier_source` -> nadal brak (I5/K4b wymaga obu)."""
    row = RegistrationRow(
        rachunek="KONTRAKTOWY 000001",
        broker_ticker="FCDRH27",
        instrument_type="future",
        is_core=False,
        theme="banki",
        base_symbol="CDR.WA",
        base_instrument_found=True,
        base_currency="PLN",
        base_exchange="GPW",
        multiplier=Decimal(100),
        multiplier_source=None,
    )
    gaps = registration_gaps([row])
    assert any(g.brak == "brak instruments.multiplier / multiplier_source" for g in gaps)
    assert not any("base_symbol" in g.brak for g in gaps)


def test_registration_gaps_future_complete_has_no_gaps():
    row = RegistrationRow(
        rachunek="KONTRAKTOWY 000001",
        broker_ticker="FCDRZ26",
        instrument_type="future",
        is_core=False,
        theme="banki",
        base_symbol="CDR.WA",
        base_instrument_found=True,
        base_currency="PLN",
        base_exchange="GPW",
        multiplier=Decimal(100),
        multiplier_source="GPW STD",
    )
    assert registration_gaps([row]) == []


def test_registration_gaps_future_missing_base_instrument():
    row = RegistrationRow(
        rachunek="KONTRAKTOWY 000001",
        broker_ticker="FXYZH27",
        instrument_type="future",
        is_core=False,
        theme="x",
        base_symbol="XYZ.WA",
        base_instrument_found=False,
        multiplier=Decimal(100),
        multiplier_source="GPW STD",
    )
    gaps = registration_gaps([row])
    assert any("instrumentu bazowego" in g.brak for g in gaps)


def test_registration_gaps_certificate_always_flagged():
    row = RegistrationRow(
        rachunek="AKCYJNY 000001",
        broker_ticker="INTLW2087767",
        instrument_type="certificate",
        is_core=False,
    )
    gaps = registration_gaps([row])
    assert len(gaps) == 1
    assert gaps[0].brak == "typ instrumentu nieobsługiwany przez ryzyko"


def test_registration_gaps_unknown_type_always_flagged():
    row = RegistrationRow(
        rachunek="AKCYJNY 000001",
        broker_ticker="ABC",
        instrument_type="unknown",
        is_core=False,
    )
    gaps = registration_gaps([row])
    assert len(gaps) == 1
    assert gaps[0].brak == "typ instrumentu nieobsługiwany przez ryzyko"


def test_registration_gaps_core_position_never_flagged():
    """Pozycje core (is_core=True) nie są satelitą -> nigdy w bramce, nawet
    bez yahoo_symbol."""
    row = RegistrationRow(
        rachunek="AKCYJNY 000001",
        broker_ticker="SPYI",
        instrument_type="equity",
        is_core=True,
        yahoo_symbol=None,
        currency=None,
        exchange=None,
        theme=None,
    )
    assert registration_gaps([row]) == []


def test_registration_gaps_missing_archetype_is_never_a_gap():
    """Brief CC-C, C4: brak wpisu w archetype_assignments NIE blokuje —
    `RegistrationRow` celowo nie ma pola archetypu, więc `registration_gaps`
    nie może go w ogóle zgłosić jako brak."""
    assert not hasattr(RegistrationRow, "archetype")
    row = RegistrationRow(
        rachunek="AKCYJNY 000001",
        broker_ticker="XYZ",
        instrument_type="equity",
        is_core=False,
        currency="USD",
        exchange="NASDAQ",
        theme="tech",
        yahoo_symbol="XYZ",
    )
    assert registration_gaps([row]) == []


# ---------------------------------------------------------------------------
# P4 (poprawka po przeglądzie nadzorcy) — "Do rejestracji (§12): brak
# archetypu" tylko dla satelity, ta sama definicja co bramka (`is_satellite`).
# ---------------------------------------------------------------------------


def test_is_satellite_matches_registration_gate_definition():
    # equity/etf spoza core -> satelita
    assert is_satellite("equity", False) is True
    assert is_satellite("etf", False) is True
    # future -> zawsze satelita (is_risk_budget_eligible)
    assert is_satellite("future", False) is True
    assert is_satellite("future", True) is True
    # core equity/etf -> NIE satelita
    assert is_satellite("equity", True) is False
    # certificate/unknown nie-core -> satelita (zawsze braki, C4)
    assert is_satellite("certificate", False) is True
    assert is_satellite("unknown", False) is True
    # certificate/unknown core (teoretycznie) -> nie-satelita
    assert is_satellite("certificate", True) is False


def test_filter_satellite_tickers_excludes_core():
    rows = [
        ("SPYI", "equity", True),  # core -> wykluczony
        ("XYZ", "equity", False),  # satelita -> zostaje
        ("FCDRZ26", "future", False),  # satelita (future) -> zostaje
    ]
    result = filter_satellite_tickers(rows)
    assert "SPYI" not in result
    assert "XYZ" in result
    assert "FCDRZ26" in result


def test_filter_satellite_tickers_excludes_core_even_for_certificate_type():
    rows = [("WEIRD", "certificate", True)]
    assert filter_satellite_tickers(rows) == []


def test_filter_satellite_tickers_empty_input():
    assert filter_satellite_tickers([]) == []


# ---------------------------------------------------------------------------
# C3 — diff pozycji
# ---------------------------------------------------------------------------


def test_diff_positions_new_closed_and_quantity_changes():
    before = [
        PositionSnapshotRow("AKCYJNY 000001", "AAA", 1, "USD", Decimal(10)),
        PositionSnapshotRow("AKCYJNY 000001", "BBB", 2, "USD", Decimal(5)),
        PositionSnapshotRow("AKCYJNY 000001", "CCC", 3, "USD", Decimal(7)),
    ]
    after = [
        PositionSnapshotRow("AKCYJNY 000001", "BBB", 2, "USD", Decimal(8)),  # ↑
        PositionSnapshotRow("AKCYJNY 000001", "CCC", 3, "USD", Decimal(2)),  # ↓
        PositionSnapshotRow("AKCYJNY 000001", "DDD", 4, "USD", Decimal(3)),  # nowa
        # AAA zamknięta (brak w after)
    ]
    changes = diff_positions(before, after)
    by_ticker = {c.broker_ticker: c.rodzaj for c in changes}
    assert by_ticker["AAA"] == "zamknięta"
    assert by_ticker["BBB"] == "zmiana ilości ↑"
    assert by_ticker["CCC"] == "zmiana ilości ↓"
    assert by_ticker["DDD"] == "nowa"


def test_diff_positions_unchanged_quantity_not_reported():
    before = [PositionSnapshotRow("AKCYJNY 000001", "AAA", 1, "USD", Decimal(10))]
    after = [PositionSnapshotRow("AKCYJNY 000001", "AAA", 1, "USD", Decimal(10))]
    assert diff_positions(before, after) == []


def test_diff_positions_sign_flip_short_counts_as_magnitude_change():
    # KONTRAKTOWY: long 10 -> short 10 to zmiana kierunku, ale |qty| taka sama
    before = [PositionSnapshotRow("KONTRAKTOWY 000001", "FCDRZ26", 1, "PLN", Decimal(10))]
    after = [PositionSnapshotRow("KONTRAKTOWY 000001", "FCDRZ26", 1, "PLN", Decimal(-10))]
    assert diff_positions(before, after) == []


# ---------------------------------------------------------------------------
# C7 §3 (M62) — zapadka zlecenia stop; poprawki po przeglądzie nadzorcy:
# P1 (stan HIGH nadpisuje decyzję), P3 (próg 0,25×ATR22 na ruchu w dobrą stronę)
# ---------------------------------------------------------------------------


def test_stop_order_decision_new_position_no_prev():
    assert stop_order_decision("long", Decimal("100"), None) == "tak (nowa pozycja)"
    assert stop_order_decision("short", Decimal("100"), None) == "tak (nowa pozycja)"


def test_stop_order_decision_long_good_direction_above_threshold_is_tak():
    # zmiana 10 >= 0,25*20=5 -> tak
    assert stop_order_decision("long", Decimal("110"), Decimal("100"), atr22_d=Decimal("20")) == "tak"


def test_stop_order_min_change_atr_constant_is_quarter():
    assert STOP_ORDER_MIN_CHANGE_ATR == Decimal("0.25")


def test_stop_order_decision_long_good_direction_exactly_at_threshold_is_tak():
    # zmiana 5 == 0,25*20=5 (próg jest >=) -> tak
    threshold = STOP_ORDER_MIN_CHANGE_ATR * Decimal("20")
    assert threshold == Decimal("5.00")
    assert stop_order_decision("long", Decimal("105"), Decimal("100"), atr22_d=Decimal("20")) == "tak"


def test_stop_order_decision_long_good_direction_below_threshold_is_nie():
    # zmiana 4 < 0,25*20=5 -> nie (zmiana < 0,25xATR22)
    result = stop_order_decision("long", Decimal("104"), Decimal("100"), atr22_d=Decimal("20"))
    assert result == "nie (zmiana < 0,25×ATR22)"


def test_stop_order_decision_good_direction_no_atr22_is_tak_without_threshold():
    result = stop_order_decision("long", Decimal("101"), Decimal("100"), atr22_d=None)
    assert result == "tak (brak ATR22 — bez progu)"


def test_stop_order_decision_long_wrong_direction_keeps_previous():
    assert (
        stop_order_decision("long", Decimal("90"), Decimal("100"), atr22_d=Decimal("20"))
        == "nie (zapadka: zostaw poprzedni poziom)"
    )


def test_stop_order_decision_long_unchanged():
    assert stop_order_decision("long", Decimal("100"), Decimal("100"), atr22_d=Decimal("20")) == "nie"


def test_stop_order_decision_short_good_direction_above_threshold_is_tak():
    # short: dobra strona = w dół; zmiana 10 >= 0,25*20=5 -> tak
    assert stop_order_decision("short", Decimal("90"), Decimal("100"), atr22_d=Decimal("20")) == "tak"


def test_stop_order_decision_short_good_direction_below_threshold_is_nie():
    result = stop_order_decision("short", Decimal("97"), Decimal("100"), atr22_d=Decimal("20"))
    assert result == "nie (zmiana < 0,25×ATR22)"


def test_stop_order_decision_short_wrong_direction_keeps_previous():
    assert (
        stop_order_decision("short", Decimal("110"), Decimal("100"), atr22_d=Decimal("20"))
        == "nie (zapadka: zostaw poprzedni poziom)"
    )


def test_stop_order_decision_short_unchanged():
    assert stop_order_decision("short", Decimal("100"), Decimal("100"), atr22_d=Decimal("20")) == "nie"


def test_stop_order_decision_high_state_overrides_everything():
    # below_stop=True -> komunikat HIGH, niezależnie od kierunku/progu/nowości.
    assert (
        stop_order_decision("long", Decimal("90"), Decimal("100"), atr22_d=Decimal("20"), below_stop=True)
        == STOP_ORDER_HIGH_MESSAGE
    )
    assert (
        stop_order_decision("long", Decimal("110"), None, atr22_d=Decimal("20"), below_stop=True)
        == STOP_ORDER_HIGH_MESSAGE
    )


def test_build_stop_order_rows_sorts_high_first_then_tak():
    items = [
        ("AAA", "AKCYJNY 000001", "long", Decimal("90"), "USD", "USD", Decimal("100"), None, Decimal("20"), False),  # nie (zapadka)
        ("BBB", "AKCYJNY 000001", "long", Decimal("110"), "USD", "USD", Decimal("100"), None, Decimal("20"), False),  # tak
        ("CCC", "KONTRAKTOWY 000001", "long", Decimal("50"), "PLN", "PLN", None, "CDR.WA", None, False),  # tak (nowa pozycja), kontrakt
        ("DDD", "AKCYJNY 000001", "long", Decimal("90"), "USD", "USD", Decimal("100"), None, Decimal("20"), True),  # HIGH
    ]
    rows = build_stop_order_rows(items)
    decisions = [r.decision for r in rows]
    assert decisions[0] == STOP_ORDER_HIGH_MESSAGE  # HIGH zawsze pierwsze
    assert decisions[1].startswith("tak")
    assert decisions[2].startswith("tak")
    assert decisions[3].startswith("nie")

    ddd = next(r for r in rows if r.broker_ticker == "DDD")
    assert ddd.state == "HIGH"
    ccc = next(r for r in rows if r.broker_ticker == "CCC")
    assert ccc.state == "NORMAL"
    assert ccc.base_symbol_label == "CDR.WA"
    assert ccc.atr22_threshold is None
    aaa = next(r for r in rows if r.broker_ticker == "AAA")
    assert aaa.base_symbol_label is None
    assert aaa.atr22_threshold == Decimal("5.00")


def test_stop_change_arrow_up_down_unchanged_and_no_prev():
    assert stop_change_arrow(Decimal("110"), Decimal("100")) == "↑"
    assert stop_change_arrow(Decimal("90"), Decimal("100")) == "↓"
    assert stop_change_arrow(Decimal("100"), Decimal("100")) == "bez zmiany"
    assert stop_change_arrow(Decimal("100"), None) == "brak"


# ---------------------------------------------------------------------------
# C7 §2 — zdarzenie przecięcia stopu (M69 EVENT; CC-U V3)
# ---------------------------------------------------------------------------


def _stop_event_row(
    ticker="XYZ",
    rachunek="AKCYJNY 000001",
    instrument_id=1,
    instrument_type="equity",
    is_core=False,
    below_stop=True,
    currency="USD",
    settlement_currency="USD",
    stop_effective=Decimal("100"),
) -> StopEventRow:
    return StopEventRow(
        broker_ticker=ticker,
        rachunek=rachunek,
        instrument_id=instrument_id,
        instrument_type=instrument_type,
        is_core=is_core,
        below_stop=below_stop,
        price_date_used=date(2026, 9, 25),
        close_d=Decimal("90"),
        stop_effective=stop_effective,
        stop_source="chandelier",
        price_source_symbol="XYZ",
        currency=currency,
        settlement_currency=settlement_currency,
    )


def test_stop_events_new_breach_no_prev_row_is_an_event():
    row = _stop_event_row()
    events = stop_events([row], prev_risk_state_by_key={})
    assert len(events) == 1
    assert events[0].broker_ticker == "XYZ"


def test_stop_events_already_high_on_prev_is_not_an_event():
    row = _stop_event_row()
    events = stop_events([row], prev_risk_state_by_key={("AKCYJNY 000001", 1, "USD"): "HIGH"})
    assert events == []


def test_stop_events_prev_normal_is_a_new_event():
    row = _stop_event_row()
    events = stop_events([row], prev_risk_state_by_key={("AKCYJNY 000001", 1, "USD"): "NORMAL"})
    assert len(events) == 1


def test_stop_events_not_below_stop_is_never_an_event():
    row = _stop_event_row(below_stop=False)
    assert stop_events([row], prev_risk_state_by_key={}) == []
    row_none = _stop_event_row(below_stop=None)
    assert stop_events([row_none], prev_risk_state_by_key={}) == []


def test_stop_events_core_position_always_skipped():
    row = _stop_event_row(ticker="SPYI", is_core=True)
    assert stop_events([row], prev_risk_state_by_key={}) == []


def test_stop_events_certificate_type_skipped_even_if_somehow_present():
    row = _stop_event_row(ticker="CERT", instrument_type="certificate")
    assert stop_events([row], prev_risk_state_by_key={}) == []


# ---------------------------------------------------------------------------
# Z1a (brief CC-C fix) — klucz poprzedniej oceny rozszerzony o
# settlement_currency (diagnoza sesji głównej: `risk_daily` UNIQUE (rachunek,
# instrument_id, settlement_currency, risk_date) — bez waluty rozliczenia w
# kluczu dwie pozycje tej samej spółki w dwóch walutach rozliczenia (np. GRAB
# EUR/USD, META EUR/USD) nadpisywały się w słownikach poprzedniej oceny).
# ---------------------------------------------------------------------------


def _prev_evaluation_row_old_key(
    rachunek: str, instrument_id: int, stop_effective: Decimal | None, risk_state: str | None
) -> tuple[str, int, Decimal | None, str | None]:
    """Pomocnik testowy: odtwarza STARE (usterkowe) budowanie słownika —
    klucz (rachunek, instrument_id), BEZ settlement_currency."""
    return (rachunek, instrument_id, stop_effective, risk_state)


def _old_prev_evaluation_maps(
    rows: list[tuple[str, int, Decimal | None, str | None]],
) -> tuple[dict[tuple[str, int], Decimal], dict[tuple[str, int], str | None]]:
    """Pomocnik testowy: odtwarza STARY (usterkowy) sposób budowania
    słowników poprzedniej oceny — klucz (rachunek, instrument_id), tak jak
    `run_cycle` robił to PRZED Z1a. Używany WYŁĄCZNIE jako kontrolka
    dodatnia — dowodzi, że stary klucz faktycznie nadpisuje wiersze."""
    prev_stops: dict[tuple[str, int], Decimal] = {}
    prev_risk_state: dict[tuple[str, int], str | None] = {}
    for rachunek, instrument_id, stop_effective, risk_state in rows:
        key = (rachunek, instrument_id)
        if stop_effective is not None:
            prev_stops[key] = stop_effective
        prev_risk_state[key] = risk_state
    return prev_stops, prev_risk_state


def test_prev_evaluation_maps_keeps_eur_and_usd_rows_separate():
    """a) GRAB (rachunek/instrument_id wspólne), EUR i USD, RÓŻNE stopy i
    RÓŻNE risk_state na poprzedniej ocenie -> `prev_evaluation_maps` z kluczem
    rozszerzonym o settlement_currency NIE nadpisuje jednego wiersza drugim."""
    rows = [
        ("AKCYJNY 000001", 42, "EUR", Decimal("3.7267"), "HIGH"),
        ("AKCYJNY 000001", 42, "USD", Decimal("3.36"), "NORMAL"),
    ]
    prev_stops, prev_risk_state = prev_evaluation_maps(rows)
    assert prev_stops[("AKCYJNY 000001", 42, "EUR")] == Decimal("3.7267")
    assert prev_stops[("AKCYJNY 000001", 42, "USD")] == Decimal("3.36")
    assert prev_risk_state[("AKCYJNY 000001", 42, "EUR")] == "HIGH"
    assert prev_risk_state[("AKCYJNY 000001", 42, "USD")] == "NORMAL"


def test_prev_evaluation_maps_omits_null_stop_but_keeps_state():
    rows = [("AKCYJNY 000001", 42, "EUR", None, "NORMAL")]
    prev_stops, prev_risk_state = prev_evaluation_maps(rows)
    assert ("AKCYJNY 000001", 42, "EUR") not in prev_stops
    assert prev_risk_state[("AKCYJNY 000001", 42, "EUR")] == "NORMAL"


def test_z1a_grab_eur_usd_each_row_keeps_its_own_prev_stop_and_direction():
    """a) Scenariusz z diagnozy sesji głównej: GRAB EUR i GRAB USD, ten sam
    rachunek i instrument_id, oba below_stop=True na D, ale RÓŻNE stopy na D
    i na poprzedniej ocenie, i RÓŻNE stany na poprzedniej ocenie (EUR: prev
    HIGH; USD: prev NORMAL). Z kluczem rozszerzonym o settlement_currency:
    - `stop_events`: zdarzenie tylko dla USD (EUR była HIGH już poprzednio);
    - budowa wierszy M62 (`prev_stops.get`) daje każdemu wierszowi WŁASNY
      stop poprzedni -> własny kierunek zmiany (nie 3,36 dla EUR)."""
    rachunek, instrument_id = "AKCYJNY 000001", 42
    prev_rows = [
        (rachunek, instrument_id, "EUR", Decimal("3.7267"), "HIGH"),
        (rachunek, instrument_id, "USD", Decimal("3.36"), "NORMAL"),
    ]
    prev_stops, prev_risk_state = prev_evaluation_maps(prev_rows)

    eur_row = _stop_event_row(
        ticker="GRAB",
        rachunek=rachunek,
        instrument_id=instrument_id,
        below_stop=True,
        currency="EUR",
        settlement_currency="EUR",
        stop_effective=Decimal("3.7267"),
    )
    usd_row = _stop_event_row(
        ticker="GRAB",
        rachunek=rachunek,
        instrument_id=instrument_id,
        below_stop=True,
        currency="USD",
        settlement_currency="USD",
        stop_effective=Decimal("3.36"),
    )

    events = stop_events([eur_row, usd_row], prev_risk_state)
    assert len(events) == 1, "EUR byla HIGH juz na poprzedniej ocenie -> nie generuje nowego zdarzenia"
    assert events[0].settlement_currency == "USD"

    # M62: każdy wiersz dostaje WŁASNY stop poprzedni (nie 3,36 dla EUR).
    eur_stop_prev = prev_stops.get((rachunek, instrument_id, "EUR"))
    usd_stop_prev = prev_stops.get((rachunek, instrument_id, "USD"))
    assert eur_stop_prev == Decimal("3.7267")
    assert usd_stop_prev == Decimal("3.36")
    assert eur_stop_prev != usd_stop_prev

    eur_stop_d = Decimal("3.7267")  # stop na D — stały, bez zmiany
    assert stop_change_arrow(eur_stop_d, eur_stop_prev) == "bez zmiany"


def test_z1a_control_old_key_without_currency_overwrites_rows():
    """b) Kontrolka dodatnia: identyczna sytuacja zbudowana STARYM kluczem
    (rachunek, instrument_id), bez settlement_currency, jak `run_cycle` robił
    to PRZED Z1a — wiersz EUR dostaje wartość USD (nadpisanie), dowodząc, że
    stary klucz faktycznie gubi/dubluje dane."""
    rachunek, instrument_id = "AKCYJNY 000001", 42
    old_rows = [
        _prev_evaluation_row_old_key(rachunek, instrument_id, Decimal("3.7267"), "HIGH"),  # EUR wpisany pierwszy
        _prev_evaluation_row_old_key(rachunek, instrument_id, Decimal("3.36"), "NORMAL"),  # USD nadpisuje EUR
    ]
    old_prev_stops, old_prev_risk_state = _old_prev_evaluation_maps(old_rows)

    # Usterka: jeden klucz (rachunek, instrument_id) -> tylko OSTATNI wiersz
    # (USD) przetrwał, wartość EUR (3,7267 / HIGH) zniknęła.
    assert old_prev_stops[(rachunek, instrument_id)] == Decimal("3.36")
    assert old_prev_stops[(rachunek, instrument_id)] != Decimal("3.7267")
    assert old_prev_risk_state[(rachunek, instrument_id)] == "NORMAL"
    assert len(old_prev_stops) == 1, "stary klucz miesza EUR i USD w jeden wpis (usterka z diagnozy)"

    # ... podczas gdy poprawny klucz (Z1a) zachowuje OBA wiersze osobno.
    correct_rows = [
        (rachunek, instrument_id, "EUR", Decimal("3.7267"), "HIGH"),
        (rachunek, instrument_id, "USD", Decimal("3.36"), "NORMAL"),
    ]
    correct_prev_stops, _ = prev_evaluation_maps(correct_rows)
    assert len(correct_prev_stops) == 2
    assert correct_prev_stops[(rachunek, instrument_id, "EUR")] == Decimal("3.7267")


# ---------------------------------------------------------------------------
# T28 — świeżość rynków
# ---------------------------------------------------------------------------


def _sessions_fn_daily(calendar_code: str, start: date, end: date) -> list[date]:
    """Stub kalendarza: sesja KAŻDEGO dnia (uproszczenie do testów) — pozwala
    kontrolować dokładnie liczbę sesji między dwiema datami."""
    if start > end:
        return []
    out = []
    d = start
    while d <= end:
        out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_market_freshness_three_sessions_behind_is_data_failure():
    today = date(2026, 9, 27)
    price_dates = {"XWAR": [date(2026, 9, 22)]}  # 4 dni kalendarzowe = 4 sesje stub przed today -> age=4
    results = market_freshness(price_dates, _sessions_fn_daily, today)
    assert len(results) == 1
    assert results[0].age_sessions >= 3
    assert results[0].is_data_failure is True


def test_market_freshness_zero_to_two_sessions_behind_is_ok():
    today = date(2026, 9, 27)
    price_dates = {"XWAR": [date(2026, 9, 26)]}  # wczoraj -> 0 sesji pomiędzy
    results = market_freshness(price_dates, _sessions_fn_daily, today)
    assert results[0].age_sessions == 0
    assert results[0].is_data_failure is False


def test_market_freshness_exactly_two_sessions_behind_is_ok():
    today = date(2026, 9, 27)
    # freshest = 24.09, today = 27.09 -> sesje w (24.09, 26.09] wg stub = 25.09, 26.09 = 2
    price_dates = {"XWAR": [date(2026, 9, 24)]}
    results = market_freshness(price_dates, _sessions_fn_daily, today)
    assert results[0].age_sessions == 2
    assert results[0].is_data_failure is False


def test_market_freshness_multiple_markets_independent():
    today = date(2026, 9, 27)
    price_dates = {
        "XWAR": [date(2026, 9, 26)],  # ok
        "XNAS": [date(2026, 9, 20)],  # stary -> DATA FAILURE
    }
    results = {r.calendar_code: r for r in market_freshness(price_dates, _sessions_fn_daily, today)}
    assert results["XWAR"].is_data_failure is False
    assert results["XNAS"].is_data_failure is True


# ---------------------------------------------------------------------------
# C7 — render_report
# ---------------------------------------------------------------------------

EXPECTED_HEADERS = [
    "## 🔴 DATA FAILURE",
    "## 🔴 STOP",
    "## Zlecenia stop do aktualizacji u brokera (M62)",
    "## Ryzyko",
    "## Zmiany pozycji",
    "## 🟢 HEARTBEAT",
]


def _complete_state() -> ReportState:
    state = ReportState(
        d=date(2026, 9, 25),
        run_started_at=datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc),
    )
    state.risk_aggregates = RiskReportAggregates(
        heat_pct_total=Decimal("2.5"),
        heat_pct_zagraniczny=Decimal("1.1"),
        level1_max_ticker="AAA",
        level1_max_pct=Decimal("0.8"),
        level1_breach_tickers=[],
        theme_ranking=[("tech", Decimal("2.0"), False)],
        level3_breach=False,
        capital_satelite_positions_pln=Decimal("10000"),
        kontraktowy_account_value_pln=Decimal("5000"),
        capital_satelite_pln=Decimal("15000"),
        stale_tickers=[],
    )
    state.heartbeat_total = 3
    state.heartbeat_covered = 3
    state.exit_code = 0
    return state


def test_render_report_complete_state_has_all_headers_in_order_and_brak_and_exit_0():
    state = _complete_state()
    text = render_report(state)

    positions = [text.index(h) for h in EXPECTED_HEADERS]
    assert positions == sorted(positions), "nagłówki muszą wystąpić w stałej kolejności"

    assert "brak" in text  # sekcje DATA FAILURE bez braków renderują "brak"
    assert not has_data_failures(state)
    assert "Kod wyjścia: 0" in text


def test_render_report_with_gaps_has_exit_code_2():
    state = _complete_state()
    state.registration_queue = [Gap("XYZ", "AKCYJNY 000001", "brak instruments.yahoo_symbol", "gdzie")]
    state.exit_code = 2 if has_data_failures(state) else 0

    assert has_data_failures(state)
    assert state.exit_code == 2

    text = render_report(state)
    assert "Kod wyjścia: 2" in text
    assert "XYZ" in text


def test_render_report_with_freshness_failure_counts_as_data_failure():
    state = _complete_state()
    state.freshness_results = [MarketFreshnessResult("XWAR", date(2026, 9, 20), 5, True)]
    assert has_data_failures(state)


def test_render_report_with_date_incomplete_items_counts_as_data_failure():
    state = _complete_state()
    state.date_incomplete_items = [IncompleteRiskItem("XYZ", "XYZ", "NASDAQ", "market_open_no_price")]
    assert has_data_failures(state)


def test_render_report_stage_not_executed_message():
    state = _complete_state()
    state.registration_queue = [Gap("XYZ", "AKCYJNY 000001", "brak instruments.theme", "gdzie")]
    state.stage_not_executed = {"ceny_fx", "ryzyko"}
    text = render_report(state)
    assert "nie wykonano (cykl zatrzymany na etapie" in text


# ---------------------------------------------------------------------------
# T28 (poprawka po przeglądzie): tabela ZAWSZE pokazuje wszystkie rynki
# ---------------------------------------------------------------------------


def test_render_report_t28_table_shows_all_markets_ok_and_failure():
    state = _complete_state()
    state.freshness_results = [
        MarketFreshnessResult("XWAR", date(2026, 9, 26), 0, False),
        MarketFreshnessResult("XNAS", date(2026, 9, 20), 5, True),
    ]
    text = render_report(state)
    assert "| GPW | XWAR |" in text
    assert "| OK |" in text or "OK |" in text
    assert "| NASDAQ | XNAS |" in text
    assert "DATA FAILURE |" in text
    assert "DATA FAILURE: XNAS" in text
    assert has_data_failures(state)


def test_render_report_t28_table_all_ok_still_shows_table_and_brak_line():
    state = _complete_state()
    state.freshness_results = [MarketFreshnessResult("XWAR", date(2026, 9, 26), 0, False)]
    text = render_report(state)
    assert "| GPW | XWAR |" in text
    assert "DATA FAILURE: brak" in text
    assert not has_data_failures(state)


def test_calendar_code_to_exchange_reverse_mapping():
    assert CALENDAR_CODE_TO_EXCHANGE["XWAR"] == "GPW"
    assert CALENDAR_CODE_TO_EXCHANGE["XNAS"] == "NASDAQ"


# ---------------------------------------------------------------------------
# STOP §2 (poprawka po przeglądzie) — nagłówek D/prev_date, tylko zdarzenia
# ---------------------------------------------------------------------------


def test_render_report_stop_section_header_shows_d_and_prev_date():
    """Poprawka po przeglądzie (P2): "poprzednia ocena=<data>", bez "(D-1)"."""
    state = _complete_state()
    state.prev_risk_date = date(2026, 9, 24)
    text = render_report(state)
    assert "D=2026-09-25" in text
    assert "poprzednia ocena=2026-09-24" in text
    assert "(D-1)" not in text


def test_render_report_m62_column_header_shows_prev_date_not_d1():
    """Poprawka po przeglądzie (P2): nagłówek kolumny "stop z poprzedniej
    oceny (<prev_date>)" zamiast "stop D-1"."""
    from mannaz.cycle import StopOrderRow

    state = _complete_state()
    state.prev_risk_date = date(2026, 9, 24)
    state.stop_order_rows = [
        StopOrderRow(
            broker_ticker="XYZ",
            rachunek="AKCYJNY 000001",
            position_kind="long",
            state="NORMAL",
            stop_d=Decimal("110"),
            stop_d_currency="USD",
            settlement_currency="USD",
            stop_prev=Decimal("100"),
            atr22_threshold=Decimal("5.00"),
            base_symbol_label=None,
            decision="tak",
        )
    ]
    text = render_report(state)
    assert "stop z poprzedniej oceny (2026-09-24)" in text
    assert "stop D-1" not in text


def test_render_report_stop_events_table_rendered():
    from mannaz.cycle import StopEvent

    state = _complete_state()
    state.stop_events = [
        StopEvent(
            broker_ticker="XYZ",
            rachunek="AKCYJNY 000001",
            price_date_used=date(2026, 9, 25),
            close_d=Decimal("90"),
            stop_effective=Decimal("100"),
            stop_source="chandelier",
            price_source_symbol="XYZ",
            currency="USD",
            settlement_currency="USD",
        )
    ]
    text = render_report(state)
    assert "XYZ" in text
    assert "chandelier" in text


# ---------------------------------------------------------------------------
# Ryzyko — pozycje HIGH, udział w kapitale satelity (poprawka po przeglądzie, P1)
# ---------------------------------------------------------------------------


def test_render_report_risk_section_shows_high_risk_tickers_and_pct():
    state = _complete_state()
    state.risk_aggregates.high_risk_tickers = ["XYZ", "ABC"]
    state.risk_aggregates.high_risk_capital_pct = Decimal("3.5")
    text = render_report(state)
    assert "pozycje HIGH: 2, udział w kapitale satelity 3,50%" in text
    assert "XYZ" in text and "ABC" in text


def test_render_report_risk_section_no_high_risk_shows_brak():
    state = _complete_state()
    text = render_report(state)
    assert "pozycje HIGH: 0, udział w kapitale satelity brak — brak" in text


def test_render_report_risk_section_contract_high_annotated_separately():
    state = _complete_state()
    state.risk_aggregates.high_risk_tickers = ["FCDRZ26"]
    state.risk_aggregates.high_risk_contract_tickers = ["FCDRZ26"]
    state.risk_aggregates.high_risk_capital_pct = None
    text = render_report(state)
    assert "kontrakty: poza udziałem (C liczy rachunek KONTRAKTOWY jako K)" in text
    assert "FCDRZ26" in text


# ---------------------------------------------------------------------------
# _default_check_ignored (poprawka po przeglądzie) — git -C <najbliższy
# istniejący przodek path>, nie REPO_ROOT (repo kodu). Test na tmp_path z
# `git init` + .gitignore — pomijany, gdy git niedostępny w PATH.
# ---------------------------------------------------------------------------

import shutil as _shutil
import subprocess as _subprocess

from mannaz.cycle import _default_check_ignored, _nearest_existing_ancestor

_GIT_AVAILABLE = _shutil.which("git") is not None


def _git(*args, cwd):
    _subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git niedostępny w PATH")
def test_check_ignored_finds_repo_via_nearest_existing_ancestor(tmp_path):
    repo = tmp_path / "other_repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text("data/reports/\n", encoding="utf-8")
    _git("add", ".gitignore", cwd=repo)
    _git(
        "-c", "user.email=t@example.com", "-c", "user.name=t",
        "commit", "-q", "-m", "init",
        cwd=repo,
    )

    target_dir = repo / "data" / "reports"
    # Katalog docelowy CELOWO nie istnieje jeszcze — check_ignored musi
    # znaleźć repo przez najbliższego ISTNIEJĄCEGO przodka (`repo`), nie
    # przez sam `target` (nieistniejący) ani przez REPO_ROOT (inne repo).
    assert not target_dir.exists()
    target = target_dir / "cykl-2026-09-25.md"

    ancestor = _nearest_existing_ancestor(target)
    assert ancestor == repo.resolve()
    assert _default_check_ignored(target) is True


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git niedostępny w PATH")
def test_check_ignored_false_when_not_ignored(tmp_path):
    repo = tmp_path / "other_repo2"
    repo.mkdir()
    _git("init", "-q", cwd=repo)

    target = repo / "not_ignored.md"
    assert _default_check_ignored(target) is False


def _init_ignoring_repo(repo: Path, gitignore_pattern: str) -> None:
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    (repo / ".gitignore").write_text(gitignore_pattern, encoding="utf-8")
    _git("add", ".gitignore", cwd=repo)
    _git(
        "-c", "user.email=t@example.com", "-c", "user.name=t",
        "commit", "-q", "-m", "init",
        cwd=repo,
    )


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git niedostępny w PATH")
def test_check_ignored_true_when_report_target_already_exists(tmp_path):
    """Q1 (poprawka po przeglądzie, po STOP przebiegu #2): diagnoza sesji
    głównej — gdy plik docelowy JUŻ ISTNIEJE, `_nearest_existing_ancestor`
    nie może zwrócić samego pliku (`git -C <plik>` -> kod 128, odczytywane
    jako "nie ignorowane"). Scenariusz: raport `cykl-...md` koliduje z już
    istniejącym plikiem tej samej nazwy (drugi przebieg tego samego dnia)."""
    repo = tmp_path / "repo_report_exists"
    _init_ignoring_repo(repo, "data/reports/\n")

    reports_dir = repo / "data" / "reports"
    reports_dir.mkdir(parents=True)
    target = reports_dir / "cykl-2026-09-25_0800.md"
    target.write_text("juz istnieje", encoding="utf-8")  # kolizja — plik JUZ ISTNIEJE

    assert target.exists()
    ancestor = _nearest_existing_ancestor(target)
    assert ancestor != target  # NIGDY sam plik
    assert ancestor == reports_dir.resolve()
    assert _default_check_ignored(target) is True


@pytest.mark.skipif(not _GIT_AVAILABLE, reason="git niedostępny w PATH")
def test_check_ignored_true_when_archive_target_already_exists(tmp_path):
    """Q1 — ten sam scenariusz dla archiwum (`raw_archive_dir`, C2): plik
    `<last_date>_<sha8>.csv` już istnieje (duplikat archiwizacji)."""
    repo = tmp_path / "repo_archive_exists"
    _init_ignoring_repo(repo, "data/raw/financeHistory/\n")

    archive_dir = repo / "data" / "raw" / "financeHistory"
    archive_dir.mkdir(parents=True)
    target = archive_dir / "2026-09-25_abcd1234.csv"
    target.write_text("juz istnieje", encoding="utf-8")

    assert target.exists()
    assert _default_check_ignored(target) is True


# ---------------------------------------------------------------------------
# Q2 (poprawka po przeglądzie, po STOP przebiegu #2) — nazwa raportu z HHMM
# lokalnym, nigdy nie nadpisuj (sufiks _2, _3, ...).
# ---------------------------------------------------------------------------

from mannaz.cycle import report_base_name, write_report_unique  # noqa: E402


def test_report_base_name_with_d_includes_local_hhmm():
    run_started_at = datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc)  # UTC
    name = report_base_name(date(2026, 9, 25), run_started_at)
    expected_hhmm = run_started_at.astimezone().strftime("%H%M")
    assert name == f"cykl-2026-09-25_{expected_hhmm}"


def test_report_base_name_without_d_uses_przebieg_prefix_and_date():
    run_started_at = datetime(2026, 9, 25, 6, 30, tzinfo=timezone.utc)
    name = report_base_name(None, run_started_at)
    local = run_started_at.astimezone()
    assert name == f"cykl-przebieg-{local:%Y-%m-%d}_{local:%H%M}"


def test_write_report_unique_first_write_uses_base_name(tmp_path):
    path = write_report_unique(tmp_path, "cykl-2026-09-25_0800", "tresc 1")
    assert path == tmp_path / "cykl-2026-09-25_0800.md"
    assert path.read_text(encoding="utf-8") == "tresc 1"


def test_write_report_unique_never_overwrites_adds_suffix(tmp_path):
    path1 = write_report_unique(tmp_path, "cykl-2026-09-25_0800", "przebieg 1")
    path2 = write_report_unique(tmp_path, "cykl-2026-09-25_0800", "przebieg 2")

    assert path1 != path2
    assert path1 == tmp_path / "cykl-2026-09-25_0800.md"
    assert path2 == tmp_path / "cykl-2026-09-25_0800_2.md"
    # pierwszy plik pozostaje BAJTOWO nietknięty
    assert path1.read_text(encoding="utf-8") == "przebieg 1"
    assert path2.read_text(encoding="utf-8") == "przebieg 2"


def test_write_report_unique_third_collision_gets_suffix_3(tmp_path):
    write_report_unique(tmp_path, "cykl-2026-09-25_0800", "a")
    write_report_unique(tmp_path, "cykl-2026-09-25_0800", "b")
    path3 = write_report_unique(tmp_path, "cykl-2026-09-25_0800", "c")
    assert path3 == tmp_path / "cykl-2026-09-25_0800_3.md"
    assert path3.read_text(encoding="utf-8") == "c"


def test_two_runs_same_d_different_hhmm_produce_two_distinct_files(tmp_path):
    """Q2: dwa przebiegi na ten sam D o różnym czasie lokalnym startu ->
    różne nazwy bazowe (HHMM się różni) -> dwa pliki bez kolizji sufiksu."""
    run1 = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
    run2 = datetime(2026, 9, 25, 9, 15, tzinfo=timezone.utc)
    name1 = report_base_name(date(2026, 9, 25), run1)
    name2 = report_base_name(date(2026, 9, 25), run2)
    assert name1 != name2

    path1 = write_report_unique(tmp_path, name1, "przebieg A")
    path2 = write_report_unique(tmp_path, name2, "przebieg B")
    assert path1 != path2
    assert path1.read_text(encoding="utf-8") == "przebieg A"
    assert path2.read_text(encoding="utf-8") == "przebieg B"


# ---------------------------------------------------------------------------
# Q3 (poprawka po przeglądzie, po STOP przebiegu #2) — wydruk odporny na
# kodowanie + check-ignore odmowa: brak zapisu, brak druku pełnego raportu,
# kod != 0, komunikat ASCII.
# ---------------------------------------------------------------------------

import io

from mannaz.cycle import safe_print  # noqa: E402


def test_safe_print_does_not_raise_on_cp1250_stream_with_emoji():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1250")
    text_with_emoji = "## 🔴 DATA FAILURE\n## 🟢 HEARTBEAT"
    safe_print(text_with_emoji, stream=stream)  # nie powinno rzucić wyjątku


def test_safe_print_writes_something_readable_to_buffer():
    buf = io.BytesIO()
    stream = io.TextIOWrapper(buf, encoding="cp1250")
    safe_print("plain ascii line", stream=stream)
    stream.flush()
    assert b"plain ascii line" in buf.getvalue()


def test_safe_print_falls_back_when_stream_has_no_reconfigure():
    class _NoReconfigureBuffer:
        def __init__(self):
            self.written = b""

        def write(self, data: bytes) -> int:
            self.written += data
            return len(data)

        def flush(self) -> None:
            pass

    class _NoReconfigureStream:
        """Symuluje strumień bez `.reconfigure` (np. niestandardowy obiekt
        podmieniony jako sys.stdout), ale z `.buffer` — druga gałąź `safe_print`."""

        def __init__(self):
            self.buffer = _NoReconfigureBuffer()

    stream = _NoReconfigureStream()
    safe_print("## 🔴 emoji linia", stream=stream)
    assert "emoji".encode("utf-8") in stream.buffer.written


# ---------------------------------------------------------------------------
# Z2 (brief CC-C fix) — raport tygodniowy pokazuje TYP rachunku, NIGDY numer.
# ---------------------------------------------------------------------------


def test_account_label_three_types_returns_type_without_number():
    assert account_label("AKCYJNY 900001") == "AKCYJNY"
    assert account_label("ZAGRANICZNY 900002") == "ZAGRANICZNY"
    assert account_label("KONTRAKTOWY 900003") == "KONTRAKTOWY"


def test_account_label_no_space_returns_whole_string_unchanged():
    assert account_label("BEZSPACJI") == "BEZSPACJI"


def test_account_label_empty_string_returns_empty_string():
    assert account_label("") == ""


def test_account_label_never_returns_pure_digits():
    for rachunek in ("AKCYJNY 900001", "ZAGRANICZNY 900002", "KONTRAKTOWY 900003"):
        label = account_label(rachunek)
        assert not label.isdigit(), f"account_label nie powinno zwrocic samych cyfr: {label!r}"


_Z2_RACHUNEK_AKCYJNY = "AKCYJNY 900001"
_Z2_RACHUNEK_ZAGRANICZNY = "ZAGRANICZNY 900002"
_Z2_RACHUNEK_KONTRAKTOWY = "KONTRAKTOWY 900003"


def _z2_state_with_accounts_in_every_section() -> ReportState:
    """Stan z rachunkami w formacie "<TYP> <numer syntetyczny>" (900001-900003)
    we WSZYSTKICH sekcjach raportu, które pokazują rachunek: DATA FAILURE
    (ciągłość + kolejka rejestracji), STOP, M62, Zmiany pozycji, HEARTBEAT
    (per rachunek)."""
    state = ReportState(
        d=date(2026, 9, 25),
        run_started_at=datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc),
    )
    # DATA FAILURE > Błędy importu (ciągłość) — string już wyrenderowany przez
    # `ContinuityGap.as_data_failure()`, dokładnie jak w `run_cycle`.
    state.import_failures = [
        ContinuityGap(_Z2_RACHUNEK_AKCYJNY, date(2026, 3, 10), date(2026, 3, 5)).as_data_failure()
    ]
    # DATA FAILURE > Kolejka rejestracji (bramka)
    state.registration_queue = [
        Gap(
            ticker="AAA",
            rachunek=_Z2_RACHUNEK_AKCYJNY,
            brak="brak instruments.yahoo_symbol",
            gdzie_uzupelnic="instruments.yahoo_symbol/exchange",
        )
    ]
    # STOP (§2)
    state.stop_events = [
        StopEvent(
            broker_ticker="BBB",
            rachunek=_Z2_RACHUNEK_ZAGRANICZNY,
            price_date_used=date(2026, 9, 25),
            close_d=Decimal("90"),
            stop_effective=Decimal("100"),
            stop_source="chandelier",
            price_source_symbol="BBB",
            currency="USD",
            settlement_currency="EUR",
        )
    ]
    # M62
    state.stop_order_rows = [
        StopOrderRow(
            broker_ticker="BBB",
            rachunek=_Z2_RACHUNEK_ZAGRANICZNY,
            position_kind="long",
            state="NORMAL",
            stop_d=Decimal("110"),
            stop_d_currency="USD",
            settlement_currency="EUR",
            stop_prev=Decimal("100"),
            atr22_threshold=Decimal("5.00"),
            base_symbol_label=None,
            decision="tak",
        )
    ]
    # Zmiany pozycji
    state.position_changes = [
        PositionChange(broker_ticker="CCC", rachunek=_Z2_RACHUNEK_KONTRAKTOWY, rodzaj="nowa"),
    ]
    # HEARTBEAT > pliki przetworzone (per rachunek)
    state.files_processed = [
        ProcessedFileReportRow(
            original_name="financeHistory (1).csv",
            sha8="deadbeef",
            status="przetworzony",
            per_rachunek={_Z2_RACHUNEK_AKCYJNY: (1, 0), _Z2_RACHUNEK_KONTRAKTOWY: (2, 1)},
            date_range=(date(2026, 3, 1), date(2026, 3, 5)),
        )
    ]
    return state


def test_render_report_never_shows_account_numbers_only_types():
    state = _z2_state_with_accounts_in_every_section()
    text = render_report(state)

    for number in ("900001", "900002", "900003"):
        assert number not in text, f"raport nie powinien pokazywac numeru rachunku (znaleziono {number!r})"
    for typ in ("AKCYJNY", "ZAGRANICZNY", "KONTRAKTOWY"):
        assert typ in text, f"raport powinien pokazywac TYP rachunku {typ!r}"


def test_render_report_positive_control_old_path_would_leak_number(monkeypatch):
    """Kontrolka dodatnia: ten sam stan wyrenderowany starą ścieżką
    (`mannaz.cycle.account_label` podmieniony na tożsamość) MUSI zawierać co
    najmniej jeden numer rachunku — dowód, że test powyżej faktycznie umie
    wykryć numer, a nie tylko że akurat żaden się nie pojawił."""
    state = _z2_state_with_accounts_in_every_section()
    monkeypatch.setattr("mannaz.cycle.account_label", lambda rachunek: rachunek)
    text = render_report(state)
    assert any(number in text for number in ("900001", "900002", "900003"))


# ---------------------------------------------------------------------------
# B-32: poziom 1 per nazwę w raporcie
# ---------------------------------------------------------------------------


def _l1_row(ticker, cur, name_key, pct, name_pct, breach, itype="equity"):
    from types import SimpleNamespace

    return SimpleNamespace(
        broker_ticker=ticker, settlement_currency=cur, name_key=name_key,
        risk_pct_satellite_capital=pct, name_risk_pct=name_pct, level1_breach=breach,
        instrument_type=itype, is_core=False, below_stop=False, price_is_stale=False,
        price_date_used=None, close_d=None, fx_rate=None, qty=Decimal(1),
    )


def _l1_summary(rows):
    from types import SimpleNamespace

    return SimpleNamespace(
        rows=rows, theme_budgets={}, total_risk_pct_satellite_capital=None,
        total_risk_pct_zagraniczny_satellite_capital=None, level3_breach=False,
        capital_satelite_positions_pln=Decimal(0), kontraktowy_account_value_pln=Decimal(0),
        capital_satelite_pln=Decimal(0), level1_breach_tickers=["STALE-ROW-LIST"],
    )


def _l1_line(rows, labels=None):
    from mannaz.cycle import _render_risk_section

    state = _complete_state()
    state.risk_aggregates = RiskReportAggregates.from_risk_summary(_l1_summary(rows), labels)
    return [ln for ln in _render_risk_section(state) if ln.startswith("- poziom 1")][0]


def test_level1_report_positive_two_rows_one_name_breach():
    line = _l1_line(
        [_l1_row("AAA", "USD", 1, Decimal("0.6"), Decimal("1.2"), True),
         _l1_row("AAA", "PLN", 1, Decimal("0.6"), Decimal("1.2"), True)],
        {1: "AAA"},
    )
    assert "(per nazwa)" in line
    assert line.count("AAA 1") == 2 or "AAA/USD" in line  # max + przekroczenie
    assert "przekroczenia > 1%: AAA" in line
    assert "AAA/USD" in line and "AAA/PLN" in line and " + " in line
    assert "STALE-ROW-LIST" not in line


def test_level1_report_negative_two_names_no_breach():
    line = _l1_line(
        [_l1_row("AAA", "USD", 1, Decimal("0.6"), Decimal("0.6"), False),
         _l1_row("BBB", "USD", 2, Decimal("0.6"), Decimal("0.6"), False)],
        {1: "AAA", 2: "BBB"},
    )
    assert "przekroczenia > 1%: brak" in line
    assert "max AAA 0,6" in line.replace(".", ",") or "max AAA 0.6" in line
    assert "niepełne" not in line


def test_level1_report_incomplete_name_listed():
    line = _l1_line(
        [_l1_row("AAA", "USD", 1, Decimal("0.6"), None, None),
         _l1_row("BBB", "USD", 2, Decimal("0.2"), Decimal("0.2"), False)],
        {1: "AAA", 2: "BBB"},
    )
    assert "niepełne: AAA (AAA)" in line
    assert "przekroczenia > 1%: brak" in line

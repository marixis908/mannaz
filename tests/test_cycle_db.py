"""Testy bazodanowe cyklu tygodniowego — brief CC-C, C9. Wzorzec
`tests/test_prices_ingest_safety_db.py`: JEDNA transakcja, `conn.rollback()`
w `finally`, DDL migracji 010 zduplikowany tutaj (nie parsujemy pliku SQL —
transakcyjność DDL Postgresa pozwala zastosować go w tej samej transakcji
testowej). Pomijane (skip), gdy `mannaz.db.get_connection()` zawiedzie.

Zero sieci: `fetch_prices`/`fetch_fx`/`risk_fn` są ZAWSZE stubami (brief C9:
"ceny/FX/ryzyka stuby") — cykl w tych testach nigdy nie woła yfinance/NBP/
pełnego przeliczenia ryzyka na prawdziwej bazie. `commit=False` przez cały
czas — zero trwałych zmian, rollback w `finally` (jak `run_fifo`/`run_risk`
w innych testach DB tego repo).

Dane syntetyczne wyłącznie: rachunek `TEST 000001`, daty 1990, ISIN
syntetyczny. Zero ilości/kosztów/kwot ownera drukowanych z tych testów."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from mannaz.db import get_connection
from mannaz.cycle import run_cycle
from mannaz.fx import FxSummary
from mannaz.prices import PricesSummary
from mannaz.risk import RiskSummary

_PROCESSED_HISTORY_FILES_DDL = """
CREATE TABLE IF NOT EXISTS processed_history_files (
    sha256              TEXT PRIMARY KEY,
    original_name       TEXT NOT NULL,
    stored_path         TEXT NOT NULL,
    first_date          DATE,
    last_date           DATE,
    rows_total          INTEGER NOT NULL,
    rows_new            INTEGER NOT NULL,
    rows_duplicate      INTEGER NOT NULL,
    processed_at        TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

RACHUNEK = "TEST 000001"


@pytest.fixture
def db_conn():
    try:
        conn = get_connection()
    except Exception as exc:  # brak .env/hasla/serwera -> caly test pomijamy
        pytest.skip(f"mannaz.db.get_connection() niedostepne: {exc}")
        return

    def _exists() -> bool:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('processed_history_files')")
            return cur.fetchone()[0] is not None

    existed_before = _exists()
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(_PROCESSED_HISTORY_FILES_DDL)
    try:
        yield conn
    finally:
        conn.rollback()
        existed_after = _exists()
        conn.close()
        assert existed_after == existed_before, (
            "brief CC-C, C9: stan tabeli processed_history_files zmienil sie trwale w tescie — "
            f"przed: {existed_before}, po: {existed_after}"
        )


class _CallSpy:
    """Zlicza wywołania, zwraca stałą wartość — do assercji "niewywołane"
    (fetch_prices/fetch_fx/risk_fn w testach 2/3/4)."""

    def __init__(self, return_value):
        self.calls = 0
        self._return_value = return_value

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self._return_value


def _stub_fetch_prices() -> _CallSpy:
    return _CallSpy(PricesSummary())


def _stub_fetch_fx() -> _CallSpy:
    return _CallSpy(FxSummary())


def _stub_risk_fn(as_of: date = date(1990, 1, 1)) -> _CallSpy:
    return _CallSpy(RiskSummary(risk_date=as_of))


def _write_bom_csv(path: Path, content: str) -> None:
    path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))


def _buy_title(name: str, isin: str, qty: str, price: str, order_no: str) -> str:
    return f"Rozliczenie transakcji kupna: {name} ({isin}) {qty} x {price} PLN nr {order_no}"


def _run_cycle_kwargs(tmp_path: Path, conn, **overrides):
    kwargs = dict(
        conn=conn,
        incoming_dir=tmp_path / "incoming",
        raw_archive_dir=tmp_path / "archive",
        reports_dir=tmp_path / "reports",
        today=date(1990, 3, 10),
        commit=False,
        fetch_prices=_stub_fetch_prices(),
        fetch_fx=_stub_fetch_fx(),
        risk_fn=_stub_risk_fn(),
        check_ignored=lambda p: True,
        detect_corp_events_fn=lambda conn: [],  # B-29: zero sieci w testach
    )
    kwargs.update(overrides)
    return kwargs


def _tx_count(conn, rachunek: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transactions WHERE rachunek = %s", (rachunek,))
        return cur.fetchone()[0]


# ---------------------------------------------------------------------------
# 1 — ten sam plik dwa razy -> za drugim razem pominięty po sha256, zero
#     nowych wierszy.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_1_same_file_twice_second_run_skipped_by_sha256(db_conn, tmp_path):
    conn = db_conn
    try:
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        content = (
            "Data;Rachunek;Waluta;Tytuł operacji;Wartość\n"
            f"01.03.1990;{RACHUNEK};PLN;"
            + _buy_title("TESTOWA SA", "PL0000000TST1", "10.000000", "5.000000", "0000000001")
            + ";-50,00\n"
        )
        _write_bom_csv(incoming / "financeHistory (1).csv", content)

        result1 = run_cycle(**_run_cycle_kwargs(tmp_path, conn))
        assert len(result1.state.files_processed) == 1
        assert result1.state.files_skipped == []
        count_after_1 = _tx_count(conn, RACHUNEK)
        assert count_after_1 > 0

        # plik nr 1 przeniesiony do archiwum -> incoming pusty; wrzucamy IDENTYCZNĄ
        # treść pod nową nazwą (identyczny sha256 pliku).
        assert list(incoming.glob("financeHistory*.csv")) == []
        _write_bom_csv(incoming / "financeHistory (1).csv", content)

        result2 = run_cycle(**_run_cycle_kwargs(tmp_path, conn))
        assert len(result2.state.files_processed) == 0
        assert len(result2.state.files_skipped) == 1
        assert result2.state.files_skipped[0].status == "pominięty (sha256 już przetworzony)"

        count_after_2 = _tx_count(conn, RACHUNEK)
        assert count_after_2 == count_after_1, "drugi przebieg nie powinien wstawic zadnych nowych wierszy"
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# 2 — plik z dziurą w historii -> DATA FAILURE, run_fifo NIEWYWOŁANE,
#     positions_fifo bez zmian, plik NIE przeniesiony.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_2_continuity_gap_stops_before_fifo(db_conn, tmp_path, monkeypatch):
    conn = db_conn
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO transactions
                    (transaction_date, rachunek, currency, title_raw, amount, occurrence_no, row_type, source_file, source_sha256)
                VALUES (%s, %s, 'PLN', 'Przelew na zewnątrz', -1, 1, 'przelew_zewnetrzny', 'synthetic_baseline.csv', %s)
                """,
                (date(1990, 1, 31), RACHUNEK, "0" * 64),
            )
            cur.execute("SELECT count(*) FROM positions_fifo")
            positions_before = cur.fetchone()[0]

        incoming = tmp_path / "incoming"
        incoming.mkdir()
        content = (
            "Data;Rachunek;Waluta;Tytuł operacji;Wartość\n"
            f"01.03.1990;{RACHUNEK};PLN;"
            + _buy_title("DZIURA SA", "PL0000000GAP1", "1.000000", "1.000000", "0000000002")
            + ";-1,00\n"
        )
        target_file = incoming / "financeHistory (1).csv"
        _write_bom_csv(target_file, content)

        fifo_spy_calls = []
        monkeypatch.setattr(
            "mannaz.cycle.run_fifo",
            lambda *a, **k: fifo_spy_calls.append((a, k)),
        )

        result = run_cycle(**_run_cycle_kwargs(tmp_path, conn))

        assert result.exit_code == 2
        assert any("dziura w historii" in f for f in result.state.import_failures)
        assert fifo_spy_calls == [], "run_fifo NIE powinno byc wywolane po zerwaniu ciaglosci"
        assert target_file.exists(), "plik z dziura NIE powinien byc przeniesiony"

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM positions_fifo")
            positions_after = cur.fetchone()[0]
        assert positions_after == positions_before
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# 3 — kupno nowego instrumentu equity bez yahoo_symbol -> DATA FAILURE w
#     kolejce rejestracji; fetch_prices i risk_fn NIEWYWOŁANE.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_3_new_instrument_without_symbol_stops_at_registration_gate(db_conn, tmp_path):
    conn = db_conn
    try:
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        content = (
            "Data;Rachunek;Waluta;Tytuł operacji;Wartość\n"
            f"01.03.1990;{RACHUNEK};PLN;"
            + _buy_title("NOWYSYM SA", "PL0000000NEW1", "5.000000", "10.000000", "0000000003")
            + ";-50,00\n"
        )
        _write_bom_csv(incoming / "financeHistory (1).csv", content)

        fetch_prices_spy = _stub_fetch_prices()
        risk_spy = _stub_risk_fn()

        result = run_cycle(
            **_run_cycle_kwargs(
                tmp_path, conn, fetch_prices=fetch_prices_spy, risk_fn=risk_spy
            )
        )

        assert result.exit_code == 2
        assert any(g.ticker == "NOWYSYM SA" for g in result.state.registration_queue) or any(
            "yahoo_symbol" in g.brak for g in result.state.registration_queue
        )
        assert fetch_prices_spy.calls == 0
        assert risk_spy.calls == 0
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# 4 — nowa seria kontraktu bez multiplier_source -> DATA FAILURE w kolejce
#     rejestracji.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_4_new_futures_series_without_multiplier_source_stops_at_registration_gate(db_conn, tmp_path):
    conn = db_conn
    try:
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        # PL0GF... -> classify_instrument_type sklasyfikuje jako 'future' (jak
        # w tests/test_parse_history.py::test_buy_future_no_ticker_uses_name_as_ticker).
        content = (
            "Data;Rachunek;Waluta;Tytuł operacji;Wartość\n"
            f"01.03.1990;{RACHUNEK};PLN;"
            + _buy_title("FTSTH99", "PL0GF0012399", "2.000000", "10.000000", "0000000004")
            + ";-20,00\n"
        )
        _write_bom_csv(incoming / "financeHistory (1).csv", content)

        result = run_cycle(**_run_cycle_kwargs(tmp_path, conn))

        assert result.exit_code == 2
        assert any(g.ticker == "FTSTH99" for g in result.state.registration_queue)
        assert any(
            "multiplier" in g.brak for g in result.state.registration_queue if g.ticker == "FTSTH99"
        )
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# 7 (poprawka po przeglądzie) — plik z nieznanym tytułem -> DATA FAILURE
#     PRZED importem, zero nowych wierszy transactions, plik NIE przeniesiony.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_7_unknown_title_stops_before_import_zero_new_rows(db_conn, tmp_path):
    conn = db_conn
    try:
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        content = (
            "Data;Rachunek;Waluta;Tytuł operacji;Wartość\n"
            f"01.03.1990;{RACHUNEK};PLN;Zupełnie nowy typ operacji którego nie znamy;-1,00\n"
        )
        target_file = incoming / "financeHistory (1).csv"
        _write_bom_csv(target_file, content)

        count_before = _tx_count(conn, RACHUNEK)

        result = run_cycle(**_run_cycle_kwargs(tmp_path, conn))

        assert result.exit_code == 2
        assert any("nieznane wzorce tytułów" in f for f in result.state.import_failures)
        assert result.state.files_processed == []
        assert result.state.files_skipped == []
        assert target_file.exists(), "plik z nieznanym tytulem NIE powinien byc przeniesiony"

        count_after = _tx_count(conn, RACHUNEK)
        assert count_after == count_before, "import z unknown title nie powinien wstawic zadnych wierszy"
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# 5 — T28: rynek z ceną starszą niż 2 sesje -> DATA FAILURE w raporcie
#     (czysta, na stanie — jak dopuszcza brief C9).
# ---------------------------------------------------------------------------


def test_5_t28_stale_market_appears_as_data_failure_in_report():
    from mannaz.cycle import ReportState, has_data_failures, MarketFreshnessResult, render_report
    from datetime import datetime, timezone

    state = ReportState(d=date(1990, 3, 9), run_started_at=datetime(1990, 3, 10, 8, tzinfo=timezone.utc))
    state.freshness_results = [MarketFreshnessResult("XWAR", date(1990, 3, 3), 5, True)]
    assert has_data_failures(state)
    text = render_report(state)
    assert "DATA FAILURE" in text
    assert "XWAR" in text


# ---------------------------------------------------------------------------
# 6 — komplet: raport ze wszystkimi sekcjami, kod 0 (czysty render_report na
#     stanie z kompletem, jak dopuszcza brief C9).
# ---------------------------------------------------------------------------


def test_6_complete_state_report_has_all_sections_and_exit_0():
    from mannaz.cycle import ReportState, RiskReportAggregates, has_data_failures, render_report
    from datetime import datetime, timezone

    state = ReportState(d=date(1990, 3, 9), run_started_at=datetime(1990, 3, 10, 8, tzinfo=timezone.utc))
    state.risk_aggregates = RiskReportAggregates(
        heat_pct_total=Decimal("1.0"),
        heat_pct_zagraniczny=Decimal("0.5"),
        level1_max_ticker=None,
        level1_max_pct=None,
        level1_breach_tickers=[],
        theme_ranking=[],
        level3_breach=False,
        capital_satelite_positions_pln=Decimal("0"),
        kontraktowy_account_value_pln=Decimal("0"),
        capital_satelite_pln=Decimal("0"),
        stale_tickers=[],
    )
    state.exit_code = 0 if not has_data_failures(state) else 2
    text = render_report(state)
    for header in (
        "## 🔴 DATA FAILURE",
        "## 🔴 STOP",
        "## Zlecenia stop do aktualizacji u brokera (M62)",
        "## Ryzyko",
        "## Zmiany pozycji",
        "## 🟢 HEARTBEAT",
    ):
        assert header in text
    assert state.exit_code == 0
    assert "Kod wyjścia: 0" in text


# ---------------------------------------------------------------------------
# Q2/Q3 (poprawka po przeglądzie, po STOP przebiegu #2) — integracja
# run_cycle <-> zapis raportu: dwa przebiegi tego samego dnia nie nadpisują
# się nawzajem; odmowa check-ignore nie zapisuje ani nie drukuje raportu.
# ---------------------------------------------------------------------------


@pytest.mark.db
def test_8_two_cycle_runs_produce_two_distinct_report_files_first_untouched(db_conn, tmp_path):
    conn = db_conn
    try:
        result1 = run_cycle(**_run_cycle_kwargs(tmp_path, conn))
        content1_before = result1.report_path.read_text(encoding="utf-8")

        result2 = run_cycle(**_run_cycle_kwargs(tmp_path, conn))

        assert result1.report_path is not None
        assert result2.report_path is not None
        assert result1.report_path != result2.report_path, (
            "Q2: dwa przebiegi (ten sam D, prawdopodobnie ten sam HHMM w teście) "
            "musza dostac RÓŻNE pliki (sufiks _2), nie nadpisywać się"
        )
        assert result1.report_path.exists()
        assert result2.report_path.exists()
        # Q2: pierwszy plik bajtowo NIETKNIĘTY po drugim przebiegu.
        assert result1.report_path.read_text(encoding="utf-8") == content1_before
    finally:
        conn.rollback()


@pytest.mark.db
def test_9_check_ignored_denied_no_file_written_ascii_error_exit_nonzero(db_conn, tmp_path):
    conn = db_conn
    try:
        reports_dir = tmp_path / "reports"
        result = run_cycle(**_run_cycle_kwargs(tmp_path, conn, check_ignored=lambda p: False))

        assert result.report_path is None
        assert result.exit_code != 0
        assert result.error_message is not None
        assert result.error_message.startswith("ERROR: report path not git-ignored:")
        assert result.error_message.isascii()
        assert not reports_dir.exists(), "Q3: check-ignore odmowa -> katalog raportow NIE tworzony"
    finally:
        conn.rollback()


# ---------------------------------------------------------------------------
# Z2 (brief CC-C fix) — raport tygodniowy nigdy nie ujawnia numeru rachunku,
# nawet z REALNYMI kontami z `transactions` (nie tylko syntetycznym `TEST
# 000001` powyżej).
# ---------------------------------------------------------------------------


def _z2_import_new_instrument(tmp_path: Path, label: str, rachunek: str, order_no: str, isin_suffix: str) -> Path:
    """Buduje plik incoming z zakupem NOWEGO (nigdy wcześniej niezaimportowanego)
    instrumentu na `rachunek` — wzór `test_3_new_instrument_...` powyżej, ale
    na realnym rachunku (zamiast syntetycznego `TEST 000001`), żeby
    `registration_gaps` (brief C4) wygenerował wiersz z prawdziwym numerem
    rachunku w `state.registration_queue`, jeśli maskowanie (`account_label`)
    nie działałoby."""
    incoming = tmp_path / f"incoming_{label}"
    incoming.mkdir()
    content = (
        "Data;Rachunek;Waluta;Tytuł operacji;Wartość\n"
        f"01.03.1990;{rachunek};PLN;"
        + _buy_title(f"Z2TESTOWA{label} SA", f"PL0000000{isin_suffix}", "1.000000", "1.000000", order_no)
        + ";-1,00\n"
    )
    _write_bom_csv(incoming / "financeHistory (1).csv", content)
    return incoming


@pytest.mark.db
def test_z2_real_account_numbers_never_leak_into_rendered_report(db_conn, tmp_path):
    conn = db_conn
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT rachunek FROM transactions")
            all_rachunki = [r[0] for r in cur.fetchall() if r[0]]
        if not all_rachunki:
            pytest.skip("brak rachunkow w transactions — test wymaga istniejacych danych")

        # Ciagi cyfr (numery rachunkow) wyciagniete ze WSZYSTKICH realnych
        # rachunkow w bazie — len>=4, zeby nie lapac przypadkowych krotkich
        # liczb (np. pojedynczych cyfr) gdzie indziej w raporcie. Nigdy nie
        # drukowane (asercje nizej uzywaja WYLACZNIE liczby trafien).
        digit_sequences = sorted(
            {m for r in all_rachunki for m in re.findall(r"\d+", r) if len(m) >= 4}
        )
        if not digit_sequences:
            pytest.skip("zaden realny rachunek nie zawiera numeru (>=4 cyfry) — nic do przetestowania")

        real_rachunek = next((r for r in all_rachunki if re.search(r"\d{4,}", r)), None)
        if real_rachunek is None:
            pytest.skip("brak rachunku z numerem >=4 cyfry")

        incoming_1 = _z2_import_new_instrument(tmp_path, "A", real_rachunek, "0000000091", "Z2A1")
        result = run_cycle(
            **_run_cycle_kwargs(
                tmp_path,
                conn,
                incoming_dir=incoming_1,
                raw_archive_dir=tmp_path / "archive_a",
                reports_dir=tmp_path / "reports_a",
            )
        )
        assert result.report_path is not None
        report_text = result.report_path.read_text(encoding="utf-8")

        hits = sum(1 for d in digit_sequences if d in report_text)
        assert hits == 0, f"raport zawiera {hits} ciag(i) cyfr pochodzacych z numerow realnych rachunkow (oczekiwano 0)"

        # Kontrolka dodatnia: identyczny scenariusz, ale `account_label`
        # podmieniony na tożsamość (stara ścieżka bez maskowania) — musi
        # ujawnić co najmniej jeden numer, dowodząc, że asercja powyżej
        # faktycznie umie wykryć numer, a nie tylko że akurat go nie było.
        incoming_2 = _z2_import_new_instrument(tmp_path, "B", real_rachunek, "0000000092", "Z2A2")
        with mock.patch("mannaz.cycle.account_label", side_effect=lambda r: r):
            result_identity = run_cycle(
                **_run_cycle_kwargs(
                    tmp_path,
                    conn,
                    incoming_dir=incoming_2,
                    raw_archive_dir=tmp_path / "archive_b",
                    reports_dir=tmp_path / "reports_b",
                )
            )
        assert result_identity.report_path is not None
        report_text_identity = result_identity.report_path.read_text(encoding="utf-8")
        identity_hits = sum(1 for d in digit_sequences if d in report_text_identity)
        assert identity_hits >= 1, "kontrolka dodatnia: z account_label=tozsamosc raport powinien ujawnic numer"
    finally:
        conn.rollback()

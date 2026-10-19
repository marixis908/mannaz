"""T1-T7 FPE-RUN: wybor uniwersum, przebieg i podkomenda; atrapy, bez sieci i bazy."""

import dataclasses
import socket
from types import SimpleNamespace

import pytest
from yfinance.exceptions import YFRateLimitError

from mannaz import estimates as est
from mannaz import run_p3

FORBIDDEN = {"qty", "quantity", "cost", "residual_cost", "value", "amount", "price",
             "ilosc", "koszt", "kwota"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("E15: polaczenie sieciowe zablokowane")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    from yfinance.data import YfData

    monkeypatch.setattr(YfData, "get", blocked)
    monkeypatch.setattr(est, "code_sha", lambda: "synthetic-sha")
    # fpe-wait A2: przebiegi bez --instrument-ids zapisywalyby universe_last.json obok
    # domyslnego archiwum; testy tego pliku go nie dotykaja (zapis testuje test_estimates_wait).
    monkeypatch.setattr(run_p3, "save_universe", lambda *a, **kw: None)


def response(body, status=200):
    return SimpleNamespace(content=body, status_code=status, headers={
        "Content-Type": "application/json", "Content-Encoding": None,
    })


BODY = (b'{"quoteSummary":{"result":[{"price":{"currency":"USD"},"earningsTrend":{"trend":[]}}],'
        b'"error":null}}')


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (1,)


class FakeConn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


@pytest.fixture
def inserts(monkeypatch):
    calls = []

    def fake_insert(cur, instrument_id, entry, parsed, sha):
        calls.append((instrument_id, entry.fetch.source_symbol, sha))
        return est.InsertSummary(len(calls), len(parsed.rows))

    monkeypatch.setattr(est, "insert_snapshot", fake_insert)
    return calls


def test_t1_universe_with_ids_reports_missing():
    cur = FakeCursor([(1, "AAA"), (3, "CCC")])
    universe = est.select_estimates_universe(cur, [3, 2, 1])
    assert universe == [(1, "AAA"), (3, "CCC")]
    sql, params = cur.executed[0]
    assert "yahoo_symbol IS NOT NULL" in sql and "ORDER BY id" in sql
    assert params == ([3, 2, 1],)
    # id 2 bez symbolu (albo bez instrumentu): wypisane, nie po cichu
    assert est.missing_universe_ids([3, 2, 1], universe) == [2]


def test_t1_universe_default_query_semantics():
    cur = FakeCursor([])
    assert est.select_estimates_universe(cur) == []
    sql = cur.executed[0][0]
    for fragment in ("instrument_type = 'equity'", "yahoo_symbol IS NOT NULL", "NOT i.is_core",
                     "NOT i.is_benchmark", "EXISTS (SELECT 1 FROM positions_fifo", "ORDER BY i.id"):
        assert fragment in sql


def test_t2_three_symbols_counts_and_sleep(tmp_path, inserts):
    statuses = {"A": 200, "B": 404, "C": 200}
    calls = []

    def http_get(url, params=None, timeout=None):
        calls.append(params["symbol"])
        return response(BODY, statuses[params["symbol"]])

    sleeps = []
    conn = FakeConn()
    summary = est.run_estimates(conn, [(1, "A"), (2, "B"), (3, "C")], archive_root=tmp_path,
                                http_get=http_get, sleep=sleeps.append, pause_s=1.5)
    assert calls == ["A", "B", "C"]
    assert sleeps == [1.5, 1.5]
    assert conn.commits == 3
    assert [c[2] for c in inserts] == ["synthetic-sha"] * 3
    # 404 liczy sie jako snapshot (S1) i jako blad (decyzja w docstringu RunSummary)
    assert (summary.symbols, summary.snapshots, summary.in_db, summary.archive_only) == (3, 3, 3, 0)
    assert (summary.errors, summary.rate_limited, summary.skipped) == (1, 0, 0)
    assert [r.status for r in summary.results] == ["ok", "blad", "ok"]
    assert summary.results[1].error == "HTTP 404" and summary.results[1].http_status == 404
    assert all(r.raw_ref and r.meta_ref for r in summary.results)
    assert summary.archive_root == str(tmp_path)


def rate_limited(url, params=None, timeout=None):
    raise YFRateLimitError()


def test_t3_series_of_429_skips_rest(tmp_path, inserts):
    calls = []

    def http_get(url, params=None, timeout=None):
        calls.append(params["symbol"])
        return rate_limited(url)

    sleeps = []
    conn = FakeConn()
    universe = [(i, f"S{i}") for i in range(1, 6)]
    summary = est.run_estimates(conn, universe, archive_root=tmp_path, http_get=http_get,
                                sleep=sleeps.append)
    assert len(calls) == 3
    assert (summary.rate_limited, summary.skipped, summary.symbols) == (3, 2, 5)
    assert summary.snapshots == 3 and summary.in_db == 3 and summary.errors == 0
    assert [r.status for r in summary.results][3:] == [est.SKIPPED_AFTER_429] * 2
    assert len(sleeps) == 2  # bez przerwy dla pominietych
    assert conn.commits == 3
    # wpis zgodnie z S1: meta bez body, ale zarchiwizowany
    assert summary.results[0].raw_ref is None and summary.results[0].meta_ref


def test_t4_single_429_resets_counter(tmp_path, inserts):
    plan = ["429", "200", "429", "200", "429", "200"]
    it = iter(plan)

    def http_get(url, params=None, timeout=None):
        kind = next(it)
        return rate_limited(url) if kind == "429" else response(BODY)

    universe = [(i, f"S{i}") for i in range(1, 7)]
    summary = est.run_estimates(FakeConn(), universe, archive_root=tmp_path, http_get=http_get,
                                sleep=lambda s: None)
    assert (summary.rate_limited, summary.skipped, summary.symbols) == (3, 0, 6)


def test_t5_archive_error_goes_on(monkeypatch, tmp_path, inserts):
    # prestworzony plik meta dla pierwszego symbolu -> FileExistsError w archiwum
    fixed = est.datetime(2026, 10, 18, 8, 0, 0, 1, tzinfo=est.timezone.utc)
    real_fetch = est.fetch_quote_summary
    monkeypatch.setattr(est, "fetch_quote_summary",
                        lambda symbol, **kw: real_fetch(symbol, now_utc=fixed, **kw))
    pre = tmp_path / "yahoo_quotesummary" / "A"
    pre.mkdir(parents=True)
    (pre / "20261018T080000000001Z.meta.json").write_text("{}", encoding="ascii")
    conn = FakeConn()
    summary = est.run_estimates(
        conn, [(1, "A"), (2, "B")], archive_root=tmp_path,
        http_get=lambda url, params=None, timeout=None: response(BODY), sleep=lambda s: None)
    first, second = summary.results
    assert first.status == "blad" and "FileExistsError" in first.error and not first.in_db
    assert second.status == "ok" and second.in_db
    assert [c[1] for c in inserts] == ["B"]  # insert_snapshot nie wywolany dla bledu archiwum
    assert (summary.errors, summary.snapshots, summary.in_db) == (1, 1, 1)
    assert conn.rollbacks == 1 and conn.commits == 1


def test_t5_db_error_goes_on(tmp_path, monkeypatch):
    def failing_insert(cur, instrument_id, entry, parsed, sha):
        if instrument_id == 1:
            raise RuntimeError("synthetic db failure")
        return est.InsertSummary(9, 0)

    monkeypatch.setattr(est, "insert_snapshot", failing_insert)
    conn = FakeConn()
    summary = est.run_estimates(
        conn, [(1, "A"), (2, "B")], archive_root=tmp_path,
        http_get=lambda url, params=None, timeout=None: response(BODY), sleep=lambda s: None)
    assert summary.results[0].status == "blad" and summary.results[1].in_db
    assert (summary.errors, summary.snapshots, summary.in_db, summary.archive_only) == (1, 2, 1, 1)
    assert conn.rollbacks == 1 and conn.commits == 1


def test_t5_exit_code_one_on_error(monkeypatch, tmp_path):
    summary = est.RunSummary(2, 1, 1, 0, 1, 0, 0, str(tmp_path), (
        est.SymbolResult(1, "A", None, None, 0, "blad", "archiwizacja: FileExistsError"),
        est.SymbolResult(2, "B", 200, None, 0, "ok", None, "r", "m", True, True),
    ))
    assert run_with(monkeypatch, summary, [(1, "A"), (2, "B")]) == 1


def run_with(monkeypatch, summary, universe, argv=("estimates",), get_conn=None):
    conn = FakeConn()
    monkeypatch.setattr(run_p3, "get_connection", get_conn or (lambda **kw: conn))
    monkeypatch.setattr(run_p3, "select_estimates_universe", lambda cur, ids=None: list(universe))
    monkeypatch.setattr(run_p3, "run_estimates", lambda *a, **kw: summary)
    with pytest.raises(SystemExit) as excinfo:
        run_p3.main(list(argv))
    assert conn.closed or get_conn is not None
    return excinfo.value.code


def make_summary(**over):
    base = dict(symbols=2, snapshots=2, in_db=2, archive_only=0, errors=0, rate_limited=0,
                skipped=0, archive_root="X", results=(
                    est.SymbolResult(1, "A", 200, None, 3, "ok", None, "r", "m", True, True),
                    est.SymbolResult(2, "B", 200, None, 3, "ok", None, "r", "m", True, True)))
    base.update(over)
    return est.RunSummary(**base)


def test_t6_exit_codes(monkeypatch, capsys):
    universe = [(1, "A"), (2, "B")]
    assert run_with(monkeypatch, make_summary(), universe) == 0
    out = capsys.readouterr().out
    # wiersze "baza: proba ..." (A1) poprzedzaja FPE-RUN; pomijamy je
    out_lines = [ln for ln in out.splitlines() if not ln.startswith("baza: proba")]
    assert out_lines[0].startswith("FPE-RUN: symbole 2")
    assert "snapshoty 2" in out and "w bazie 2" in out and "pominiete 0" in out
    assert run_with(monkeypatch, make_summary(errors=1), universe) == 1
    assert run_with(monkeypatch, make_summary(rate_limited=1), universe) == 1
    assert run_with(monkeypatch, make_summary(skipped=1), universe) == 1
    # puste uniwersum: kod 2, run_estimates nie jest wywolany
    def boom(*a, **kw):
        raise AssertionError("run_estimates nie moze byc wywolany")

    monkeypatch.setattr(run_p3, "run_estimates", boom)
    conn = FakeConn()
    monkeypatch.setattr(run_p3, "get_connection", lambda **kw: conn)
    monkeypatch.setattr(run_p3, "select_estimates_universe", lambda cur, ids=None: [])
    with pytest.raises(SystemExit) as excinfo:
        run_p3.main(["estimates", "--instrument-ids", "7", "8"])
    assert excinfo.value.code == 2 and conn.closed
    out = capsys.readouterr().out
    assert "pominiete id=7: brak yahoo_symbol lub instrumentu" in out
    assert "pominiete id=8: brak yahoo_symbol lub instrumentu" in out


def test_t6_missing_id_with_success_is_one(monkeypatch, capsys):
    # recenzja Codex V2 #1: pominiete id przy udanym reszcie przebiegu = kod 1 i licznik
    code = run_with(monkeypatch, make_summary(), [(1, "A"), (2, "B")],
                    argv=("estimates", "--instrument-ids", "1", "2", "9"))
    assert code == 1
    out = capsys.readouterr().out
    assert "pominiete id=9: brak yahoo_symbol lub instrumentu" in out and "pominiete 1" in out


def test_t6_db_error_during_run_is_four(monkeypatch):
    # fpe-wait A5 (D4): blad bazy przy zapisie symbolu = kod 4 (dotad 2; nizsze od 1).
    # Dane testu: symbol A zarchiwizowany z bledem bazy (archived=True), B w bazie;
    # dotychczasowe rate_limited=1 i drugi blad HTTP 404 dawalyby teraz 1, nie 4.
    summary = make_summary(errors=1, results=(
        est.SymbolResult(1, "A", 200, None, 0, "blad", "baza: OperationalError", "r", "m",
                         False, True, True),
        est.SymbolResult(2, "B", 200, None, 3, "ok", None, "r", "m", True, True)))
    assert run_with(monkeypatch, summary, [(1, "A"), (2, "B")]) == 4


def test_t6_connection_error_is_two(monkeypatch, capsys, tmp_path):
    def failing(**kw):
        raise RuntimeError("synthetic")

    # --db-wait-s 0: jedna proba; brak pliku uniwersum w tmp_path = kod 2 (A3)
    argv = ("estimates", "--db-wait-s", "0", "--archive-root", str(tmp_path / "raw"))
    assert run_with(monkeypatch, make_summary(), [(1, "A")], argv=argv, get_conn=failing) == 2
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "FPE-STAN:" in out


def test_t6_archive_root_default_and_override(monkeypatch):
    args = run_p3.build_parser().parse_args(["estimates"])
    assert args.archive_root.parts[-3:] == ("data", "estimates", "raw")
    assert args.instrument_ids is None
    args = run_p3.build_parser().parse_args(["estimates", "--instrument-ids", "5", "6",
                                             "--archive-root", "ABC"])
    assert args.instrument_ids == [5, 6] and str(args.archive_root) == "ABC"
    # skrot opcji odrzucony: wrapper kopiuje tylko katalog z pelnego --archive-root
    with pytest.raises(SystemExit) as excinfo:
        run_p3.build_parser().parse_args(["estimates", "--archive-r=ABC"])
    assert excinfo.value.code == 2


def test_t7_no_amount_fields_and_output(monkeypatch, capsys):
    for cls in (est.RunSummary, est.SymbolResult):
        assert not {f.name for f in dataclasses.fields(cls)} & FORBIDDEN
    assert run_with(monkeypatch, make_summary(), [(1, "A"), (2, "B")]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if not ln.startswith("baza: proba")]
    # linie FPE-STAN (A5) maja separator U+00B7 jak FPE-RUN; ASCII dotyczy wierszy symboli
    assert lines[0].startswith("FPE-RUN:") and all(
        line.isascii() for line in lines[1:] if not line.startswith("FPE-"))
    words = set(out.replace("=", " ").replace(":", " ").split())
    assert not words & FORBIDDEN
    assert "id=1 symbol=A http_status=200 parse_status=None wiersze=3" in out


# Decyzja planisty 2026-10-18, pkt 1: blad odczytu przy HTTP 200 = bledy (kod 1);
# stan danych (ksztalt CPR.WA: brak konsensusu i brak daty okresu) = kod 0.
CPR_LIKE = (
    b'{"quoteSummary":{"result":[{"price":{"currency":"PLN"},"earningsTrend":{"trend":['
    + b",".join(
        b'{"period":"%s","earningsEstimate":{},"revenueEstimate":{"avg":{"raw":0},'
        b'"low":{"raw":0},"high":{"raw":0},"numberOfAnalysts":{"raw":0}}}' % label
        for label in (b"0q", b"+1q", b"0y", b"+1y"))
    + b']}}],"error":null}}'
)
READ_ERRORS = {
    "bledny JSON": b"{nie-json",
    "quoteSummary.error": b'{"quoteSummary":{"result":null,"error":{"code":"Not Found"}}}',
    "brak wyniku dostawcy": b'{"quoteSummary":{"result":[],"error":null}}',
}


def run_cli_on_body(monkeypatch, tmp_path, body):
    import functools

    conn = FakeConn()
    monkeypatch.setattr(run_p3, "get_connection", lambda **kw: conn)
    monkeypatch.setattr(run_p3, "select_estimates_universe", lambda cur, ids=None: [(1, "SYN")])
    http_get = lambda url, params=None, timeout=None: response(body, 200)  # noqa: E731
    monkeypatch.setattr(run_p3, "run_estimates", functools.partial(
        est.run_estimates, http_get=http_get, sleep=lambda s: None))
    with pytest.raises(SystemExit) as excinfo:
        run_p3.main(["estimates", "--archive-root", str(tmp_path)])
    return excinfo.value.code


@pytest.mark.parametrize("prefix", sorted(READ_ERRORS))
def test_t8_read_error_on_http_200_is_error(monkeypatch, tmp_path, capsys, inserts, prefix):
    assert run_cli_on_body(monkeypatch, tmp_path, READ_ERRORS[prefix]) == 1
    out = capsys.readouterr().out
    assert "bledy 1" in out and f"parse_status={prefix}" in out and "status=blad" in out
    assert len(inserts) == 1  # snapshot nadal zapisany (S1/S3), tylko klasyfikacja przebiegu


def test_t8_cpr_like_no_consensus_is_ok(monkeypatch, tmp_path, capsys, inserts):
    parsed = est.parse_quote_summary(CPR_LIKE)
    # kontrolka: ksztalt jak CPR.WA - 4 wiersze, brak konsensusu, stan danych (brak daty okresu)
    assert len(parsed.rows) == 4 and parsed.status == est.NO_PERIOD_END
    assert all(row.rev_status == est.NO_CONSENSUS for row in parsed.rows)
    assert run_cli_on_body(monkeypatch, tmp_path, CPR_LIKE) == 0
    out = capsys.readouterr().out
    assert "bledy 0" in out and "status=ok" in out

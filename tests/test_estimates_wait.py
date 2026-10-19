"""W1-W11 fpe-wait etap A: czekanie na baze, zapasowe uniwersum, tryb awaryjny, kody wyjscia.

Testy czyste: atrapy clock/sleep/connect/http_get, siec zablokowana (jak E15), bez bazy.
"""

import functools
import hashlib
import json
import socket
from types import SimpleNamespace

import pytest

from mannaz import db
from mannaz import estimates as est
from mannaz import run_p3

DOT = "\u00b7"
BODY = (b'{"quoteSummary":{"result":[{"price":{"currency":"USD"},"earningsTrend":{"trend":[]}}],'
        b'"error":null}}')


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("E15: polaczenie sieciowe zablokowane")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    from yfinance.data import YfData

    monkeypatch.setattr(YfData, "get", blocked)
    monkeypatch.setattr(est, "code_sha", lambda: "synthetic-sha")


def response(body, status=200):
    return SimpleNamespace(content=body, status_code=status, headers={
        "Content-Type": "application/json", "Content-Encoding": None,
    })


class FakeClock:
    """Zegar monotoniczny i sleep, ktory go przesuwa; nic nie czeka naprawde."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (1,)


class FakeConn:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.commits = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self.rows)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        self.closed = True


def hand_sha(pairs):
    """Reczne sha kanoniczne: id<TAB>symbol<LF> per pozycja, posortowane po id."""
    text = "".join(f"{i}\t{s}\n" for i, s in sorted(pairs))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- A1: wait_for_db --------------------------------------------------------

def test_w1_three_failures_then_success():
    fake, calls, logs = FakeClock(), [], []
    sentinel = object()

    def connect(connect_timeout):
        calls.append(connect_timeout)
        if len(calls) <= 3:
            raise ConnectionError("tresc wyjatku nie moze trafic do logu")
        return sentinel

    got = est.wait_for_db(connect, clock=fake.clock, sleep=fake.sleep, log=logs.append)
    assert got is sentinel
    assert fake.sleeps == [15, 15, 15]
    assert logs == [
        f"baza: proba 1 {DOT} t=0s {DOT} ConnectionError",
        f"baza: proba 2 {DOT} t=15s {DOT} ConnectionError",
        f"baza: proba 3 {DOT} t=30s {DOT} ConnectionError",
        f"baza: proba 4 {DOT} t=45s {DOT} ok",
    ]
    assert not any("tresc" in line for line in logs)  # R7: bez tresci wyjatku


def test_w2_always_failing_returns_none_by_deadline():
    fake, starts, logs = FakeClock(), [], []

    def connect(connect_timeout):
        starts.append(fake.clock())
        raise OSError("x")

    got = est.wait_for_db(connect, deadline_s=600, interval_s=15, clock=fake.clock,
                          sleep=fake.sleep, log=logs.append)
    assert got is None
    # proby w t = 0, 15, ..., 585 (w t=600 limit juz przekroczony): 40 prob, 39 przerw
    assert len(starts) == 40 and starts[-1] == 585 and starts[-1] < 600
    assert len(fake.sleeps) == 39 and len(logs) == 40
    assert all(line.isascii() or DOT in line for line in logs)


def test_w2c_late_sleep_no_attempt_after_deadline():
    fake, starts = FakeClock(), []

    def connect(connect_timeout):
        starts.append(fake.clock())
        raise OSError("x")

    def late_sleep(seconds):  # przerwa dluzsza o 100 s: proby w t=0, 115, ..., 575; t=690 juz nie
        fake.sleep(seconds + 100)

    assert est.wait_for_db(connect, deadline_s=600, interval_s=15, clock=fake.clock,
                           sleep=late_sleep, log=lambda s: None) is None
    assert starts and all(t < 600 for t in starts)


def test_w2b_zero_deadline_single_attempt_no_sleep():
    fake, calls = FakeClock(), []

    def connect(connect_timeout):
        calls.append(connect_timeout)
        raise OSError("x")

    assert est.wait_for_db(connect, deadline_s=0, clock=fake.clock, sleep=fake.sleep,
                           log=lambda s: None) is None
    assert calls == [10] and fake.sleeps == []


def test_w3_connect_timeout_passed_each_call():
    fake, calls = FakeClock(), []

    def connect(**kw):
        calls.append(kw)
        if len(calls) < 3:
            raise OSError("x")
        return object()

    est.wait_for_db(connect, clock=fake.clock, sleep=fake.sleep, log=lambda s: None)
    assert calls == [{"connect_timeout": 10}] * 3


def test_w4_get_connection_connect_timeout_only_when_given(monkeypatch):
    seen = []
    monkeypatch.setattr(db, "load_dotenv_into_environ", lambda *a, **kw: None)  # nie czytaj .env
    monkeypatch.setenv("POSTGRES_PASSWORD", "synthetic")
    monkeypatch.setattr(db.psycopg, "connect", lambda **kw: seen.append(kw) or "conn")
    assert db.get_connection() == "conn"
    assert "connect_timeout" not in seen[0]
    db.get_connection(connect_timeout=7)
    assert seen[1]["connect_timeout"] == 7
    assert {k: v for k, v in seen[1].items() if k != "connect_timeout"} == seen[0]


# --- A2: plik uniwersum -----------------------------------------------------

def test_w5_save_sha_and_atomicity(tmp_path, monkeypatch):
    path = tmp_path / "universe_last.json"
    saved_at = est.save_universe(path, [(2, "BBB"), (1, "AAA")])
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert saved_at == doc["saved_at_utc"] and saved_at.endswith("Z")
    assert doc["items"] == [{"instrument_id": 1, "yahoo_symbol": "AAA"},
                            {"instrument_id": 2, "yahoo_symbol": "BBB"}]
    manual = hashlib.sha256("1\tAAA\n2\tBBB\n".encode("utf-8")).hexdigest()
    assert doc["sha256"] == manual == hand_sha([(1, "AAA"), (2, "BBB")])
    stored = est.load_universe(path)
    assert stored.items == [(1, "AAA"), (2, "BBB")] and stored.reason is None
    assert stored.age_days == 0
    # wyjatek w trakcie zapisu: stary plik bajt w bajt, bez resztek tymczasowych
    before = path.read_bytes()

    def failing_replace(src, dst):
        raise OSError("synthetic")

    monkeypatch.setattr(est.os, "replace", failing_replace)
    with pytest.raises(OSError):
        est.save_universe(path, [(3, "CCC")])
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["universe_last.json"]


def test_w6_load_bad_files_give_reason(tmp_path):
    good = tmp_path / "good.json"
    est.save_universe(good, [(1, "AAA"), (2, "BBB")])
    doc = json.loads(good.read_text(encoding="utf-8"))
    cases = {}
    changed = json.loads(json.dumps(doc))
    changed["items"][0]["yahoo_symbol"] = "ZZZ"  # sha niezmienione
    cases["sha"] = json.dumps(changed)
    cases["json"] = "{nie-json"
    del_fields = {k: v for k, v in doc.items() if k != "items"}
    cases["pola"] = json.dumps(del_fields)
    for reason, text in cases.items():
        path = tmp_path / f"{reason}.json"
        path.write_text(text, encoding="utf-8")
        got = est.load_universe(path)
        assert got.items is None and got.reason == reason
    missing = est.load_universe(tmp_path / "nie-ma.json")
    assert missing.items is None and missing.reason == "brak"
    assert est.load_universe(good).items == [(1, "AAA"), (2, "BBB")]  # kontrolka dodatnia


# --- helpery przebiegu przez CLI --------------------------------------------

def run_cli(monkeypatch, argv, *, conn=None, universe=(), http_get=None, db_down=False):
    """Uruchom `estimates` z atrapami; zwraca kod wyjscia. Zapis uniwersum jest prawdziwy."""
    def failing(**kw):
        raise OSError("synthetic")

    monkeypatch.setattr(run_p3, "get_connection", failing if db_down else (lambda **kw: conn))
    monkeypatch.setattr(run_p3, "select_estimates_universe", lambda cur, ids=None: list(universe))
    if http_get is None:
        def http_get(url, params=None, timeout=None):
            raise AssertionError("http_get nie moze byc wywolany")
    monkeypatch.setattr(run_p3, "run_estimates", functools.partial(
        est.run_estimates, http_get=http_get, sleep=lambda s: None))
    with pytest.raises(SystemExit) as excinfo:
        run_p3.main(list(argv))
    return excinfo.value.code


@pytest.fixture
def inserts(monkeypatch):
    calls = []

    def fake_insert(cur, instrument_id, entry, parsed, sha):
        calls.append(instrument_id)
        return est.InsertSummary(len(calls), len(parsed.rows))

    monkeypatch.setattr(est, "insert_snapshot", fake_insert)
    return calls


def ok_get(url, params=None, timeout=None):
    return response(BODY, 200)


def test_w7_save_only_for_full_nonempty_universe(monkeypatch, tmp_path, inserts):
    raw = tmp_path / "raw"
    path = tmp_path / "universe_last.json"
    argv = ["estimates", "--archive-root", str(raw)]
    # z --instrument-ids: nie zapisuje
    code = run_cli(monkeypatch, argv + ["--instrument-ids", "1"], conn=FakeConn(),
                   universe=[(1, "AAA")], http_get=ok_get)
    assert code == 0 and not path.exists()
    # puste uniwersum: nie zapisuje
    assert run_cli(monkeypatch, argv, conn=FakeConn(), universe=[]) == 2
    assert not path.exists()
    # kontrolka dodatnia: pelne niepuste uniwersum zapisuje
    assert run_cli(monkeypatch, argv, conn=FakeConn(), universe=[(1, "AAA"), (2, "BBB")],
                   http_get=ok_get) == 0
    assert est.load_universe(path).items == [(1, "AAA"), (2, "BBB")]
    # kolejny przebieg z --instrument-ids nie nadpisuje pelnego pliku
    before = path.read_bytes()
    run_cli(monkeypatch, argv + ["--instrument-ids", "2"], conn=FakeConn(),
            universe=[(2, "BBB")], http_get=ok_get)
    assert path.read_bytes() == before


# --- A3/A4: tryb awaryjny ---------------------------------------------------

def make_universe_file(tmp_path, pairs=((1, "AAA"), (2, "BBB"))):
    est.save_universe(tmp_path / "universe_last.json", list(pairs))


def test_w8_emergency_run(monkeypatch, tmp_path, capsys, inserts):
    make_universe_file(tmp_path)
    raw = tmp_path / "raw"
    code = run_cli(monkeypatch, ["estimates", "--db-wait-s", "0", "--archive-root", str(raw)],
                   db_down=True, http_get=ok_get)
    out = capsys.readouterr().out
    assert code == 4
    assert inserts == []  # zero wywolan insert_snapshot
    metas = sorted(raw.rglob("*.meta.json"))
    assert len(metas) == 2 and len(list(raw.rglob("*.body"))) == 2
    ids = sorted(json.loads(p.read_bytes())["instrument_id"] for p in metas)
    assert ids == [1, 2]
    stored = est.load_universe(tmp_path / "universe_last.json")
    lines = out.splitlines()
    mode = [line for line in lines if line.startswith("FPE-RUN: tryb awaryjny")]
    assert mode == [f"FPE-RUN: tryb awaryjny {DOT} uniwersum z {stored.saved_at_utc} {DOT} "
                    f"wiek 0 d {DOT} symbole 2"]
    # linia trybu przed linia podsumowania
    summary_idx = next(i for i, line in enumerate(lines) if line.startswith("FPE-RUN: symbole"))
    assert lines.index(mode[0]) < summary_idx
    assert f"w bazie 0 {DOT} tylko archiwum 2" in out
    assert f"FPE-STAN: bledy 0 {DOT} 429 0 {DOT} pominiete 0 {DOT} tylko archiwum (baza) 2 {DOT} " \
           "tryb awaryjny tak" in out


def test_w9_no_db_no_file_is_two(monkeypatch, tmp_path, capsys):
    raw = tmp_path / "raw"
    code = run_cli(monkeypatch, ["estimates", "--db-wait-s", "0", "--archive-root", str(raw)],
                   db_down=True)  # domyslny http_get zglasza AssertionError, gdyby byl wywolany
    out = capsys.readouterr().out
    assert code == 2
    assert not raw.exists()
    assert "(brak)" in out and "FPE-STAN:" in out


# --- A5: kody wyjscia -------------------------------------------------------

def summary_with(results, **over):
    errors = sum(1 for r in results if r.error is not None)
    base = dict(symbols=len(results), snapshots=sum(1 for r in results if r.archived),
                in_db=sum(1 for r in results if r.in_db), archive_only=0, errors=errors,
                rate_limited=0, skipped=0, archive_root="X", results=tuple(results))
    base.update(over)
    return est.RunSummary(**base)


def sym(i, **kw):
    base = dict(instrument_id=i, symbol=f"S{i}", http_status=200, rows=0, status="ok",
                raw_ref="r", meta_ref="m", in_db=True, archived=True)
    base.update(kw)
    return est.SymbolResult(**base)


def run_with_summary(monkeypatch, summary, universe):
    conn = FakeConn()
    monkeypatch.setattr(run_p3, "get_connection", lambda **kw: conn)
    monkeypatch.setattr(run_p3, "select_estimates_universe", lambda cur, ids=None: list(universe))
    monkeypatch.setattr(run_p3, "run_estimates", lambda *a, **kw: summary)
    monkeypatch.setattr(run_p3, "save_universe", lambda *a, **kw: None)
    with pytest.raises(SystemExit) as excinfo:
        run_p3.main(["estimates"])
    return excinfo.value.code


def test_w10_exit_code_table(monkeypatch, capsys):
    db_err = dict(status="blad", error="baza: OperationalError", in_db=False, db_failed=True)
    http_404 = dict(status="blad", error="HTTP 404", http_status=404)
    emergency = summary_with([sym(1, in_db=False), sym(2, in_db=False)], emergency=True)
    cases = [
        ("4 awaryjny", emergency, 4),
        ("4 blad bazy w trakcie", summary_with([sym(1, **db_err), sym(2)]), 4),
        ("1+4", summary_with([sym(1, **db_err), sym(2, **http_404)]), 1),
        ("0", summary_with([sym(1), sym(2)]), 0),
        # blad 'baza:' bez archiwum nie moze zniknac z obu licznikow -> 1, nie 0
        ("1 baza bez archiwum", summary_with([sym(1, archived=False, **db_err), sym(2)]), 1),
    ]
    for name, summary, expected in cases:
        assert run_with_summary(monkeypatch, summary, [(1, "A"), (2, "B")]) == expected, name
        out = capsys.readouterr().out
        assert "FPE-STAN:" in out, name
    # (2): puste uniwersum
    assert run_with_summary(monkeypatch, summary_with([]), []) == 2
    assert "FPE-STAN:" in capsys.readouterr().out
    # liczniki w linii stanow dla 1+4: blad HTTP liczy sie jako blad, blad bazy jako archiwum
    run_with_summary(monkeypatch, cases[2][1], [(1, "A"), (2, "B")])
    out = capsys.readouterr().out
    assert f"FPE-STAN: bledy 1 {DOT} 429 0 {DOT} pominiete 0 {DOT} tylko archiwum (baza) 1 {DOT} " \
           "tryb awaryjny nie" in out


class DbDownConn(FakeConn):
    """Baza pada przy zapisie symbolu (po udanym odczycie uniwersum)."""

    def cursor(self):
        raise RuntimeError("baza padla")


@pytest.mark.parametrize("status, body, prefix", [
    (404, b'{"finance":{"error":"x"}}', "HTTP 404"),
    (200, b"nie json", "odczyt: bledny JSON"),
])
def test_w10b_fetch_or_read_error_with_db_error_is_one(tmp_path, status, body, prefix):
    # Recenzja Codex V3: blad pobrania/odczytu tego samego symbolu nie moze zniknac
    # pod bledem bazy (1 > 4).
    summary = est.run_estimates(DbDownConn(), [(1, "AAA")], archive_root=tmp_path,
                                http_get=lambda *a, **kw: response(body, status),
                                sleep=lambda s: None)
    (res,) = summary.results
    assert res.archived and res.db_failed and res.error.startswith(prefix)
    assert "baza: RuntimeError" in res.error
    errors = summary.errors - est.db_error_count(summary)
    db_only = est.db_only_count(summary)
    assert (errors, db_only) == (1, 1)
    assert run_p3._fpe_exit_code(errors, 0, 0, db_only) == 1


def test_w11_emergency_with_ids_intersects_file(monkeypatch, tmp_path, capsys, inserts):
    make_universe_file(tmp_path)
    raw = tmp_path / "raw"
    asked = []

    def http_get(url, params=None, timeout=None):
        asked.append(params["symbol"])
        return response(BODY, 200)

    code = run_cli(
        monkeypatch,
        ["estimates", "--db-wait-s", "0", "--archive-root", str(raw), "--instrument-ids", "1", "9"],
        db_down=True, http_get=http_get)
    out = capsys.readouterr().out
    assert code == 1
    assert asked == ["AAA"]  # tylko przeciecie z plikiem
    assert "pominiete id=9: poza plikiem uniwersum" in out
    assert "symbole 1" in out and f"pominiete 1 {DOT} tylko archiwum (baza) 1" in out
    assert len(list(raw.rglob("*.meta.json"))) == 1


def test_parser_db_wait_default_and_override():
    args = run_p3.build_parser().parse_args(["estimates"])
    assert args.db_wait_s == 600
    assert run_p3.build_parser().parse_args(["estimates", "--db-wait-s", "0"]).db_wait_s == 0

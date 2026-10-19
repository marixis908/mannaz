"""Testy czyste dashboardu rew. 2 (brief D2, E4): sekcje S1, S3-S9.

Bez bazy i bez sieci: atrapy kursora/polaczenia i funkcji modulow (monkeypatch).
Syntetyczne tickery (AAA, BBB, ZZZ) i rachunki (ACC1 111) - zadnych danych portfela.

Dla kazdej sekcji: kontrolka dodatnia (zrodlo z danymi -> wartosc i konwencja
`data-conv`) i ujemna (zrodlo puste -> `brak pomiaru`, bez 0/NaN w HTML).
"""

from __future__ import annotations

import inspect
import json
import re
import sys
from datetime import date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

_DASH = Path(__file__).resolve().parent.parent / "scripts" / "dashboard"
if str(_DASH) not in sys.path:
    sys.path.insert(0, str(_DASH))

import generate  # noqa: E402
import model  # noqa: E402

from mannaz import fifo as fifo_mod  # noqa: E402
from mannaz.cycle import BuyBelowStopEvent  # noqa: E402
from mannaz.satellite import run_satellite_stage as REAL_STAGE  # noqa: E402

D = date(2026, 10, 19)
RD = date(2026, 10, 16)
PREV = date(2026, 10, 9)
DOT = chr(0xB7)  # tylko na wypadek potrzeby; plik jest ASCII

PASS = model.ProvenanceResult(passed=True, overall_status="PASS", tests=[], failed_tests=[], note=None)


# ---------------------------------------------------------------------------
# Parser HTML (stdlib) i atrapy
# ---------------------------------------------------------------------------

_VOID = {"meta", "br", "link", "input", "img", "hr"}


class Parsed(HTMLParser):
    def __init__(self, text: str):
        super().__init__(convert_charrefs=True)
        self._stack: list[dict] = []
        self._secs: list[str | None] = []
        self.elems: list[dict] = []
        self.feed(text)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        a = dict(attrs)
        rec = {
            "tag": tag, "attrs": a, "cls": (a.get("class") or "").split(), "text": [],
            "sec": self._secs[-1] if self._secs else None, "id": a.get("id"),
        }
        if tag == "section":
            self._secs.append(a.get("id"))
        self._stack.append(rec)
        self.elems.append(rec)

    def handle_data(self, data):
        for r in self._stack:
            r["text"].append(data)

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                if tag == "section":
                    self._secs.pop()
                del self._stack[i:]
                break

    @staticmethod
    def txt(rec) -> str:
        return "".join(rec["text"]).strip()

    def by_class(self, cls: str, sec: str | None = None):
        return [r for r in self.elems if cls in r["cls"] and (sec is None or r["sec"] == sec)]

    def nums(self, sec: str | None = None):
        return self.by_class("num", sec)

    def nas(self, sec: str | None = None):
        return self.by_class("na", sec)

    def section(self, sec_id: str) -> dict:
        found = [r for r in self.elems if r["tag"] == "section" and r["id"] == sec_id]
        assert len(found) == 1, f"sekcja {sec_id}: {len(found)} wystapien"
        return found[0]

    def sec_text(self, sec_id: str) -> str:
        return self.txt(self.section(sec_id))

    def role(self, sec_id: str, role: str):
        return [r for r in self.elems if r["sec"] == sec_id and r["attrs"].get("data-role") == role]


def conv_ok(value: str | None) -> bool:
    if not value:
        return False
    parts = value.split("|")
    return len(parts) == 4 and all(p.strip() for p in parts)


def assert_nums_have_conv(p: Parsed, sec: str | None = None) -> int:
    nums = p.nums(sec)
    for r in nums:
        assert conv_ok(r["attrs"].get("data-conv")), f"num bez poprawnego data-conv: {Parsed.txt(r)!r} {r['attrs'].get('data-conv')!r}"
    return len(nums)


_ZERO_RE = re.compile(r"^-?0([.,]0+)?%?$")
_BAD_WORDS = {"NaN", "nan", "Infinity", "-Infinity"}


def assert_no_zero_nums(p: Parsed, sec: str | None = None) -> None:
    for r in p.nums(sec):
        t = Parsed.txt(r)
        assert not _ZERO_RE.match(t) and t not in _BAD_WORDS, f"brak pomiaru zapisany jako liczba: {t!r}"


CLOSED_COUNT_SRC = "risk_daily vs positions_as_of(D)"


def is_closed_count(r) -> bool:
    """Licznik pozycji zamknietych od daty oceny (ocena planisty D2-r1 pkt 3): prawdziwy
    pomiar (moze byc 0), nie "brak pomiaru" - wylaczony z asercji braku zer."""
    return CLOSED_COUNT_SRC in (r["attrs"].get("data-conv") or "")


class FakeConn:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


class FakeCur:
    """Skryptowany kursor: reguly (podciag SQL po normalizacji bialych znakow, wynik)."""

    def __init__(self, rules, connection=None):
        self.rules = rules
        self.executed: list[tuple[str, object]] = []
        self._rows: list = []
        self.connection = connection if connection is not None else FakeConn()

    def execute(self, sql, params=None):
        flat = " ".join(sql.split())
        self.executed.append((flat, params))
        for sub, res in self.rules:
            if sub in flat:
                rows = res(params) if callable(res) else res
                if isinstance(rows, Exception):
                    raise rows
                self._rows = list(rows)
                return
        raise AssertionError("nieoczekiwane SQL: " + flat[:100])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def all_select_only(self) -> bool:
        return all(s.lstrip().upper().startswith("SELECT") for s, _ in self.executed)


class _CurCtx:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self.cur

    def __exit__(self, *a):
        return False


class FakeDbConn:
    def __init__(self, rules=None):
        self.read_only = False
        self.closed = False
        self.rollbacks = 0
        self.rules = rules or []
        self.cursors: list[FakeCur] = []

    def cursor(self):
        c = FakeCur(self.rules, connection=self)
        self.cursors.append(c)
        return _CurCtx(c)

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Dane syntetyczne
# ---------------------------------------------------------------------------


def make_rr(iid, ticker, **kw):
    base = dict(
        rachunek="ACC1 111", instrument_id=iid, settlement_currency="USD", quote_currency="USD",
        risk_state="ok", stop_effective=Decimal("10.5000"), stop_source="two_n_held",
        risk_pct_satellite_capital=Decimal("0.620"), level1_breach=False, multiplier_missing=False,
        close_d=Decimal("12"), regime=None, warning=None, risk_pln=Decimal("100"), note=None,
        computed_at=datetime(2026, 10, 16, 20, 0), price_is_stale=False, price_date_used=RD,
        instrument_type="equity", is_core=False, theme="T1", name_key=iid,
        name_risk_pct=Decimal("0.620"), name_label=ticker, broker_ticker=ticker,
    )
    base.update(kw)
    return base


def make_pos(iid, ticker, **kw):
    base = dict(
        rachunek="ACC1 111", instrument_id=iid, broker_ticker=ticker, instrument_type="equity",
        is_core=False, settlement_currency="USD",
    )
    base.update(kw)
    return base


INSTR = {
    1: {"name": "Alpha Corp", "broker_ticker": "AAA", "theme": "T1"},
    2: {"name": "Beta Corp", "broker_ticker": "BBB", "theme": "T2"},
    3: {"name": "Gamma Corp", "broker_ticker": "CCC", "theme": None},
    9: {"name": "ZZZ Closed", "broker_ticker": "ZZZ", "theme": "T1"},
}

PREV_RULE = ("max(risk_date) FROM risk_daily WHERE risk_date <", [(PREV,)])


def s3s4(monkeypatch, *, d_risk=RD, risk_rows=None, open_positions=None, newer=frozenset(), events=(),
         prev_rows=None, events_exc=None, provenance=PASS, sum_open=Decimal("2.5"), themes=None):
    cur = FakeCur([("max(risk_date) FROM risk_daily WHERE risk_date <", prev_rows if prev_rows is not None else [(PREV,)])])
    calls = []

    def fake_events(c, d, prev):
        calls.append((d, prev))
        if events_exc is not None:
            raise events_exc
        return list(events)

    monkeypatch.setattr(generate, "load_buy_below_stop_events", fake_events)
    rr = risk_rows if risk_rows is not None else []
    by_key = {(r["rachunek"], r["instrument_id"], r["settlement_currency"]): r for r in rr}
    s3, s4 = generate._build_s3_s4(
        cur, D, d_risk, provenance, rr, by_key, open_positions or [], INSTR, set(newer), sum_open,
        themes if themes is not None else {"T1": Decimal("1.2")},
    )
    return s3, s4, cur, calls


def sat_of(**blocks):
    sat = {"no_d": None, "s1": S1_OK}
    sat.update(blocks)
    return sat


S1_OK = {
    "d": D, "d_source": "podane", "d_risk": RD, "risk_source": "podane", "price_n": 9, "price_N": 10,
    "kontraktowy": {"ok": True, "message": None}, "code_sha": "abc1234", "source_runs_n": 42,
    "source_runs_max_fetched_at": datetime(2026, 10, 19, 6, 30),
}


def registry_cur(total_v, total_e, viol=(), ev=(), windows=()):
    return FakeCur([
        ("FROM satellite_mandate_violations WHERE registry = 1 AND window_end <= %s GROUP BY", list(viol)),
        ("FROM satellite_mandate_events WHERE registry = 1 AND event_date <= %s GROUP BY", list(ev)),
        ("ORDER BY window_end DESC", list(windows)),
        ("SELECT count(*) FROM satellite_mandate_violations", [(total_v,)]),
        ("SELECT count(*) FROM satellite_mandate_events", [(total_e,)]),
    ])


def make_report(verified=False, with_attr=True):
    def win(frm, to, twr):
        return {
            "from": frm, "to": to, "twr": twr, "abs": {"max_dd": Decimal("0.12")},
            "mandate": {"status": "ok", "violations": ["abs"] if not verified else []},
            "bench": {
                "sp500": {"delta_w_pln": Decimal("1234.5"), "infeasible": False, "max_dd_rel": Decimal("0.05")},
                "spyi": {"delta_w_pln": Decimal("-20.25"), "infeasible": True, "max_dd_rel": Decimal("0.07")},
            },
        }

    def awin(frm, to):
        return {
            "from": frm, "to": to, "control": {"complete": True},
            "by_account": [
                {"lokalny": Decimal("100.10"), "fx": Decimal("-5.05"), "total": Decimal("95.05")},
                {"lokalny": Decimal("200.20"), "fx": Decimal("10.00"), "total": Decimal("210.20")},
            ],
            "by_currency": [{"key": "USD", "lokalny": Decimal("300.30"), "fx": Decimal("4.95"), "total": Decimal("305.25")}],
        }

    rep = {
        "S": date(2026, 3, 2),
        "windows": {"od_S": win("2026-03-02", "2026-10-19", Decimal("0.0345")), "24m": win("2024-10-19", "2026-10-19", Decimal("0.0711"))},
        "data_verified": verified,
        "data_unverified_reasons": [] if verified else ["brak kotwic M78 (test)", "przyczyna druga"],
        "gate": None,
        "alerts": {"abs": {"limit": 0.35, "current_drawdown": Decimal("0.10"), "state": "ok"}},
        "conventions": {"currency": "PLN", "benchmarks": {"sp500": {"symbol": "SPXTR", "source": "zrodlo-test"}}},
    }
    if with_attr:
        rep["attribution"] = {
            "windows": {"od_S": awin("2026-03-02", "2026-10-19"), "24m": awin("2024-10-19", "2026-10-19")},
            "conventions": "konwencja-testowa-M85",
        }
    return rep


def patch_stage(monkeypatch, report=None, stage_exc=None, load_exc=None):
    calls: dict = {"stage": [], "load": []}

    def fake_load(path):
        calls["load"].append(path)
        if load_exc is not None:
            raise load_exc
        return ["snap"]

    def fake_stage(*args, **kw):
        calls["stage"].append((args, kw))
        if stage_exc is not None:
            raise stage_exc
        return SimpleNamespace(report=report)

    monkeypatch.setattr(generate, "load_account_values_dir", fake_load)
    monkeypatch.setattr(generate, "run_satellite_stage", fake_stage)
    return calls


def s7_cur(rows_by_iid):
    def handler(params):
        ids, d1, d2 = params
        out = []
        for iid in ids:
            out.extend((iid, a, k, v) for (a, k, v) in rows_by_iid.get(iid, []))
        return out

    return FakeCur([("FROM archetype_assignments", handler)])


@pytest.fixture
def sat_full(monkeypatch):
    """Pelna atrapa: wszystkie sekcje z danymi, zbudowana przez funkcje generatora."""
    rr = [make_rr(1, "AAA"), make_rr(2, "BBB", level1_breach=True, risk_pct_satellite_capital=Decimal("1.250"), name_risk_pct=Decimal("1.250"))]
    ops = [make_pos(1, "AAA"), make_pos(2, "BBB")]
    ev = BuyBelowStopEvent(
        broker_ticker="AAA", rachunek="ACC1 111", settlement_currency="USD", quote_currency="USD",
        txn_date=date(2026, 10, 15), qty=Decimal("3"), price_quote=Decimal("9.8765"), fx_missing=False,
        stop_before=Decimal("10.5000"), stop_before_date=PREV, stop_before_source="two_n_held",
        stop_d=Decimal("10.5000"), stop_d_source="two_n_held",
    )
    s3, s4, _, _ = s3s4(monkeypatch, risk_rows=rr, open_positions=ops, events=[ev])
    cur = registry_cur(
        3, 4,
        viol=[("abs", 2, date(2025, 1, 2), date(2026, 9, 30), "niezweryfikowane", datetime(2026, 10, 1, 7, 0))],
        ev=[("abs", 1, 2, date(2026, 5, 1), date(2026, 9, 1), "niezweryfikowane", datetime(2026, 10, 1, 7, 0))],
        windows=[("abs", date(2025, 1, 2), date(2026, 9, 30), Decimal("0.40"), Decimal("0.35"), "niezweryfikowane")],
    )
    s5a = generate._fetch_registry1(cur, D)
    patch_stage(monkeypatch, report=make_report(verified=False))
    s5b, s9 = generate._build_s5b_s9(FakeDbConn(), D, "dir-test")
    s7 = generate._build_s7(s7_cur({1: [("compounder", "key-a", "v2")]}), D, ops, INSTR)
    return sat_of(s3=s3, s4=s4, s5a=s5a, s5b=s5b, s7=s7, s9=s9)


@pytest.fixture
def sat_empty(monkeypatch):
    """Atrapy pustych zrodel: brak oceny ryzyka, pusty rejestr 1, wyjatek modulu, brak pozycji."""
    s3, s4, _, _ = s3s4(monkeypatch, d_risk=None)
    s5a = generate._fetch_registry1(registry_cur(0, 0), D)
    patch_stage(monkeypatch, stage_exc=RuntimeError("TAJNE"))
    s5b, s9 = generate._build_s5b_s9(FakeDbConn(), D, None)
    s7 = generate._build_s7(s7_cur({}), D, [], INSTR)
    return sat_of(s3=s3, s4=s4, s5a=s5a, s5b=s5b, s7=s7, s9=s9)


def payload_of(sat):
    return {"meta": {"pin": {"max_source_run_id": 77, "risk_computed_at": None}}, "satellites": sat}


def render(sat) -> str:
    return generate.render_template(payload_of(sat))


# ---------------------------------------------------------------------------
# S1
# ---------------------------------------------------------------------------


def test_s1_positive_d_risk_date_sha_runs_with_conv():
    p = Parsed(model.render_s1_html(sat_of()))
    t = p.sec_text("sec-s1")
    assert "2026-10-19" in t and "2026-10-16" in t and "abc1234" in t
    nums = {Parsed.txt(r): r["attrs"]["data-conv"] for r in p.nums("sec-s1")}
    assert "9/10" in nums and "42" in nums
    assert assert_nums_have_conv(p, "sec-s1") == 2
    assert "source_runs" in nums["42"] and "2026-10-19" in nums["42"]
    assert p.role("sec-s1", "conv-footer")


def test_s1_negative_no_d_all_sections_brak_and_no_exception():
    conn = FakeDbConn([("FROM source_runs", [(5, datetime(2026, 10, 19, 6, 30), 77)])])
    payload, counts = generate.build_no_date_payload(conn)
    assert counts["no_d"] is True
    p = Parsed(generate.render_template(payload))
    for sec in ("sec-s1", "sec-s3", "sec-s4", "sec-s5a", "sec-s5b", "sec-s6", "sec-s7", "sec-s8", "sec-s9"):
        assert generate.BRAK_NO_D in p.sec_text(sec), sec
    assert generate.BRAK_NO_D == "brak pomiaru: brak daty D wg B-36 (brak kompletnej daty w cenach i KONTRAKTOWY)"
    assert sorted(counts["brak_sections"]) == sorted(model.SECTION_NAMES)
    assert_no_zero_nums(p)
    assert all(c.all_select_only() for c in conn.cursors)


def test_main_no_d_exit0_html_written_readonly(monkeypatch, tmp_path, capsys):
    fake = FakeDbConn([("FROM source_runs", [(5, datetime(2026, 10, 19, 6, 30), 77)])])
    monkeypatch.setattr(generate, "get_connection", lambda: fake)
    monkeypatch.setattr(generate, "resolve_default_risk_date", lambda conn: None)
    monkeypatch.setattr(generate, "code_sha", lambda: "abc1234")
    out = tmp_path / "o" / "d.html"
    rc = generate.main(["--out", str(out)])
    assert rc == 0 and out.is_file()
    assert fake.read_only is True and fake.closed is True
    assert generate.BRAK_NO_D in out.read_text(encoding="utf-8")
    so = capsys.readouterr().out
    assert "Brak daty D wg B-36" in so and "Sekcje brak pomiaru: 8" in so


@pytest.mark.parametrize("argv_extra, d_src, r_src", [
    ([], "domyslne B-36", "domyslne: max(risk_date) <= D"),
    (["--date", "2026-10-19", "--risk-date", "2026-10-16"], "podane", "podane"),
])
def test_main_date_sources_passed_to_builder(monkeypatch, tmp_path, argv_extra, d_src, r_src):
    fake = FakeDbConn()
    seen = {}
    monkeypatch.setattr(generate, "get_connection", lambda: fake)
    monkeypatch.setattr(generate, "resolve_default_risk_date", lambda conn: D)
    monkeypatch.setattr(generate, "_default_risk_date", lambda cur, d: RD)

    def fake_build(conn, d, d_risk, d_source, risk_source, account_values_dir):
        seen.update(d=d, d_risk=d_risk, d_source=d_source, risk_source=risk_source, avd=account_values_dir)
        sat = sat_of()
        counts = dict(before=1, after=1, sat=1, core=0, fut=0, t27_ok=1, t27_stale=0, t27_incomplete=0,
                      provenance_overall="PASS", newer_positions=0, s5b_status="brak", s5b_data_verified=None,
                      brak_sections=["s6", "s8"], provenance_tests=[])
        return payload_of(sat), counts

    monkeypatch.setattr(generate, "build_dashboard_payload", fake_build)
    out = tmp_path / "x.html"
    assert generate.main(["--out", str(out), "--account-values-dir", "adir"] + argv_extra) == 0
    assert seen == dict(d=D, d_risk=RD, d_source=d_src, risk_source=r_src, avd="adir")


def test_s1_and_s3_s4_no_assessment_le_d(monkeypatch):
    s3, s4, _, _ = s3s4(monkeypatch, d_risk=None)
    for blk in (s3, s4):
        assert blk == {"status": "brak", "reason": "brak pomiaru: brak oceny ryzyka <= D"}
    sat = sat_of(s3=s3, s4=s4)
    p = Parsed(model.render_s3_html(sat) + model.render_s4_html(sat))
    assert "brak pomiaru: brak oceny ryzyka <= D" in p.sec_text("sec-s3")
    assert "brak pomiaru: brak oceny ryzyka <= D" in p.sec_text("sec-s4")
    assert p.nums() == []
    s1 = dict(S1_OK, d_risk=None, risk_source=None)
    p1 = Parsed(model.render_s1_html(sat_of() | {"s1": s1}))
    assert any(Parsed.txt(r).startswith("brak pomiaru") for r in p1.nas("sec-s1"))


# ---------------------------------------------------------------------------
# S3
# ---------------------------------------------------------------------------


def test_s3_positive_pct_per_name_with_conv(monkeypatch):
    rr = [make_rr(1, "AAA", name_risk_pct=Decimal("0.620")), make_rr(2, "BBB", level1_breach=True,
          risk_pct_satellite_capital=Decimal("1.250"), name_risk_pct=Decimal("1.250"))]
    s3, _, _, _ = s3s4(monkeypatch, risk_rows=rr, open_positions=[make_pos(1, "AAA"), make_pos(2, "BBB")])
    p = Parsed(model.render_s3_html(sat_of(s3=s3)))
    assert s3["limits"] == {"l1": Decimal(1), "l2": Decimal(3), "l3": Decimal(15)}
    texts = {Parsed.txt(r) for r in p.nums("sec-s3")}
    assert {"0,620%", "1,250%"} <= texts
    assert "2,50% / 15%" in texts  # poziom 3: suma otwartych ryzyk z oceny
    assert assert_nums_have_conv(p, "sec-s3") >= 6
    for r in p.nums("sec-s3"):
        assert r["attrs"]["data-conv"].endswith("|2026-10-16") or "licznik" in r["attrs"]["data-conv"]
    sec = p.sec_text("sec-s3")
    assert "AAA" in sec and "BBB" in sec and "NARUSZENIE L1" in sec


def test_s3_negative_empty_risk_rows_brak_no_zero(monkeypatch):
    s3, _, _, _ = s3s4(monkeypatch, risk_rows=[], open_positions=[], sum_open=None, themes={})
    assert s3["names"] == [] and s3["positions"] == []
    p = Parsed(model.render_s3_html(sat_of(s3=s3)))
    other = [r for r in p.nums("sec-s3") if not is_closed_count(r)]
    assert other == []
    assert [Parsed.txt(r) for r in p.nums("sec-s3") if is_closed_count(r)] == ["0"]  # licznik zamknietych = 0 (pomiar)
    assert len(p.nas("sec-s3")) >= 3
    assert all(Parsed.txt(r).startswith("brak pomiaru") for r in p.nas("sec-s3"))


def test_s3_negative_provenance_fail_brak_with_reason(monkeypatch):
    bad = model.ProvenanceResult(passed=False, overall_status="FAIL", tests=[], failed_tests=["test 2"], note=None)
    s3, s4, _, _ = s3s4(monkeypatch, provenance=bad, risk_rows=[make_rr(1, "AAA")])
    assert s3["status"] == "brak" and s3["reason"].startswith("brak pomiaru: sekcja ryzyka, pochodzenie FAIL")
    p = Parsed(model.render_s3_html(sat_of(s3=s3)))
    assert p.nums() == []


# ---------------------------------------------------------------------------
# S4
# ---------------------------------------------------------------------------


def _event(**kw):
    base = dict(
        broker_ticker="AAA", rachunek="ACC1 111", settlement_currency="USD", quote_currency="USD",
        txn_date=date(2026, 10, 15), qty=Decimal("3"), price_quote=Decimal("9.8765"), fx_missing=False,
        stop_before=Decimal("10.5000"), stop_before_date=PREV, stop_before_source="two_n_held",
        stop_d=Decimal("10.5000"), stop_d_source="two_n_held",
    )
    base.update(kw)
    return BuyBelowStopEvent(**base)


def test_s4_positive_ratchet_tag_and_buy_below_stop_event_with_date(monkeypatch):
    s3, s4, cur, calls = s3s4(monkeypatch, risk_rows=[make_rr(1, "AAA")], open_positions=[make_pos(1, "AAA")], events=[_event()])
    assert calls == [(RD, PREV)]  # okno (prev, risk_date]
    p = Parsed(model.render_s4_html(sat_of(s4=s4)))
    t = p.sec_text("sec-s4")
    assert "zapadka 2N" in t and "two_n_held" in t
    assert "dokupienie ponizej stopu" in t and "2026-10-15" in t
    assert "okno dokupien: (2026-10-09, 2026-10-16]" in t
    texts = {Parsed.txt(r): r["attrs"]["data-conv"] for r in p.nums("sec-s4")}
    assert "10,5000" in texts and "9,8765" in texts
    assert texts["10,5000"].startswith("USD|cena split_adj|")
    assert assert_nums_have_conv(p, "sec-s4") >= 3
    assert "ACC1 111" not in t and "111" not in t  # numer rachunku nie wycieka
    assert cur.all_select_only()


def test_s4_negative_prev_none_is_brak_not_empty_list(monkeypatch):
    s3, s4, _, calls = s3s4(monkeypatch, risk_rows=[make_rr(1, "AAA")], open_positions=[make_pos(1, "AAA")],
                            prev_rows=[(None,)], events=[])
    assert s4["prev"] is None and calls == [(RD, None)]
    p = Parsed(model.render_s4_html(sat_of(s4=s4)))
    t = p.sec_text("sec-s4")
    assert "brak pomiaru: brak poprzedniej oceny ryzyka" in t
    assert "Brak zdarzen w oknie" not in t and "okno dokupien: (" not in t
    assert not any(Parsed.txt(r) == "-" for r in p.elems if r["tag"] == "td")  # flaga to brak, nie "-"
    assert_no_zero_nums(p)


def test_s4_negative_event_loader_exception_brak_with_class_name_only(monkeypatch):
    s3, s4, cur, _ = s3s4(monkeypatch, risk_rows=[make_rr(1, "AAA")], open_positions=[make_pos(1, "AAA")],
                          events_exc=RuntimeError("TAJNE tresc"))
    assert s4["events"] is None and s4["events_error"] == "RuntimeError"
    assert cur.connection.rollbacks == 1
    h = model.render_s4_html(sat_of(s4=s4))
    p = Parsed(h)
    assert "brak pomiaru: odczyt zdarzen dokupienia nieudany (RuntimeError)" in p.sec_text("sec-s4")
    assert "TAJNE" not in h
    assert "Brak zdarzen w oknie" not in h


def test_s4_empty_event_list_with_prev_is_explicit_empty_not_brak(monkeypatch):
    _, s4, _, _ = s3s4(monkeypatch, risk_rows=[make_rr(1, "AAA")], open_positions=[make_pos(1, "AAA")], events=[])
    assert "Brak zdarzen w oknie" in Parsed(model.render_s4_html(sat_of(s4=s4))).sec_text("sec-s4")


# ---------------------------------------------------------------------------
# Q1
# ---------------------------------------------------------------------------


def _q1_cur(txs):
    """txs: (rachunek, instrument_id, row_type, date). Filtruje jak SQL (zakres dat + row_type = ANY)."""
    def handler(params):
        lo, hi, types = params
        return sorted({(r, i) for (r, i, rt, dt) in txs if lo < dt <= hi and rt in types})

    return FakeCur([("FROM transactions WHERE transaction_date >", handler)])


def test_q1_positive_position_in_risk_set_without_txn_has_stop(monkeypatch):
    monkeypatch.setattr(generate, "positions_as_of", lambda cur, d: [{"rachunek": "ACC1 111", "instrument_id": 1}])
    cur = _q1_cur([("ACC1 111", 1, "kupno", date(2026, 10, 1))])  # przed ocena: poza oknem
    newer = generate._positions_newer_than_risk(cur, D, RD, {("ACC1 111", 1)})
    assert newer == set()
    s3, s4, _, _ = s3s4(monkeypatch, risk_rows=[make_rr(1, "AAA")], open_positions=[make_pos(1, "AAA")], newer=newer)
    assert s3["positions"][0]["state"] == "ok" and s4["positions"][0]["stop_effective"] == Decimal("10.5000")
    p = Parsed(model.render_s4_html(sat_of(s4=s4)))
    assert "10,5000" in {Parsed.txt(r) for r in p.nums("sec-s4")}


def test_q1_negative_a_absent_in_positions_as_of_risk_date(monkeypatch):
    monkeypatch.setattr(generate, "positions_as_of", lambda cur, d: [])
    newer = generate._positions_newer_than_risk(_q1_cur([]), D, RD, {("ACC1 111", 2)})
    assert newer == {("ACC1 111", 2)}
    rr = [make_rr(2, "BBB", stop_effective=Decimal("99.9999"), risk_pct_satellite_capital=Decimal("7.777"))]
    s3, s4, _, _ = s3s4(monkeypatch, risk_rows=rr, open_positions=[make_pos(2, "BBB")], newer=newer)
    msg = "brak pomiaru: pozycja nowsza niz ocena ryzyka 2026-10-16"
    for blk, fn, sec in ((s3, model.render_s3_html, "sec-s3"), (s4, model.render_s4_html, "sec-s4")):
        assert blk["positions"][0]["state"] == "nowsza" and blk["positions"][0]["reason"] == msg
        h = fn(sat_of(**{"s3" if sec == "sec-s3" else "s4": blk}))
        p = Parsed(h)
        assert msg in p.sec_text(sec)
        assert "99,9999" not in h
        assert all(Parsed.txt(r) not in ("7,777%",) for r in p.nums(sec))
        # w wierszu pozycji nie ma liczb (stop/ryzyko): zadne 0 w miejscu stopu/ryzyka
        rows = [r for r in p.elems if r["tag"] == "tr" and "Beta Corp" in Parsed.txt(r)]
        assert len(rows) == 1 and "0" not in [Parsed.txt(x) for x in p.nums(sec) if not is_closed_count(x)]


@pytest.mark.parametrize("row_type", list(fifo_mod._FIFO_ROW_TYPES))
def test_q1_negative_b_fifo_row_type_in_window_marks_newer(monkeypatch, row_type):
    monkeypatch.setattr(generate, "positions_as_of", lambda cur, d: [{"rachunek": "ACC1 111", "instrument_id": 1}])
    cur = _q1_cur([("ACC1 111", 1, row_type, date(2026, 10, 17))])
    assert generate._positions_newer_than_risk(cur, D, RD, {("ACC1 111", 1)}) == {("ACC1 111", 1)}
    assert set(cur.executed[0][1][2]) == set(fifo_mod._FIFO_ROW_TYPES)  # parametr SQL = _FIFO_ROW_TYPES


def test_q1_negative_b_sale_exactly_on_d_counts_but_on_risk_date_does_not(monkeypatch):
    monkeypatch.setattr(generate, "positions_as_of", lambda cur, d: [{"rachunek": "ACC1 111", "instrument_id": 1}])
    on_d = _q1_cur([("ACC1 111", 1, "sprzedaz", D)])
    on_rd = _q1_cur([("ACC1 111", 1, "sprzedaz", RD)])
    assert generate._positions_newer_than_risk(on_d, D, RD, {("ACC1 111", 1)}) == {("ACC1 111", 1)}  # (RD, D]
    assert generate._positions_newer_than_risk(on_rd, D, RD, {("ACC1 111", 1)}) == set()


def test_q1_c_non_fifo_row_type_does_not_mark_newer(monkeypatch):
    other = "dywidenda"
    assert other not in fifo_mod._FIFO_ROW_TYPES
    monkeypatch.setattr(generate, "positions_as_of", lambda cur, d: [{"rachunek": "ACC1 111", "instrument_id": 1}])
    cur = _q1_cur([("ACC1 111", 1, other, date(2026, 10, 17))])
    assert generate._positions_newer_than_risk(cur, D, RD, {("ACC1 111", 1)}) == set()
    s3, s4, _, _ = s3s4(monkeypatch, risk_rows=[make_rr(1, "AAA")], open_positions=[make_pos(1, "AAA")], newer=set())
    assert s4["positions"][0]["state"] == "ok"


@pytest.mark.parametrize("d_risk", [None, D, date(2026, 10, 25)])
def test_q1_no_comparison_when_risk_date_missing_or_not_before_d(monkeypatch, d_risk):
    monkeypatch.setattr(generate, "positions_as_of", lambda cur, d: pytest.fail("nie powinno czytac"))
    cur = FakeCur([])
    assert generate._positions_newer_than_risk(cur, D, d_risk, {("ACC1 111", 1)}) == set()
    assert cur.executed == []


def test_q1_d_position_closed_after_risk_date_not_in_s3_s4_rows(monkeypatch):
    # ZZZ: wiersz w risk_daily z oceny, ale brak w pozycjach otwartych na D (zamknieta po ocenie)
    rr = [make_rr(1, "AAA"), make_rr(9, "ZZZ")]
    s3, s4, _, _ = s3s4(monkeypatch, risk_rows=rr, open_positions=[make_pos(1, "AAA")])
    for blk in (s3, s4):
        assert [r["name"] for r in blk["positions"]] == ["Alpha Corp"]
    h4 = model.render_s4_html(sat_of(s4=s4))
    assert "ZZZ" not in h4
    h3 = model.render_s3_html(sat_of(s3=s3))
    per_pos = h3.split("Per pozycja na D", 1)[1]
    assert "ZZZ" not in per_pos
    assert "ZZZ" in h3  # agregat per emitent czytany z oceny (opisane w README), nie z wierszy pozycji


def test_q1_end_to_end_mixed_positive_and_negative_in_same_section(monkeypatch):
    rr = [make_rr(1, "AAA"), make_rr(2, "BBB", stop_effective=Decimal("55.5555"))]
    s3, s4, _, _ = s3s4(monkeypatch, risk_rows=rr, open_positions=[make_pos(1, "AAA"), make_pos(2, "BBB")],
                        newer={("ACC1 111", 2)})
    h = model.render_s4_html(sat_of(s4=s4))
    p = Parsed(h)
    assert "10,5000" in {Parsed.txt(r) for r in p.nums("sec-s4")}
    assert "55,5555" not in h and "pozycja nowsza niz ocena ryzyka 2026-10-16" in h
    assert_nums_have_conv(p, "sec-s4")


# ---------------------------------------------------------------------------
# S5a
# ---------------------------------------------------------------------------

_SUGGESTION_RE = re.compile(r"\b(sprzedaj|kup|zmniejsz|zwieksz)\w*", re.IGNORECASE)


def test_s5a_positive_counts_limits_status_and_alert():
    cur = registry_cur(
        3, 4,
        viol=[("abs", 2, date(2025, 1, 2), date(2026, 9, 30), "niezweryfikowane", datetime(2026, 10, 1, 7, 0)),
              ("rel_spyi", 1, date(2025, 3, 3), date(2026, 8, 31), "zweryfikowane", datetime(2026, 10, 1, 7, 0))],
        ev=[("abs", 1, 2, date(2026, 5, 1), date(2026, 9, 1), "niezweryfikowane", datetime(2026, 10, 1, 7, 0))],
        windows=[("abs", date(2025, 1, 2), date(2026, 9, 30), Decimal("0.40"), Decimal("0.35"), "niezweryfikowane")],
    )
    s5a = generate._fetch_registry1(cur, D)
    assert s5a["status"] == "ok" and s5a["limits"] == {"abs": 0.35, "rel": 0.20}
    assert cur.all_select_only()
    assert all(params == (D,) for sql, params in cur.executed if "<= %s" in sql)
    p = Parsed(model.render_s5a_html(sat_of(s5a=s5a)))
    t = p.sec_text("sec-s5a")
    nums = {Parsed.txt(r) for r in p.nums("sec-s5a")}
    assert {"35%", "20%", "2", "1", "2", "40,00%"} <= nums
    assert "niezweryfikowane" in t and "zweryfikowane" in t
    assert "NARUSZENIE MANDATU" in t  # alert w UI
    assert "brak naruszen w rejestrze 1" in t  # rel_sp500 bez wierszy przy niepustej tabeli
    assert assert_nums_have_conv(p, "sec-s5a") >= 8
    assert not _SUGGESTION_RE.search(t)


def test_s5a_negative_empty_table_brak_pomiaru_rejestr_pusty():
    s5a = generate._fetch_registry1(registry_cur(0, 0), D)
    p = Parsed(model.render_s5a_html(sat_of(s5a=s5a)))
    t = p.sec_text("sec-s5a")
    assert t.count("brak pomiaru: rejestr 1 pusty") == 6  # 3 komponenty x (naruszenia, zdarzenia)
    assert "NARUSZENIE" not in t and "brak naruszen w rejestrze 1" not in t
    assert {Parsed.txt(r) for r in p.nums("sec-s5a")} <= {"35%", "20%"}  # tylko limity, zadnych zer
    assert_no_zero_nums(p, "sec-s5a")
    assert not _SUGGESTION_RE.search(t)


def test_s5a_negative_nonempty_table_without_component_rows():
    s5a = generate._fetch_registry1(registry_cur(5, 5), D)  # tabele niepuste, brak wierszy registry=1 <= D
    p = Parsed(model.render_s5a_html(sat_of(s5a=s5a)))
    t = p.sec_text("sec-s5a")
    assert t.count("brak naruszen w rejestrze 1") == 6
    assert "rejestr 1 pusty" not in t and "NARUSZENIE" not in t
    assert_no_zero_nums(p, "sec-s5a")
    assert not _SUGGESTION_RE.search(t)


def test_s5a_negative_read_error_brak_with_class_name_and_rollback():
    cur = FakeCur([("SELECT count(*)", RuntimeError("TAJNE"))])
    s5a = generate._fetch_registry1(cur, D)
    assert s5a == {"status": "brak", "reason": "brak pomiaru: odczyt rejestru 1 nieudany (RuntimeError)"}
    assert cur.connection.rollbacks == 1
    h = model.render_s5a_html(sat_of(s5a=s5a))
    assert "TAJNE" not in h and "odczyt rejestru 1 nieudany (RuntimeError)" in h


# ---------------------------------------------------------------------------
# S5b
# ---------------------------------------------------------------------------


def test_s5b_positive_windows_bench_alerts_and_unverified_status(monkeypatch):
    calls = patch_stage(monkeypatch, report=make_report(verified=False))
    conn = FakeDbConn()
    s5b, _ = generate._build_s5b_s9(conn, D, "dir-test")
    (args, kw), = calls["stage"]
    assert args == (conn, D) and kw["write"] is False and kw["out_dir"] is None and kw["snapshots"] == ["snap"]
    REAL_STAGE_SIG = inspect.signature(REAL_STAGE)
    REAL_STAGE_SIG.bind(conn, D, out_dir=None, write=False, snapshots=[])  # zgodne z prawdziwa sygnatura
    assert calls["load"] == ["dir-test"] and s5b["d"] == D
    h = model.render_s5b_html(sat_of(s5b=s5b))
    p = Parsed(h)
    tags = [Parsed.txt(r) for r in p.by_class("tag", "sec-s5b")]
    assert "niezweryfikowane" in tags and "zweryfikowane" not in tags
    status = p.role("sec-s5b", "data-status")[0]
    st = Parsed.txt(status)
    assert "niezweryfikowane" in st and "brak kotwic M78 (test)" in st and "przyczyna druga" in st
    nums = {Parsed.txt(r): r["attrs"]["data-conv"] for r in p.nums("sec-s5b")}
    assert "3,45%" in nums and "7,11%" in nums            # TWR okien od_S i 24m
    assert "1 234,50" in nums and nums["1 234,50"].startswith("PLN|")  # delta W
    assert any(k.startswith("10,00% / 35%") for k in nums)  # alert biezacy / limit
    assert any(k.startswith("12,00% / 35%") for k in nums)  # max_dd abs
    assert assert_nums_have_conv(p, "sec-s5b") >= 10
    t = p.sec_text("sec-s5b")
    assert "od_S" in t and "24m" in t and "SPXTR (zrodlo-test)" in t
    assert "benchmark niewykonalny bez finansowania" in t


def test_s5b_positive_verified_flag_shows_zweryfikowane(monkeypatch):
    patch_stage(monkeypatch, report=make_report(verified=True))
    s5b, _ = generate._build_s5b_s9(FakeDbConn(), D, None)
    p = Parsed(model.render_s5b_html(sat_of(s5b=s5b)))
    tags = [Parsed.txt(r) for r in p.by_class("tag", "sec-s5b")]
    assert "zweryfikowane" in tags and "niezweryfikowane" not in tags


def test_s5b_default_account_values_dir_is_cycle_manual_dir(monkeypatch):
    calls = patch_stage(monkeypatch, report=make_report())
    generate._build_s5b_s9(FakeDbConn(), D, None)
    assert calls["load"] == [generate.DEFAULT_MANUAL_DIR]


def test_s5b_same_d_as_s2_in_source():
    src = inspect.getsource(generate.build_dashboard_payload)
    assert "_build_s5b_s9(conn, d, account_values_dir)" in src  # to samo D co sklad (S2)


@pytest.mark.parametrize("where", ["stage", "load"])
def test_s5b_negative_exception_brak_without_message_rest_renders(monkeypatch, where):
    exc = RuntimeError("TAJNE")
    patch_stage(monkeypatch, report=make_report(), stage_exc=exc if where == "stage" else None,
                load_exc=exc if where == "load" else None)
    conn = FakeDbConn()
    s5b, s9 = generate._build_s5b_s9(conn, D, None)
    reason = "brak pomiaru: modul satelity: RuntimeError"
    assert s5b == {"status": "brak", "reason": reason} and s9 == s5b
    assert conn.rollbacks == 1
    sat = sat_of(s5b=s5b, s9=s9, s7=generate._build_s7(s7_cur({1: [("compounder", "key-a", "v2")]}), D, [make_pos(1, "AAA")], INSTR))
    h = render(sat)
    p = Parsed(h)
    assert reason in p.sec_text("sec-s5b") and reason in p.sec_text("sec-s9")
    assert "TAJNE" not in h
    assert "compounder" in p.sec_text("sec-s7")  # reszta dashboardu renderowana
    assert p.nums("sec-s5b") == [] and p.nums("sec-s9") == []


def test_s5b_negative_rollback_failure_does_not_mask_brak(monkeypatch):
    patch_stage(monkeypatch, stage_exc=ValueError("TAJNE"))

    class BadConn:
        def rollback(self):
            raise OSError("zerwane")

    s5b, s9 = generate._build_s5b_s9(BadConn(), D, None)
    assert s5b["reason"] == "brak pomiaru: modul satelity: ValueError"


# ---------------------------------------------------------------------------
# S9
# ---------------------------------------------------------------------------


def test_s9_positive_sums_local_fx_total_with_conv_and_footer(monkeypatch):
    patch_stage(monkeypatch, report=make_report())
    _, s9 = generate._build_s5b_s9(FakeDbConn(), D, None)
    w = s9["windows"]["od_S"]
    assert (w["sum_lokalny"], w["sum_fx"], w["sum_total"]) == (Decimal("300.30"), Decimal("4.95"), Decimal("305.25"))
    p = Parsed(model.render_s9_html(sat_of(s9=s9)))
    nums = {Parsed.txt(r): r["attrs"]["data-conv"] for r in p.nums("sec-s9")}
    assert {"300,30", "4,95", "305,25"} <= set(nums)
    assert nums["300,30"].startswith("PLN|") and nums["300,30"].endswith("|2026-10-19")
    assert assert_nums_have_conv(p, "sec-s9") >= 12
    foot = Parsed.txt(p.role("sec-s9", "conv-footer")[0])
    assert "PLN" in foot and "konwencja-testowa-M85" in foot and "2026-10-19" in foot
    t = p.sec_text("sec-s9")
    assert "USD" in t and "kontrola pelna" in t


@pytest.mark.parametrize("variant", ["no_key", "empty_windows"])
def test_s9_negative_no_attribution_brak_and_s5b_still_ok(monkeypatch, variant):
    rep = make_report(with_attr=False)
    if variant == "empty_windows":
        rep["attribution"] = {"windows": {}, "conventions": "x"}
    patch_stage(monkeypatch, report=rep)
    s5b, s9 = generate._build_s5b_s9(FakeDbConn(), D, None)
    assert s5b["status"] == "ok"
    assert s9 == {"status": "brak", "reason": "brak pomiaru: modul nie zwrocil atrybucji"}
    p = Parsed(model.render_s9_html(sat_of(s9=s9)))
    assert "brak pomiaru: modul nie zwrocil atrybucji" in p.sec_text("sec-s9")
    assert p.nums("sec-s9") == []


# ---------------------------------------------------------------------------
# S6, S8
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture_name", ["sat_full", "sat_empty"])
def test_s6_s8_always_brak_with_co_zasili(request, fixture_name):
    p = Parsed(render(request.getfixturevalue(fixture_name)))
    for sec, backlog in (("sec-s6", "B-89"), ("sec-s8", "B-90")):
        assert p.sec_text(sec).count("brak pomiaru") >= 1
        nas = p.nas(sec)
        assert nas and all(Parsed.txt(r).startswith("brak pomiaru") for r in nas)
        assert p.nums(sec) == []
        co = p.role(sec, "co-zasili")
        assert len(co) == 1 and backlog in Parsed.txt(co[0])
        assert len(p.role(sec, "conv-footer")) == 1


def test_s6_s8_brak_sections_listed_in_counts():
    assert "s6" in model.brak_sections(sat_of()) and "s8" in model.brak_sections(sat_of())


# ---------------------------------------------------------------------------
# S7
# ---------------------------------------------------------------------------


def test_s7_positive_active_row_archetype_key_version():
    cur = s7_cur({1: [("compounder", "key-a", "v2")]})
    s7 = generate._build_s7(cur, D, [make_pos(1, "AAA")], INSTR)
    assert cur.executed[0][1] == ([1], D, D) and cur.all_select_only()
    p = Parsed(model.render_s7_html(sat_of(s7=s7)))
    t = p.sec_text("sec-s7")
    assert "Alpha Corp" in t and "compounder" in t and "key-a" in t and "v2" in t and "T1" in t
    assert p.nas("sec-s7") == []


def test_s7_negative_no_row_and_two_active_rows():
    cur = s7_cur({3: [("a", "k1", "v1"), ("b", "k2", "v1")]})
    ops = [make_pos(1, "AAA"), make_pos(2, "BBB"), make_pos(3, "CCC")]
    s7 = generate._build_s7(cur, D, ops, INSTR)
    p = Parsed(model.render_s7_html(sat_of(s7=s7)))
    nas = [Parsed.txt(r) for r in p.nas("sec-s7")]
    assert any(x == "brak pomiaru: brak przypisania archetypu" for x in nas)
    assert any(x.startswith("brak pomiaru: niejednoznaczne przypisanie") for x in nas)
    t = p.sec_text("sec-s7")
    assert "k1" not in t and "k2" not in t  # niejednoznaczne: nie zgadujemy
    assert p.nums("sec-s7") == []
    rows = {s["name"]: s for s in s7["rows"]}
    assert rows["Gamma Corp"]["state"] == "brak" and rows["Beta Corp"]["state"] == "brak"


def test_s7_negative_no_open_positions_no_query_brak_message():
    cur = s7_cur({})
    s7 = generate._build_s7(cur, D, [], INSTR)
    assert cur.executed == [] and s7["rows"] == []
    p = Parsed(model.render_s7_html(sat_of(s7=s7)))
    assert "brak pomiaru: brak pozycji na D" in p.sec_text("sec-s7")


# ---------------------------------------------------------------------------
# Konwencja globalna, brak pomiaru nigdy jako liczba, JSON
# ---------------------------------------------------------------------------


def test_global_every_num_has_four_field_conv_and_rev1_defaults(sat_full):
    p = Parsed(render(sat_full))
    nums = p.nums()
    assert len(nums) > 0
    for r in nums:
        assert conv_ok(r["attrs"].get("data-conv")), (Parsed.txt(r), r["attrs"].get("data-conv"))
    for sec in ("sec-s1", "sec-s3", "sec-s4", "sec-s5a", "sec-s5b", "sec-s9"):
        assert p.nums(sec), sec  # kazda sekcja z liczbami je ma
    defaults = [r for r in p.elems if r["tag"] == "section" and "data-conv-default" in r["attrs"]]
    assert {r["id"] for r in defaults} >= {"sec-composition", "sec-concentration", "sec-positions", "sec-risk", "sec-contracts"}
    for r in defaults:
        assert conv_ok(r["attrs"]["data-conv-default"]), r["id"]
    for sec in ("sec-s1", "sec-s3", "sec-s4", "sec-s5a", "sec-s5b", "sec-s6", "sec-s7", "sec-s8", "sec-s9"):
        assert len(p.role(sec, "conv-footer")) == 1, sec


def test_global_brak_pomiaru_never_as_number(sat_empty):
    h = render(sat_empty)
    p = Parsed(h)
    nas = p.nas()
    assert len(nas) >= 10
    for r in nas:
        assert Parsed.txt(r).startswith("brak pomiaru"), Parsed.txt(r)
    for sec in ("sec-s1", "sec-s3", "sec-s4", "sec-s5a", "sec-s5b", "sec-s6", "sec-s7", "sec-s8", "sec-s9"):
        assert_no_zero_nums(p, sec)
        assert not re.search(r"\b(NaN|nan|Infinity)\b", p.sec_text(sec)), sec
    assert "TAJNE" not in h
    assert assert_nums_have_conv(p) >= 1  # takze w pustej atrapie kazda liczba ma konwencje (S1, limity S5a)


def test_json_embedded_parses_without_nan_infinity(sat_full):
    payload = payload_of(sat_full)
    payload["extra"] = {"a": float("nan"), "b": [float("inf"), float("-inf")], "c": Decimal("1.50"), "d": D}
    sat_full["s5b"] = dict(sat_full["s5b"], reasons=["zly </script><b>x</b> <!-- znacznik"])
    h = generate.render_template(payload)
    m = re.search(r'<script type="application/json" id="dashboard-data">(.*?)</script>', h, re.DOTALL)
    assert m, "brak osadzonego JSON"

    def boom(c):
        raise AssertionError("stala niedozwolona w JSON: " + c)

    data = json.loads(m.group(1), parse_constant=boom)
    assert data["extra"] == {"a": None, "b": [None, None], "c": "1.50", "d": "2026-10-19"}
    assert data["satellites"]["s5b"]["reasons"] == ["zly </script><b>x</b> <!-- znacznik"]
    assert {n["pct"] for n in data["satellites"]["s3"]["names"]} == {"0.620", "1.250"}  # Decimal jako string (bez float)
    assert "TAJNE" not in m.group(1)


def test_generator_rendering_is_deterministic_and_selects_only(sat_full):
    assert render(sat_full) == render(sat_full)


def test_s9_generator_copies_module_values_does_not_compute(monkeypatch):
    rep = make_report()
    for wname in ("od_S", "24m"):
        w = rep["attribution"]["windows"][wname]
        w["by_account"] = [
            {"lokalny": Decimal("10.10"), "fx": Decimal("2.20"), "total": Decimal("99.99")},
            {"lokalny": Decimal("20.20"), "fx": Decimal("-3.30"), "total": Decimal("5.55")},
        ]
        w["by_currency"] = [
            {"key": "USD", "lokalny": Decimal("1.25"), "fx": Decimal("-0.75"), "total": Decimal("0.5")},
            {"key": "EUR", "lokalny": Decimal("2.00"), "fx": Decimal("1.00"), "total": Decimal("7.77")},  # total != lokalny + fx
        ]
    patch_stage(monkeypatch, report=rep)
    _, s9 = generate._build_s5b_s9(FakeDbConn(), D, None)
    for wname in ("od_S", "24m"):
        w = s9["windows"][wname]
        # by_currency przepisane 1:1
        assert w["by_currency"] == [
            {"key": "USD", "lokalny": Decimal("1.25"), "fx": Decimal("-0.75"), "total": Decimal("0.5")},
            {"key": "EUR", "lokalny": Decimal("2.00"), "fx": Decimal("1.00"), "total": Decimal("7.77")},
        ]
        # sumy = suma pol modulu z by_account; total z modulu, nie lokalny + fx ani total - fx
        assert (w["sum_lokalny"], w["sum_fx"], w["sum_total"]) == (Decimal("30.30"), Decimal("-1.10"), Decimal("105.54"))
        assert w["sum_total"] != w["sum_lokalny"] + w["sum_fx"]
    p = Parsed(model.render_s9_html(sat_of(s9=s9)))
    texts = [Parsed.txt(r) for r in p.nums("sec-s9")]
    for expected in ("30,30", "-1,10", "105,54", "1,25", "-0,75", "0,50", "2,00", "1,00", "7,77"):
        assert expected in texts, expected
    assert "29,20" not in texts and "106,64" not in texts  # lokalny+fx i total-fx nie wystepuja
    assert assert_nums_have_conv(p, "sec-s9") >= 12


# ---------------------------------------------------------------------------
# Ocena planisty D2-r1 (2026-10-19): zakres liczenia naruszen (S5a/S5b),
# agregaty S3 ze stanem oceny i licznikiem zamknietych, etykieta TWR od S.
# ---------------------------------------------------------------------------


def test_s5a_scope_label_full_weeks_to_max_window_end():
    cur = registry_cur(
        3, 1,
        viol=[("abs", 2, date(2025, 1, 2), date(2026, 9, 30), "niezweryfikowane", datetime(2026, 10, 1, 7, 0)),
              ("rel_spyi", 1, date(2025, 3, 3), date(2026, 10, 9), "niezweryfikowane", datetime(2026, 10, 1, 7, 0))],
    )
    s5a = generate._fetch_registry1(cur, D)
    h = model.render_s5a_html(sat_of(s5a=s5a))
    p = Parsed(h)
    scope = Parsed.txt(p.role("sec-s5a", "violations-scope")[0])
    assert "pelne tygodnie do 2026-10-09" in scope  # max window_end ze wszystkich komponentow
    footer = Parsed.txt(p.role("sec-s5a", "conv-footer")[0])
    assert "zakres liczenia naruszen: pelne tygodnie do 2026-10-09" in footer
    assert "z biezacym tygodniem" not in h


def test_s5a_scope_label_without_windows_is_not_a_date():
    s5a = generate._fetch_registry1(registry_cur(5, 5), D)
    p = Parsed(model.render_s5a_html(sat_of(s5a=s5a)))
    scope = Parsed.txt(p.role("sec-s5a", "violations-scope")[0])
    assert "brak okien z naruszeniem w rejestrze 1" in scope and "pelne tygodnie do" not in scope


def test_s5b_scope_and_twr_from_s_label(monkeypatch):
    patch_stage(monkeypatch, report=make_report())
    s5b, _ = generate._build_s5b_s9(FakeDbConn(), D, None)
    h = model.render_s5b_html(sat_of(s5b=s5b))
    p = Parsed(h)
    assert Parsed.txt(p.role("sec-s5b", "twr-label")[0]) == "TWR od S = 2026-03-02"
    scope = Parsed.txt(p.role("sec-s5b", "violations-scope")[0])
    assert "z biezacym tygodniem do D = 2026-10-19" in scope
    footer = Parsed.txt(p.role("sec-s5b", "conv-footer")[0])
    assert "TWR od S = 2026-03-02" in footer and "z biezacym tygodniem do D = 2026-10-19" in footer
    sec = p.sec_text("sec-s5b").lower()
    assert "indeks" not in sec and "i_sat" not in sec and "pelne tygodnie" not in sec


def test_s3_aggregates_state_and_closed_since_risk_count(monkeypatch):
    # ZZZ zamknieta po ocenie (wiersz oceny, brak na D); CCC rdzen - nie budzetowa, nie liczona
    rr = [make_rr(1, "AAA"), make_rr(9, "ZZZ"), make_rr(7, "CCC", instrument_type="etf", is_core=True)]
    s3, _, _, _ = s3s4(monkeypatch, risk_rows=rr, open_positions=[make_pos(1, "AAA")])
    assert s3["closed_since_risk"] == 1
    p = Parsed(model.render_s3_html(sat_of(s3=s3)))
    scope = p.role("sec-s3", "agg-scope")[0]
    t = Parsed.txt(scope)
    assert "stan oceny ryzyka 2026-10-16" in t and "zamkniete od tej daty" in t
    nums = [r for r in p.nums("sec-s3") if Parsed.txt(r) == "1" and "risk_daily vs positions_as_of(D)" in r["attrs"]["data-conv"]]
    assert len(nums) == 1
    footer = Parsed.txt(p.role("sec-s3", "conv-footer")[0])
    assert "agregaty = stan oceny ryzyka 2026-10-16, pozycje zamkniete od tej daty: 1" in footer
    assert_nums_have_conv(p, "sec-s3")


def test_s3_closed_since_risk_zero_when_all_open(monkeypatch):
    s3, _, _, _ = s3s4(monkeypatch, risk_rows=[make_rr(1, "AAA")], open_positions=[make_pos(1, "AAA")])
    assert s3["closed_since_risk"] == 0
    p = Parsed(model.render_s3_html(sat_of(s3=s3)))
    assert "pozycje zamkniete od tej daty: 0" in Parsed.txt(p.role("sec-s3", "conv-footer")[0])

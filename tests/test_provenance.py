"""B-23, B-54: `mannaz.provenance` — SHA kodu i wyzwalacz przebiegu (bez bazy)."""

from __future__ import annotations

import re
import subprocess
from types import SimpleNamespace

import pytest

from mannaz import provenance
from mannaz.provenance import current_trigger, triggered_by


@pytest.fixture(autouse=True)
def _clear_sha_cache():
    provenance.code_sha.cache_clear()
    yield
    provenance.code_sha.cache_clear()


def test_code_sha_is_commit_sha_with_optional_dirty_suffix():
    sha = provenance.code_sha()
    assert sha is not None
    assert re.fullmatch(r"[0-9a-f]{40}(-dirty)?", sha)


def test_code_sha_dirty_suffix_from_tracked_changes(monkeypatch):
    out = {("rev-parse", "HEAD"): "a" * 40, ("status", "--porcelain", "--untracked-files=no"): " M src/x.py"}
    monkeypatch.setattr(provenance, "_git", lambda *a: out[a])
    assert provenance.code_sha() == "a" * 40 + "-dirty"


def test_code_sha_none_when_git_unavailable(monkeypatch):
    def boom(*a):
        raise subprocess.CalledProcessError(128, ["git"])

    monkeypatch.setattr(provenance, "_git", boom)
    assert provenance.code_sha() is None


def test_triggered_by_sets_and_resets_and_inner_wins():
    seen = []

    @triggered_by("cycle")
    def inner():
        seen.append(current_trigger())

    @triggered_by("cli")
    def outer():
        seen.append(current_trigger())
        inner()
        seen.append(current_trigger())

    assert current_trigger() is None
    outer()
    assert seen == ["cli", "cycle", "cli"]
    assert current_trigger() is None


def test_triggered_by_resets_after_exception():
    @triggered_by("cli")
    def fails():
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        fails()
    assert current_trigger() is None


def test_triggered_by_rejects_unknown_value():
    with pytest.raises(ValueError):
        triggered_by("cron")


def test_entrypoints_set_trigger(monkeypatch):
    from mannaz import cycle, run_p3

    seen = []
    parser = SimpleNamespace(parse_args=lambda argv: SimpleNamespace(func=lambda a: seen.append(current_trigger())))
    monkeypatch.setattr(run_p3, "build_parser", lambda: parser)
    run_p3.main([])
    assert seen == ["cli"]
    # run_cycle opakowany dekoratorem 'cycle' (pełny przebieg wymaga bazy)
    assert cycle.run_cycle.__wrapped__ is not None
    assert current_trigger() is None

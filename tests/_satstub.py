"""Pomocnik testow: minimalny wynik etapu satelity dla testow `run_cycle`, ktore
nie sprawdzaja satelity (prawdziwy etap trwa ok. 200 s i robi INSERT do tabel M89)."""

from __future__ import annotations

from datetime import date

from mannaz.satellite import SatelliteStageResult


def minimal_stage_result(d: date) -> SatelliteStageResult:
    report = {
        "d_from": "2023-10-12",
        "d_to": d.isoformat(),
        "nav_d_pln": None,
        "data_verified": False,
        "data_unverified_reasons": ["stub testowy"],
        "S": None,
        "windows": {},
        "alerts": {},
        "registry2": {"T42": "2026-09-30", "status": "przed T42", "verdict_label": "bez werdyktu do 2028-09-30"},
    }
    return SatelliteStageResult(
        d=d, report=report, persistence={1: {"registry": 1, "absent": True}, 2: {"registry": 2, "absent": True}},
        out_dir=None, md_path=None, written=False,
    )


def satellite_stub(conn, d, **kwargs) -> SatelliteStageResult:
    """Wstrzykiwany jako `satellite_fn` w `run_cycle`."""
    return minimal_stage_result(d)

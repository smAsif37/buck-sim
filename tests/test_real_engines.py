"""Integration tests that need a real SPICE engine. Skipped automatically when absent.

Run with:  pytest -m ngspice   (needs `ngspice` on PATH and `pip install spicelib`)
           LTSPICE_CMD=... pytest -m ltspice
"""

from __future__ import annotations

import copy
import os
import shutil
from dataclasses import replace

import pytest

from buck_sim.engines.ltspice import LTspiceEngine
from buck_sim.engines.ngspice import NgspiceEngine
from buck_sim.runner import RunContext, run_job
from buck_sim.spec import JobSpec


def run_demo(tmp_path, engine, settings, example_files, spec_dict, tests):
    d = copy.deepcopy(spec_dict)
    d["tests"] = tests
    d["limits"] = {}
    spec = JobSpec.model_validate(d)
    return run_job(tmp_path / "job", spec, example_files, "buck_demo.cir", RunContext(engine, settings))


def check_demo_results(res):
    by = {t["test"]: t for t in res["tests"]}
    for t in res["tests"]:
        assert t["status"] == "ok", f"{t['test']}: {t['error']}\n{t['log_tail']}"
    ss = by["steady_state"]["metrics"]
    # closed loop 12 V -> 5 V at 2 A: loose, physics-based bounds
    assert ss["vout_mean_v"] == pytest.approx(5.0, abs=0.15)
    assert 70 < ss["efficiency_pct"] < 99
    assert 0 < ss["ripple_pp_v"] < 0.3
    assert ss["iout_avg_a"] == pytest.approx(2.0, abs=0.1)
    lt = by["load_transient"]["metrics"]
    assert lt["up_final_v"] == pytest.approx(5.0, abs=0.15)
    assert lt["up_peak_dev_v"] < 1.0


@pytest.mark.ngspice
@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice is not installed")
def test_ngspice_end_to_end(tmp_path, settings, example_files, spec_dict):
    res = run_demo(tmp_path, NgspiceEngine(), replace(settings, engine="ngspice"), example_files, spec_dict,
                   ["steady_state", "load_transient"])
    check_demo_results(res)


@pytest.mark.ngspice
@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice is not installed")
def test_ngspice_regulation_sweep(tmp_path, settings, example_files, spec_dict):
    res = run_demo(tmp_path, NgspiceEngine(), replace(settings, engine="ngspice"), example_files, spec_dict,
                   ["line_regulation"])
    t = res["tests"][0]
    assert t["status"] == "ok", t["error"]
    assert t["metrics"]["line_reg_pct"] < 3.0


@pytest.mark.ltspice
@pytest.mark.skipif(not os.environ.get("LTSPICE_CMD"), reason="LTSPICE_CMD is not set")
def test_ltspice_end_to_end(tmp_path, settings, example_files, spec_dict):
    engine = LTspiceEngine(os.environ["LTSPICE_CMD"], os.environ.get("LTSPICE_ARGS") or None)
    res = run_demo(tmp_path, engine, replace(settings, engine="ltspice"), example_files, spec_dict,
                   ["steady_state", "load_transient"])
    check_demo_results(res)


@pytest.mark.ngspice
@pytest.mark.skipif(shutil.which("ngspice") is None, reason="ngspice is not installed")
@pytest.mark.parametrize("filetype", ["ascii", "binary"])
@pytest.mark.parametrize("old_header", [False, True], ids=["new-header", "no-command-line"])
def test_ngspice_raw_formats(tmp_path, filetype, old_header):
    """Both raw formats must be readable (Linux ngspice defaults to binary, Windows to ascii),
    also without the "Command:" header line that ngspice only writes from v44 on."""
    import subprocess

    from buck_sim.engines.rawio import read_raw_traces

    (tmp_path / ".spiceinit").write_text(f"set filetype={filetype}\n")
    (tmp_path / "t.cir").write_text("t\nV1 a 0 SIN(0 1 1k)\nR1 a 0 1k\n.tran 1u 1m\n.end\n")
    eng = NgspiceEngine()
    subprocess.run([*eng.cmd, "-b", "-r", "t.raw", "t.cir"], cwd=tmp_path, capture_output=True, timeout=60)
    raw = tmp_path / "t.raw"
    data = raw.read_bytes()
    assert (b"Binary:" if filetype == "binary" else b"Values:") in data
    if old_header:
        # emulate ngspice < 44 (e.g. Debian's ngspice 39): drop the "Command:" line
        head, sep, body = data.partition(b"Binary:\n" if filetype == "binary" else b"Values:\n")
        head = b"".join(ln for ln in head.splitlines(keepends=True) if not ln.startswith(b"Command:"))
        raw.write_bytes(head + sep + body)
        assert b"Command:" not in raw.read_bytes()[:2000]
    time, traces = read_raw_traces(raw, dialect="ngspice")
    assert len(time) > 100 and time[-1] == pytest.approx(1e-3)
    assert {k.lower() for k in traces} >= {"v(a)", "i(v1)"}

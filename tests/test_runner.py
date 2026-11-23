import copy
import json

import numpy as np
import pytest

from buck_sim.analysis import (
    check_complete,
    evaluate_limits,
    export_waveforms,
    regulation_summary,
    steady_metrics,
    transient_metrics,
)
from buck_sim.engines.base import Engine, EngineError, Waveforms
from buck_sim.netlist import NetlistLoader
from buck_sim.runner import JobCancelled, JobInputError, RunContext, run_job
from buck_sim.spec import JobSpec, Limit
from buck_sim.testbench import TestbenchBuilder


def run(tmp_path, files, spec, engine, settings, main="buck_demo.cir", **ctx_kw):
    return run_job(tmp_path / "job", spec, files, main, RunContext(engine, settings, **ctx_kw))


def test_full_pipeline_on_mock(tmp_path, example_files, spec, mock_engine, settings):
    res = run(tmp_path, example_files, spec, mock_engine, settings)
    assert res["engine"]["synthetic"] is True
    assert any("SYNTHETIC" in w for w in res["warnings"])
    assert [t["test"] for t in res["tests"]] == list(spec.tests)
    assert all(t["status"] == "ok" for t in res["tests"]), [t["error"] for t in res["tests"]]
    ss = res["tests"][0]["metrics"]
    assert ss["vout_mean_v"] == pytest.approx(5.0, abs=0.05)
    assert 80 < ss["efficiency_pct"] < 100
    assert 0 < ss["ripple_pp_v"] < 0.2
    lt = res["tests"][1]["metrics"]
    assert lt["up_peak_dev_v"] > 0 and lt["up_settle_us"] is not None
    assert res["tests"][3]["points"] and len(res["tests"][3]["points"]) == 5
    assert len(res["tests"][4]["points"]) == 6
    # artefacts
    job = tmp_path / "job"
    on_disk = json.loads((job / "results.json").read_text())
    assert on_disk["summary"] == res["summary"]
    wave = json.loads((job / "waveforms" / "steady_state.json").read_text())
    assert wave["synthetic"] is True and len(wave["time_us"]) <= 2000
    assert set(wave["series"]) == {"vout_v", "vin_v", "iload_a", "iin_a"}
    assert "detail" in wave and len(wave["detail"]["time_us"]) > 100
    assert (job / "work" / "steady_state" / "steady_state" / "tb.cir").exists()
    # results are strict JSON (no NaN/Infinity)
    json.loads((job / "results.json").read_text(), parse_constant=lambda c: pytest.fail(c))


def test_limits_and_verdict(tmp_path, example_files, spec_dict, mock_engine, settings):
    d = copy.deepcopy(spec_dict)
    d["limits"] = {"steady_state.ripple_pp_v": {"max": 1e-9}, "steady_state.vout_error_pct": {"min": -5, "max": 5}}
    d["tests"] = ["steady_state"]
    res = run(tmp_path, example_files, JobSpec.model_validate(d), mock_engine, settings)
    assert res["summary"]["verdict"] == "fail"
    assert res["summary"]["checks_failed"] == 1 and res["summary"]["checks_total"] == 2
    checks = {c["key"]: c["passed"] for c in res["tests"][0]["checks"]}
    assert checks == {"steady_state.ripple_pp_v": False, "steady_state.vout_error_pct": True}


def test_limit_for_unselected_test_warns(tmp_path, example_files, spec_dict, mock_engine, settings):
    d = copy.deepcopy(spec_dict)
    d["tests"] = ["steady_state"]
    d["limits"] = {"load_transient.up_peak_dev_v": {"max": 1}}
    res = run(tmp_path, example_files, JobSpec.model_validate(d), mock_engine, settings)
    assert any("not selected" in w for w in res["warnings"])
    assert res["summary"]["verdict"] == "n/a"


def test_one_failing_test_does_not_stop_the_others(tmp_path, example_files, spec_dict, mock_engine, settings):
    d = copy.deepcopy(spec_dict)
    d["design"]["load_slew_a_per_us"] = 0.001  # step would take 1.5 ms -> load_transient cannot be built
    res = run(tmp_path, example_files, JobSpec.model_validate(d), mock_engine, settings)
    by = {t["test"]: t for t in res["tests"]}
    assert by["load_transient"]["status"] == "error" and "too slow" in by["load_transient"]["error"]
    assert by["steady_state"]["status"] == "ok" and by["line_regulation"]["status"] == "ok"
    assert res["summary"]["verdict"] == "error" and res["summary"]["tests_error"] == 1


def test_input_errors_are_reported(tmp_path, example_files, spec_dict, mock_engine, settings):
    d = copy.deepcopy(spec_dict)
    d["circuit"]["vin_source"] = "V99"
    with pytest.raises(JobInputError, match="V99"):
        run(tmp_path, example_files, JobSpec.model_validate(d), mock_engine, settings)
    files = dict(example_files)
    del files["toyctrl.lib"]
    with pytest.raises(JobInputError, match="toyctrl.lib"):
        run(tmp_path, files, JobSpec.model_validate(spec_dict), mock_engine, settings)


def test_cancellation(tmp_path, example_files, spec, mock_engine, settings):
    with pytest.raises(JobCancelled):
        run(tmp_path, example_files, spec, mock_engine, settings, cancelled=lambda: True)


def test_time_step_budget(tmp_path, example_files, spec_dict, mock_engine, settings):
    from dataclasses import replace

    tight = replace(settings, max_time_points=1000)
    d = copy.deepcopy(spec_dict)
    d["tests"] = ["steady_state"]
    res = run(tmp_path, example_files, JobSpec.model_validate(d), mock_engine, tight)
    assert res["tests"][0]["status"] == "error" and "time steps" in res["tests"][0]["error"]


class TruncatingEngine(Engine):
    name = "trunc"

    def simulate(self, bundle, workdir, timeout_s):
        from buck_sim.engines.mock import MockEngine

        wf = MockEngine().simulate(bundle, workdir, timeout_s)
        n = len(wf.time) // 3
        return Waveforms(wf.time[:n], {k: v[:n] for k, v in wf.signals.items()}, "trunc", log_tail="Time step too small")


class FailingEngine(Engine):
    name = "fail"

    def simulate(self, bundle, workdir, timeout_s):
        raise EngineError("simulator exploded", "line1\nline2")


def test_incomplete_simulation_is_an_error_with_log(tmp_path, example_files, spec_dict, settings):
    d = copy.deepcopy(spec_dict)
    d["tests"] = ["steady_state"]
    res = run(tmp_path, example_files, JobSpec.model_validate(d), TruncatingEngine(), settings)
    t = res["tests"][0]
    assert t["status"] == "error" and "stopped early" in t["error"] and "Time step too small" in t["log_tail"]


def test_engine_failure_is_isolated_per_test(tmp_path, example_files, spec_dict, settings):
    d = copy.deepcopy(spec_dict)
    d["tests"] = ["steady_state", "line_regulation"]
    res = run(tmp_path, example_files, JobSpec.model_validate(d), FailingEngine(), settings)
    assert [t["status"] for t in res["tests"]] == ["error", "error"]
    assert res["tests"][0]["log_tail"] == "line1\nline2"


def test_parallel_sweeps_give_same_result(tmp_path, example_files, spec_dict, mock_engine, settings):
    from dataclasses import replace

    d = copy.deepcopy(spec_dict)
    d["tests"] = ["line_regulation"]
    a = run(tmp_path / "a", example_files, JobSpec.model_validate(d), mock_engine, settings)
    b = run(tmp_path / "b", example_files, JobSpec.model_validate(d), mock_engine, replace(settings, sim_parallelism=4))
    assert a["tests"][0]["points"] == b["tests"][0]["points"]


# ---------------------------------------------------------------- analysis unit
def _bundle_and_wave(example_files, spec, mock_engine, tmp_path, which="steady"):
    tb = TestbenchBuilder(NetlistLoader(example_files).load_main("buck_demo.cir"), spec)
    b = {"steady": lambda: tb.steady_state(12, 2), "load": tb.load_transient, "line": tb.line_transient}[which]()
    return b, mock_engine.simulate(b, tmp_path, 10)


def test_steady_metrics_exact_on_synthetic_waveform(example_files, spec, tmp_path, mock_engine):
    b, _ = _bundle_and_wave(example_files, spec, mock_engine, tmp_path)
    t = np.linspace(0, b.tstop, 200001)
    # 5 V +- 10 mV triangle-free sine at fsw, 12 V in, 2 A load, 10.5 W in
    vout = 5.0 + 0.01 * np.sin(2 * np.pi * spec.design.fsw_hz * t)
    ctl = np.full_like(t, 2.0)
    vin = np.full_like(t, 12.0)
    iin = np.full_like(t, -10.5 / 12.0)
    wf = Waveforms(t, {"vin": vin, "vout": vout, "ctl": ctl, "iin": iin}, "test")
    m, warns = steady_metrics(wf, b)
    assert m["vout_mean_v"] == pytest.approx(5.0, abs=1e-4)
    assert m["ripple_pp_v"] == pytest.approx(0.02, rel=0.01)
    assert m["iout_avg_a"] == pytest.approx(2.0, rel=1e-3)
    assert m["pin_w"] == pytest.approx(10.5, rel=1e-6)
    assert m["pout_w"] == pytest.approx(10.0, rel=1e-3)
    assert m["efficiency_pct"] == pytest.approx(10.0 / 10.5 * 100, rel=2e-3)
    assert m["iin_avg_a"] == pytest.approx(10.5 / 12, rel=1e-6)
    assert warns == []


def test_efficiency_over_100_is_flagged(example_files, spec, tmp_path, mock_engine):
    b, _ = _bundle_and_wave(example_files, spec, mock_engine, tmp_path)
    t = np.linspace(0, b.tstop, 1000)
    wf = Waveforms(t, {"vin": np.full_like(t, 12.0), "vout": np.full_like(t, 5.0), "ctl": np.full_like(t, 2.0),
                       "iin": np.full_like(t, -0.5)}, "test")
    m, warns = steady_metrics(wf, b)
    assert m["efficiency_pct"] > 100 and any("above 100" in w for w in warns)
    wf.signals["iin"] = np.full_like(t, +0.5)  # wrong sign
    m, warns = steady_metrics(wf, b)
    assert m["efficiency_pct"] is None and any("not positive" in w for w in warns)


def test_transient_metrics_on_known_response(example_files, spec, tmp_path, mock_engine):
    b, _ = _bundle_and_wave(example_files, spec, mock_engine, tmp_path, "load")
    d = spec.design
    dt = d.timing().period / 100
    t = np.arange(0, b.tstop + dt / 2, dt)
    tu, td = b.events["t_up"], b.events["t_down"]
    tau = 15e-6
    v = np.full_like(t, 5.0)
    up = (t >= tu) & (t < td)
    v[up] = 5.0 - 0.15 * np.exp(-(t[up] - tu) / tau)
    dn = t >= td
    v[dn] = 5.0 + 0.10 * np.exp(-(t[dn] - td) / tau)
    wf = Waveforms(t, {"vin": np.full_like(t, 12.0), "vout": v, "ctl": np.ones_like(t), "iin": -np.ones_like(t)}, "test")
    m = transient_metrics(wf, b)
    assert m["up_peak_dev_v"] == pytest.approx(0.15, abs=0.02)
    assert m["down_peak_dev_v"] == pytest.approx(0.10, abs=0.02)
    assert m["vout_pre_v"] == pytest.approx(5.0, abs=1e-3)
    # band = 2 % of 5 V = 0.1 V: |0.15 e^(-x/tau)| < 0.1 -> x = tau*ln(1.5) = 6.1 us ; down never exceeds 0.1
    assert m["up_settle_us"] == pytest.approx(15 * np.log(1.5), abs=2.0)
    assert m["down_settle_us"] == pytest.approx(0.0, abs=1.0)
    assert m["up_vout_min_v"] < 4.86 and m["down_vout_max_v"] > 5.09


def test_regulation_summary_math(spec):
    pts = [{"x": 9, "vout_mean_v": 4.99, "efficiency_pct": 90, "ripple_pp_v": 0.01},
           {"x": 15, "vout_mean_v": 5.01, "efficiency_pct": 88, "ripple_pp_v": 0.02}]
    m = regulation_summary("line_regulation", pts, spec.design)
    assert m["line_reg_pct"] == pytest.approx(0.4)
    assert m["line_reg_mv_per_v"] == pytest.approx(20 / 6)
    assert m["efficiency_min_pct"] == 88 and m["ripple_max_pp_v"] == 0.02
    m = regulation_summary("load_regulation", pts, spec.design)
    assert m["load_reg_mv_per_a"] == pytest.approx(20 / 1.5)
    assert m["efficiency_peak_pct"] == 90


def test_evaluate_limits_edge_cases():
    limits = {"steady_state.ripple_pp_v": Limit(max=0.05), "steady_state.efficiency_pct": Limit(min=90),
              "load_transient.up_settle_us": Limit(max=100)}
    checks = evaluate_limits("steady_state", {"ripple_pp_v": 0.05, "efficiency_pct": None}, limits)
    assert {c["key"]: c["passed"] for c in checks} == {"steady_state.ripple_pp_v": True, "steady_state.efficiency_pct": False}


def test_check_complete_threshold(example_files, spec, tmp_path, mock_engine):
    b, wf = _bundle_and_wave(example_files, spec, mock_engine, tmp_path)
    check_complete(wf, b)
    short = Waveforms(wf.time[: len(wf.time) // 2], {k: v[: len(wf.time) // 2] for k, v in wf.signals.items()}, "x")
    with pytest.raises(EngineError):
        check_complete(short, b)


def test_export_waveforms_is_bounded_and_finite(example_files, spec, tmp_path, mock_engine):
    b, wf = _bundle_and_wave(example_files, spec, mock_engine, tmp_path, "line")
    w = export_waveforms(wf, b, "line_transient", max_points=500)
    assert len(w["time_us"]) <= 500 and "detail" not in w
    assert all(np.isfinite(v).all() for v in w["series"].values())
    assert w["events_us"]["t_up"] > 0

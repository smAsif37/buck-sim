"""Run a whole job: parse inputs, build testbenches, simulate, measure, report."""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .analysis import (
    check_complete,
    evaluate_limits,
    export_waveforms,
    regulation_summary,
    steady_metrics,
    transient_metrics,
)
from .config import Settings
from .engines.base import Engine, EngineError, Waveforms
from .metrics import MeasurementError
from .netlist import NetlistError, NetlistLoader
from .registry import METRICS, TEST_LABELS
from .spec import JobSpec
from .testbench import Bundle, TestbenchBuilder, TestbenchError

log = logging.getLogger(__name__)

RESULT_SCHEMA = 1


class JobCancelled(Exception):
    """Raised when the job was cancelled or the worker is shutting down."""


class JobInputError(ValueError):
    """The uploaded inputs / spec cannot be turned into simulations (user-fixable)."""


@dataclass
class RunContext:
    engine: Engine
    settings: Settings
    progress: Callable[[str], None] = lambda _msg: None
    cancelled: Callable[[], bool] = lambda: False


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)


def prepare(
    files: dict[str, bytes], netlist_name: str, spec: JobSpec, engine: Engine, settings: Settings
) -> tuple[TestbenchBuilder, list[str]]:
    """Parse + validate everything that can be validated without simulating."""
    libs = settings.builtin_libs if engine.supports_builtin_libs else frozenset()
    try:
        parsed = NetlistLoader(files, builtin_libs=libs).load_main(netlist_name)
        builder = TestbenchBuilder(parsed, spec, engine.extra_lines)
    except (NetlistError, TestbenchError) as e:
        raise JobInputError(str(e)) from e
    return builder, parsed.warnings


def _plan(builder: TestbenchBuilder, test: str) -> list[Bundle]:
    if test == "steady_state":
        d = builder.design
        return [builder.steady_state(d.vin_nom, d.iout_max)]
    if test == "load_transient":
        return [builder.load_transient()]
    if test == "line_transient":
        return [builder.line_transient()]
    if test == "line_regulation":
        return builder.line_sweep()
    if test == "load_regulation":
        return builder.load_sweep()
    raise JobInputError(f"unknown test {test!r}")


def _check_size(bundle: Bundle, settings: Settings) -> None:
    n = bundle.tstop / bundle.tmax
    if n > settings.max_time_points:
        raise TestbenchError(
            f"{bundle.name}: {n:.3g} time steps requested (limit {settings.max_time_points}). "
            "Reduce hold_time_s / startup_time_s / steps_per_cycle."
        )


def _simulate(bundle: Bundle, workroot: Path, ctx: RunContext) -> Waveforms:
    if ctx.cancelled():
        raise JobCancelled()
    _check_size(bundle, ctx.settings)
    ctx.progress(f"simulating {bundle.name}")
    wf = ctx.engine.simulate(bundle, workroot / bundle.name, ctx.settings.sim_timeout_s)
    check_complete(wf, bundle)
    return wf


def _run_test(
    test: str,
    builder: TestbenchBuilder,
    spec: JobSpec,
    job_dir: Path,
    ctx: RunContext,
    deadline: float,
) -> dict:
    t0 = time.monotonic()
    result: dict = {
        "test": test,
        "label": TEST_LABELS[test],
        "status": "ok",
        "error": None,
        "log_tail": "",
        "metrics": {},
        "checks": [],
        "points": [],
        "warnings": [],
        "waveform": None,
        "duration_s": 0.0,
    }
    workroot = job_dir / "work" / test
    try:
        bundles = _plan(builder, test)
        if test in ("steady_state", "load_transient", "line_transient"):
            wf = _simulate(bundles[0], workroot, ctx)
            if test == "steady_state":
                metrics, warns = steady_metrics(wf, bundles[0])
                result["warnings"].extend(warns)
            else:
                metrics = transient_metrics(wf, bundles[0])
            result["metrics"] = metrics
            wave = export_waveforms(wf, bundles[0], test)
            write_json_atomic(job_dir / "waveforms" / f"{test}.json", wave)
            result["waveform"] = f"{test}.json"
        else:
            points: list[dict] = []

            def one(b: Bundle) -> dict:
                if time.monotonic() > deadline:
                    raise EngineError("job time limit reached")
                wf = _simulate(b, workroot, ctx)
                m, warns = steady_metrics(wf, b)
                key = "vin_v" if test == "line_regulation" else "iload_a"
                return {"x": b.params[key], "metrics": m, "warnings": warns}

            workers = max(1, min(ctx.settings.sim_parallelism, len(bundles)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                outs = list(pool.map(one, bundles))
            for o in outs:
                result["warnings"].extend(o["warnings"])
                points.append({"x": o["x"], **o["metrics"]})
            result["points"] = points
            result["metrics"] = regulation_summary(test, points, spec.design)
            result["x_label"] = "Vin (V)" if test == "line_regulation" else "Load current (A)"
        result["checks"] = evaluate_limits(test, result["metrics"], spec.limits)
    except JobCancelled:
        raise
    except EngineError as e:
        result.update(status="error", error=str(e), log_tail=e.log_tail)
    except (TestbenchError, MeasurementError) as e:
        result.update(status="error", error=str(e))
    result["warnings"] = list(dict.fromkeys(result["warnings"]))
    result["duration_s"] = round(time.monotonic() - t0, 3)
    return result


def summarise(tests: list[dict]) -> dict:
    n_checks = sum(len(t["checks"]) for t in tests)
    n_failed = sum(1 for t in tests for c in t["checks"] if not c["passed"])
    n_err = sum(1 for t in tests if t["status"] == "error")
    if n_err:
        verdict = "error"
    elif n_checks == 0:
        verdict = "n/a"
    else:
        verdict = "fail" if n_failed else "pass"
    return {
        "verdict": verdict,
        "tests_total": len(tests),
        "tests_ok": len(tests) - n_err,
        "tests_error": n_err,
        "checks_total": n_checks,
        "checks_failed": n_failed,
    }


def run_job(
    job_dir: Path,
    spec: JobSpec,
    files: dict[str, bytes],
    netlist_name: str,
    ctx: RunContext,
) -> dict:
    """Execute a job and write ``results.json`` into ``job_dir``. Returns the results dict."""
    started = time.monotonic()
    deadline = started + ctx.settings.job_timeout_s
    ctx.progress("preparing")
    builder, warnings = prepare(files, netlist_name, spec, ctx.engine, ctx.settings)
    for key in spec.limits:
        if key.split(".", 1)[0] not in spec.tests:
            warnings.append(f"limit {key!r} ignored because that test is not selected")
    if ctx.engine.synthetic:
        warnings.append("SYNTHETIC DATA: produced by the mock engine, not by a SPICE simulator")

    tests: list[dict] = []
    for test in spec.tests:
        if ctx.cancelled():
            raise JobCancelled()
        if time.monotonic() > deadline:
            tests.append(
                {
                    "test": test, "label": TEST_LABELS[test], "status": "error",
                    "error": "job time limit reached before this test started", "log_tail": "",
                    "metrics": {}, "checks": [], "points": [], "warnings": [], "waveform": None,
                    "duration_s": 0.0,
                }
            )  # fmt: skip
            continue
        ctx.progress(f"running {test}")
        tests.append(_run_test(test, builder, spec, job_dir, ctx, deadline))

    results = {
        "schema": RESULT_SCHEMA,
        "app_version": __version__,
        "engine": {
            "name": ctx.engine.name,
            "version": ctx.engine.version(),
            "synthetic": ctx.engine.synthetic,
        },
        "warnings": list(dict.fromkeys(warnings)),
        "summary": summarise(tests),
        "tests": tests,
        "metric_info": {k: {"unit": u, "label": lbl} for k, (u, lbl) in METRICS.items()},
        "duration_s": round(time.monotonic() - started, 3),
    }
    write_json_atomic(job_dir / "results.json", results)
    return results


def load_spec_file(path: Path) -> JobSpec:
    return JobSpec.model_validate(json.loads(path.read_text(encoding="utf-8")))

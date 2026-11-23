"""Turn raw waveforms into the metrics, checks and plot data for each test."""

from __future__ import annotations

import numpy as np

from .engines.base import EngineError, Waveforms
from .metrics import (
    MeasurementError,
    cycle_average,
    peak_to_peak,
    step_metrics,
    time_mean,
    time_rms_ac,
    uniform_resample,
    window_min_max,
)
from .registry import METRICS
from .spec import Limit
from .testbench import Bundle

Metrics = dict[str, float | None]


def _clean(x: float | None) -> float | None:
    if x is None:
        return None
    x = float(x)
    if not np.isfinite(x):
        return None
    return float(f"{x:.9g}")


def check_complete(wf: Waveforms, bundle: Bundle) -> None:
    """Raise if the simulation ended before the requested stop time."""
    if wf.time[-1] < (1.0 - 1e-5) * bundle.tstop:
        raise EngineError(
            f"simulation stopped early at t = {wf.time[-1] * 1e6:.4g} us of {bundle.tstop * 1e6:.4g} us "
            "(convergence problem, 'time step too small', or the model failed)",
            wf.log_tail,
        )


def load_current(wf: Waveforms, vout_nom: float) -> np.ndarray:
    """Load current actually drawn by the programmable load."""
    return wf.signals["ctl"] * wf.signals["vout"] / vout_nom


# --------------------------------------------------------------------------- #
# Steady state
# --------------------------------------------------------------------------- #
def steady_metrics(wf: Waveforms, b: Bundle) -> tuple[Metrics, list[str]]:
    t = wf.time
    vin, vout, iin = wf.signals["vin"], wf.signals["vout"], wf.signals["iin"]
    w0, w1 = b.events["meas_start"], b.events["meas_end"]
    vnom = b.design.vout_nom
    warnings: list[str] = []

    iload = load_current(wf, vnom)
    vmean = time_mean(t, vout, w0, w1)
    pout = time_mean(t, vout * iload, w0, w1)
    pin = time_mean(t, vin * (-iin), w0, w1)
    eff: float | None = None
    if pin > 1e-9:
        eff = pout / pin * 100.0
        if eff > 100.5:
            warnings.append(
                f"{b.name}: efficiency {eff:.1f}% is above 100% - check that the input source current "
                "sign/orientation is standard and the window is settled"
            )
    else:
        warnings.append(f"{b.name}: input power is not positive; efficiency not computed")

    metrics: Metrics = {
        "vout_mean_v": vmean,
        "vout_error_pct": (vmean - vnom) / vnom * 100.0,
        "ripple_pp_v": peak_to_peak(t, vout, w0, w1),
        "ripple_rms_v": time_rms_ac(t, vout, w0, w1),
        "iout_avg_a": time_mean(t, iload, w0, w1),
        "iin_avg_a": -time_mean(t, iin, w0, w1),
        "pout_w": pout,
        "pin_w": pin,
        "efficiency_pct": eff,
    }
    return {k: _clean(v) for k, v in metrics.items()}, warnings


# --------------------------------------------------------------------------- #
# Transients
# --------------------------------------------------------------------------- #
def transient_metrics(wf: Waveforms, b: Bundle) -> Metrics:
    d = b.design
    tm = d.timing()
    t, vout = wf.time, wf.signals["vout"]
    ev = b.events
    t_up, t_dn, t_end = ev["t_up"], ev["t_down"], ev["tstop"]
    meas = tm.meas_window
    band = d.settle_band_pct / 100.0 * d.vout_nom

    up = step_metrics(
        t, vout, period=tm.period, t_event=t_up,
        pre_window=(t_up - meas, t_up), final_window=(t_dn - meas, t_dn),
        t_end=t_dn, band_abs=band,
    )  # fmt: skip
    down = step_metrics(
        t, vout, period=tm.period, t_event=t_dn,
        pre_window=(t_dn - meas, t_dn), final_window=(t_end - meas, t_end),
        t_end=t_end, band_abs=band,
    )  # fmt: skip

    def us(x: float | None) -> float | None:
        return None if x is None else x * 1e6

    metrics: Metrics = {
        "vout_pre_v": up["pre"],
        "up_peak_dev_v": up["peak_dev"],
        "up_dev_pct": up["peak_dev"] / d.vout_nom * 100.0,
        "up_vout_min_v": up["raw_min"],
        "up_vout_max_v": up["raw_max"],
        "up_settle_us": us(up["settle_s"]),
        "up_final_v": up["final"],
        "down_peak_dev_v": down["peak_dev"],
        "down_dev_pct": down["peak_dev"] / d.vout_nom * 100.0,
        "down_vout_min_v": down["raw_min"],
        "down_vout_max_v": down["raw_max"],
        "down_settle_us": us(down["settle_s"]),
        "down_final_v": down["final"],
    }
    return {k: _clean(v) for k, v in metrics.items()}


# --------------------------------------------------------------------------- #
# Regulation (summaries of steady-state sweeps)
# --------------------------------------------------------------------------- #
def regulation_summary(kind: str, points: list[dict], b_design) -> Metrics:
    """``points``: list of dicts with 'vout_mean_v', 'efficiency_pct', 'ripple_pp_v', 'x'."""
    vouts = [p["vout_mean_v"] for p in points if p.get("vout_mean_v") is not None]
    if len(vouts) < 2:
        raise MeasurementError("not enough valid sweep points")
    vmin, vmax = min(vouts), max(vouts)
    span = vmax - vmin
    effs = [p["efficiency_pct"] for p in points if p.get("efficiency_pct") is not None]
    ripples = [p["ripple_pp_v"] for p in points if p.get("ripple_pp_v") is not None]
    if kind == "line_regulation":
        dvin = b_design.vin_max - b_design.vin_min
        m: Metrics = {
            "vout_min_v": vmin,
            "vout_max_v": vmax,
            "line_reg_pct": span / b_design.vout_nom * 100.0,
            "line_reg_mv_per_v": span / dvin * 1000.0,
            "efficiency_min_pct": min(effs) if effs else None,
            "ripple_max_pp_v": max(ripples) if ripples else None,
        }
    else:
        di = b_design.iout_max - b_design.iout_min
        m = {
            "vout_min_v": vmin,
            "vout_max_v": vmax,
            "load_reg_pct": span / b_design.vout_nom * 100.0,
            "load_reg_mv_per_a": span / di * 1000.0,
            "efficiency_peak_pct": max(effs) if effs else None,
            "efficiency_min_pct": min(effs) if effs else None,
            "ripple_max_pp_v": max(ripples) if ripples else None,
        }
    return {k: _clean(v) for k, v in m.items()}


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #
def evaluate_limits(test: str, metrics: Metrics, limits: dict[str, Limit]) -> list[dict]:
    checks: list[dict] = []
    for key, lim in limits.items():
        tname, mname = key.split(".", 1)
        if tname != test:
            continue
        val = metrics.get(mname)
        ok = (
            val is not None
            and (lim.min is None or val >= lim.min)
            and (lim.max is None or val <= lim.max)
        )
        unit, label = METRICS[key]
        checks.append(
            {"key": key, "label": label, "unit": unit, "value": val, "min": lim.min, "max": lim.max, "passed": bool(ok)}
        )
    return checks


# --------------------------------------------------------------------------- #
# Plot data
# --------------------------------------------------------------------------- #
def _round_list(a: np.ndarray) -> list[float]:
    return [float(f"{x:.6g}") for x in a]


def export_waveforms(wf: Waveforms, b: Bundle, kind: str, max_points: int = 2000) -> dict:
    """Cycle-averaged overview (+ raw ripple detail for steady state), JSON friendly."""
    d = b.design
    tm = d.timing()
    t = wf.time
    iload = load_current(wf, d.vout_nom)
    series_raw = {
        "vout_v": wf.signals["vout"],
        "vin_v": wf.signals["vin"],
        "iload_a": iload,
        "iin_a": -wf.signals["iin"],
    }
    grid = None
    series: dict[str, np.ndarray] = {}
    for k, y in series_raw.items():
        tc, yc = cycle_average(t, y, tm.period)
        grid = tc
        series[k] = yc
    assert grid is not None
    n = len(grid)
    idx = np.unique(np.linspace(0, n - 1, min(n, max_points)).round().astype(int))
    out: dict = {
        "kind": kind,
        "name": b.name,
        "engine": wf.engine,
        "synthetic": wf.synthetic,
        "time_us": _round_list(grid[idx] * 1e6),
        "series": {k: _round_list(v[idx]) for k, v in series.items()},
        "events_us": {k: float(f"{v * 1e6:.6g}") for k, v in b.events.items()},
        "params": {k: float(f"{v:.6g}") for k, v in b.params.items()},
    }
    if kind == "steady_state":
        n_cyc = 5
        t1 = b.events["meas_end"]
        t0 = max(t1 - n_cyc * tm.period, t[0])
        tg, vg = uniform_resample(t, wf.signals["vout"], tm.period / 40.0, t0, t1)
        out["detail"] = {
            "time_us": _round_list((tg - t0) * 1e6),
            "vout_v": _round_list(vg),
        }
        lo, hi = window_min_max(t, wf.signals["vout"], b.events["meas_start"], b.events["meas_end"])
        out["detail"]["vout_min_v"] = float(f"{lo:.6g}")
        out["detail"]["vout_max_v"] = float(f"{hi:.6g}")
    return out

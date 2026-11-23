"""Numerical helpers for waveform measurements.

All functions work on (possibly non-uniform) simulator time grids.
"""

from __future__ import annotations

import numpy as np


class MeasurementError(ValueError):
    """A measurement could not be made (e.g. window outside the simulated time)."""


def trapz(y: np.ndarray, x: np.ndarray) -> float:
    """Trapezoidal integral (independent of the NumPy version)."""
    if len(x) < 2:
        return 0.0
    return float(np.sum((x[1:] - x[:-1]) * (y[1:] + y[:-1]) * 0.5))


def window(t: np.ndarray, y: np.ndarray, t0: float, t1: float) -> tuple[np.ndarray, np.ndarray]:
    """Samples of ``y`` on [t0, t1], with interpolated end points."""
    if not t1 > t0:
        raise MeasurementError("empty measurement window")
    # Simulators may stop a hair before the requested stop time; tolerate 1 ppm.
    tol = 1e-6 * (t[-1] - t[0]) + 1e-15
    t0_req, t1_req = t0, t1
    if t0 < t[0] and t0 >= t[0] - tol:
        t0 = t[0]
    if t1 > t[-1] and t1 <= t[-1] + tol:
        t1 = t[-1]
    if t0 < t[0] - 1e-15 or t1 > t[-1] + 1e-15:
        t0, t1 = t0_req, t1_req
        raise MeasurementError(
            f"measurement window [{t0:.6g}, {t1:.6g}] s is outside the simulated range "
            f"[{t[0]:.6g}, {t[-1]:.6g}] s"
        )
    inside = (t > t0) & (t < t1)
    tw = np.concatenate(([t0], t[inside], [t1]))
    yw = np.concatenate(([np.interp(t0, t, y)], y[inside], [np.interp(t1, t, y)]))
    return tw, yw


def time_mean(t: np.ndarray, y: np.ndarray, t0: float, t1: float) -> float:
    tw, yw = window(t, y, t0, t1)
    return trapz(yw, tw) / (tw[-1] - tw[0])


def time_rms_ac(t: np.ndarray, y: np.ndarray, t0: float, t1: float) -> float:
    """RMS of the AC component (mean removed) over the window."""
    tw, yw = window(t, y, t0, t1)
    span = tw[-1] - tw[0]
    mean = trapz(yw, tw) / span
    return float(np.sqrt(max(trapz((yw - mean) ** 2, tw) / span, 0.0)))


def peak_to_peak(t: np.ndarray, y: np.ndarray, t0: float, t1: float) -> float:
    _, yw = window(t, y, t0, t1)
    return float(np.max(yw) - np.min(yw))


def window_min_max(t: np.ndarray, y: np.ndarray, t0: float, t1: float) -> tuple[float, float]:
    _, yw = window(t, y, t0, t1)
    return float(np.min(yw)), float(np.max(yw))


def uniform_resample(t: np.ndarray, y: np.ndarray, dt: float, t0: float | None = None, t1: float | None = None):
    """Linear resample onto a uniform grid with spacing ``dt``."""
    a = t[0] if t0 is None else t0
    b = t[-1] if t1 is None else t1
    n = int(np.floor((b - a) / dt + 1e-9)) + 1
    grid = a + dt * np.arange(n)
    return grid, np.interp(grid, t, y)


def cycle_average(
    t: np.ndarray, y: np.ndarray, period: float, points_per_period: int = 20
) -> tuple[np.ndarray, np.ndarray]:
    """Moving average over one switching period (centred), on a uniform grid.

    Removes switching ripple so steps/settling can be judged cleanly. The returned
    time axis is the centre of each averaging window.
    """
    n = max(int(points_per_period), 2)
    dt = period / n
    grid, yu = uniform_resample(t, y, dt)
    if len(grid) < n + 1:
        raise MeasurementError("simulation is shorter than one switching period")
    kernel = np.ones(n) / n
    avg = np.convolve(yu, kernel, mode="valid")  # len = len(grid) - n + 1
    centres = grid[0] + dt * (np.arange(len(avg)) + (n - 1) / 2.0)
    return centres, avg


def step_metrics(
    t: np.ndarray,
    y: np.ndarray,
    *,
    period: float,
    t_event: float,
    pre_window: tuple[float, float],
    final_window: tuple[float, float],
    t_end: float,
    band_abs: float,
) -> dict[str, float | None]:
    """Deviation and settling after a disturbance at ``t_event``.

    * ``pre``/``final`` are time-means over the given windows.
    * ``peak_dev`` is the largest |cycle-averaged Vout - pre| after the event.
    * ``settle`` is the time from the event until the cycle-averaged waveform stays
      within +-band_abs of ``final`` (None if it has not settled by ``t_end``).
    """
    pre = time_mean(t, y, *pre_window)
    final = time_mean(t, y, *final_window)
    tc, yc = cycle_average(t, y, period)
    mask = (tc >= t_event) & (tc <= t_end)
    if not np.any(mask):
        raise MeasurementError("no data after the disturbance")
    seg_t, seg_y = tc[mask], yc[mask]
    dev = seg_y - pre
    peak_dev = float(np.max(np.abs(dev)))
    raw_min, raw_max = window_min_max(t, y, t_event, t_end)

    outside = np.abs(seg_y - final) > band_abs
    if not np.any(outside):
        settle: float | None = 0.0
    else:
        last = int(np.max(np.nonzero(outside)))
        settle = None if last >= len(seg_t) - 1 else float(seg_t[last] - t_event)
    return {
        "pre": pre,
        "final": final,
        "peak_dev": peak_dev,
        "raw_min": raw_min,
        "raw_max": raw_max,
        "settle_s": settle,
    }


def decimate(arr: np.ndarray, max_points: int) -> np.ndarray:
    """Index-stride decimation that always keeps the first and last sample."""
    n = len(arr)
    if n <= max_points:
        return arr
    idx = np.unique(np.linspace(0, n - 1, max_points).round().astype(int))
    return arr[idx]

"""Read simulator ``.raw`` files and map traces to our normalised signal names."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np

from ..testbench import CTL_NODE, SignalNames
from .base import EngineError, Waveforms


def read_raw_traces(path: Path) -> tuple[np.ndarray, dict[str, np.ndarray]]:
    """Read every trace of a raw file with spicelib. Returns (time, traces)."""
    try:
        from spicelib import RawRead  # imported lazily: only real engines need it
    except ImportError as e:  # pragma: no cover - depends on the environment
        raise EngineError("the 'spicelib' package is required to read simulator output") from e
    try:
        raw = RawRead(str(path))
        names = list(raw.get_trace_names())
        time = np.abs(np.asarray(raw.get_axis(0), dtype=float))
        traces = {n: np.asarray(raw.get_trace(n).get_wave(0), dtype=float) for n in names}
    except Exception as e:  # spicelib raises several exception types
        raise EngineError(f"could not read simulator output {path.name}: {e}") from e
    return time, traces


def resolve_trace(available: Iterable[str], candidates: list[str]) -> str:
    """Case-insensitive lookup of the first matching candidate trace name."""
    lut = {n.lower(): n for n in available}
    for c in candidates:
        if c.lower() in lut:
            return lut[c.lower()]
    shown = sorted(lut.values(), key=str.lower)[:30]
    raise EngineError(f"trace {candidates[0]!r} not found in simulator output. Available (first 30): {shown}")


def waveforms_from_traces(
    time: np.ndarray, traces: dict[str, np.ndarray], names: SignalNames, engine: str
) -> Waveforms:
    avail = list(traces)
    vin = traces[resolve_trace(avail, [f"V({names.vin_node})", f"v({names.vin_node})"])]
    vout = traces[resolve_trace(avail, [f"V({names.vout_node})"])]
    ctl = traces[resolve_trace(avail, [f"V({CTL_NODE})"])]
    iin = traces[
        resolve_trace(
            avail,
            [f"I({names.vin_source})", f"{names.vin_source}#branch", f"i({names.vin_source})"],
        )
    ]
    n = len(time)
    for label, arr in (("vin", vin), ("vout", vout), ("ctl", ctl), ("iin", iin)):
        if len(arr) != n:
            raise EngineError(f"trace {label} has {len(arr)} points but time has {n}")
    if n < 10:
        raise EngineError(f"simulation produced only {n} time points")
    if not np.all(np.diff(time) >= 0):
        # LTspice can emit repeated/unsorted points at breakpoints; keep the sorted order.
        order = np.argsort(time, kind="stable")
        time, vin, vout, ctl, iin = time[order], vin[order], vout[order], ctl[order], iin[order]
    # np.interp needs strictly increasing x; nudge exact duplicates apart.
    dup = np.diff(time) <= 0
    if np.any(dup):
        keep = np.concatenate(([True], ~dup))
        time, vin, vout, ctl, iin = time[keep], vin[keep], vout[keep], ctl[keep], iin[keep]
    for label, arr in (("vin", vin), ("vout", vout), ("ctl", ctl), ("iin", iin)):
        if not np.all(np.isfinite(arr)):
            raise EngineError(f"trace {label} contains NaN/Inf values (simulation diverged)")
    return Waveforms(
        time=time,
        signals={"vin": vin, "vout": vout, "ctl": ctl, "iin": iin},
        engine=engine,
    )

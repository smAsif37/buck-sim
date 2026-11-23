"""Generate simulator-ready testbench netlists for each test.

All stimulus is built from two PWL sources, which keeps the netlists portable
between LTspice and ngspice:

* the input source (``Vin``) ramps from 0 V so the DC operating point is the
  trivial all-zero solution (robust convergence, realistic start-up), then
  holds / steps as the test requires;
* a *programmable resistive load* ``Bbs_load`` draws ``I = V(bs_ctl) * V(vout) / Vnom``
  where ``V(bs_ctl)`` is a PWL "set-point" in amperes. At ``Vout == Vnom`` the
  load current equals the set-point, and the load collapses gracefully while the
  output is still starting up.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .netlist import (
    NetlistError,
    ParsedNetlist,
    find_element,
    find_subckts,
    top_level_indices,
)
from .spec import Design, JobSpec, Timing


class TestbenchError(ValueError):
    """The requested test cannot be built from this netlist / design."""

    __test__ = False  # not a pytest test class


CTL_NODE = "bs_ctl"
CTL_SOURCE = "Vbs_ctl"
LOAD_NAME = "Bbs_load"
SUBCKT_VIN_NODE = "bs_vin"
SUBCKT_VOUT_NODE = "bs_vout"
SUBCKT_VIN_SOURCE = "Vbs_in"
_RESERVED_RE = re.compile(r"\bbs_|\bVbs_|\bBbs_|\bXbs_", re.IGNORECASE)

PWL = list[tuple[float, float]]


def fmt(x: float) -> str:
    return f"{x:.10g}"


def pwl_text(points: PWL) -> str:
    return "PWL(" + " ".join(f"{fmt(t)} {fmt(v)}" for t, v in points) + ")"


class PwlBuilder:
    """Build a strictly time-increasing PWL waveform from holds and ramps."""

    def __init__(self, initial: float = 0.0) -> None:
        self.points: PWL = [(0.0, initial)]

    @property
    def value(self) -> float:
        return self.points[-1][1]

    def hold_until(self, t: float) -> PwlBuilder:
        if t > self.points[-1][0]:
            self.points.append((t, self.value))
        return self

    def ramp(self, t_start: float, duration: float, to: float) -> PwlBuilder:
        if duration <= 0:
            raise TestbenchError("internal error: ramp duration must be positive")
        if t_start < self.points[-1][0] - 1e-18:
            raise TestbenchError("internal error: PWL points are not time-ordered")
        self.hold_until(t_start)
        self.points.append((t_start + duration, to))
        return self

    def build(self) -> PWL:
        for (t0, _), (t1, _) in zip(self.points, self.points[1:], strict=False):
            if not t1 > t0:
                raise TestbenchError("internal error: PWL times must be strictly increasing")
        return list(self.points)


@dataclass(frozen=True)
class SignalNames:
    """Where to find each quantity in the simulator output."""

    vin_node: str
    vout_node: str
    vin_source: str


@dataclass
class Bundle:
    """One simulator run: netlist text plus everything needed to analyse it."""

    name: str
    netlist: str
    tstop: float
    tmax: float
    vin_pwl: PWL
    ctl_pwl: PWL
    signals: SignalNames
    design: Design
    events: dict[str, float] = field(default_factory=dict)
    params: dict[str, float] = field(default_factory=dict)


ExtraLines = Callable[[SignalNames], list[str]]


class TestbenchBuilder:
    """Prepare a parsed netlist once, then emit bundles for each test."""

    __test__ = False  # not a pytest test class

    def __init__(self, parsed: ParsedNetlist, spec: JobSpec, extra_lines: ExtraLines | None = None) -> None:
        self.stmts = list(parsed.statements)
        self.spec = spec
        self.design: Design = spec.design
        self.timing: Timing = spec.design.timing()
        self._extra = extra_lines or (lambda _s: [])
        self._check_reserved()
        if spec.mode == "circuit":
            self._prepare_circuit()
        else:
            self._prepare_subckt()

    # ------------------------------------------------------------------ prep
    def _check_reserved(self) -> None:
        for s in self.stmts:
            if _RESERVED_RE.search(s):
                raise TestbenchError(
                    "the netlist uses names starting with 'bs_' which are reserved by the simulator service; "
                    "please rename them"
                )

    def _prepare_circuit(self) -> None:
        m = self.spec.circuit
        assert m is not None
        gnd = m.gnd_node.lower()
        try:
            self._src_idx, src = find_element(self.stmts, m.vin_source)
            self._load_idx, load = find_element(self.stmts, m.load_element)
        except NetlistError as e:
            raise TestbenchError(str(e)) from e
        if src[0][0].upper() != "V" or len(src) < 3:
            raise TestbenchError(f"{m.vin_source!r} is not a voltage source (V...) with two nodes")
        if src[2].lower() != gnd:
            raise TestbenchError(
                f"the negative node of {src[0]} is {src[2]!r}, expected the ground node {m.gnd_node!r}"
            )
        if load[0][0].upper() not in "RIB" or len(load) < 3:
            raise TestbenchError(f"{m.load_element!r} must be an R, I or B element with two nodes")
        load_nodes = {load[1].lower(), load[2].lower()}
        if load_nodes != {m.vout_node.lower(), gnd}:
            raise TestbenchError(
                f"load element {load[0]} must connect {m.vout_node!r} and {m.gnd_node!r}, "
                f"but it connects {load[1]!r} and {load[2]!r}"
            )
        if self._src_idx == self._load_idx:
            raise TestbenchError("input source and load must be different elements")
        self._src_name = src[0]
        self._src_nodes = (src[1], src[2])
        self.names = SignalNames(vin_node=src[1], vout_node=m.vout_node, vin_source=src[0])
        self._gnd = m.gnd_node

    def _prepare_subckt(self) -> None:
        m = self.spec.subckt
        assert m is not None
        subckts = find_subckts(self.stmts)
        sc = subckts.get(m.subckt.lower())
        if sc is None:
            raise TestbenchError(
                f"subcircuit {m.subckt!r} not found. Defined: {sorted(s.name for s in subckts.values())}"
            )
        pins_l = [p.lower() for p in sc.pins]
        for role, pin in (("vin_pin", m.vin_pin), ("vout_pin", m.vout_pin), ("gnd_pin", m.gnd_pin)):
            if pin.lower() not in pins_l:
                raise TestbenchError(f"{role} {pin!r} is not a pin of {sc.name} (pins: {sc.pins})")
        if len({m.vin_pin.lower(), m.vout_pin.lower(), m.gnd_pin.lower()}) != 3:
            raise TestbenchError("vin_pin, vout_pin and gnd_pin must be three different pins")
        ties = {k.lower(): v for k, v in m.ties.items()}
        for pin in ties:
            if pin not in pins_l:
                raise TestbenchError(f"tied pin {pin!r} is not a pin of {sc.name} (pins: {sc.pins})")
            if pin in (m.vin_pin.lower(), m.vout_pin.lower(), m.gnd_pin.lower()):
                raise TestbenchError(f"pin {pin!r} is already mapped as vin/vout/gnd; do not tie it")
        missing = [p for p in sc.pins if p.lower() not in ties and p.lower() not in
                   (m.vin_pin.lower(), m.vout_pin.lower(), m.gnd_pin.lower())]
        if missing:
            raise TestbenchError(
                f"pins {missing} of {sc.name} are not connected; add them to 'ties' "
                "(vin, vout, gnd or a constant voltage such as 5)"
            )
        self._sc = sc
        self._ties = ties
        self.names = SignalNames(vin_node=SUBCKT_VIN_NODE, vout_node=SUBCKT_VOUT_NODE, vin_source=SUBCKT_VIN_SOURCE)
        self._gnd = "0"

    # -------------------------------------------------------------- assembly
    def _assemble(
        self,
        name: str,
        vin_pwl: PWL,
        ctl_pwl: PWL,
        tstop: float,
        events: dict[str, float],
        params: dict[str, float] | None = None,
    ) -> Bundle:
        d = self.design
        lines: list[str] = [f"* buck-sim testbench: {name}"]
        load_expr = f"I=V({CTL_NODE})*V({self.names.vout_node})/{fmt(d.vout_nom)}"
        if self.spec.mode == "circuit":
            for i, stmt in enumerate(self.stmts):
                if i == self._src_idx:
                    n1, n2 = self._src_nodes
                    lines.append(f"{self._src_name} {n1} {n2} {pwl_text(vin_pwl)}")
                elif i == self._load_idx:
                    lines.append(f"{LOAD_NAME} {self.names.vout_node} {self._gnd} {load_expr}")
                else:
                    lines.append(stmt)
        else:
            m = self.spec.subckt
            assert m is not None
            lines.extend(self.stmts)
            lines.append(f"{SUBCKT_VIN_SOURCE} {SUBCKT_VIN_NODE} 0 {pwl_text(vin_pwl)}")
            conns: list[str] = []
            tie_sources: list[str] = []
            for pin in self._sc.pins:
                pl = pin.lower()
                if pl == m.vin_pin.lower():
                    conns.append(SUBCKT_VIN_NODE)
                elif pl == m.vout_pin.lower():
                    conns.append(SUBCKT_VOUT_NODE)
                elif pl == m.gnd_pin.lower():
                    conns.append("0")
                else:
                    target = self._ties[pl]
                    if target == "vin":
                        conns.append(SUBCKT_VIN_NODE)
                    elif target == "vout":
                        conns.append(SUBCKT_VOUT_NODE)
                    elif target == "gnd":
                        conns.append("0")
                    else:
                        node = f"bs_tie_{len(tie_sources)}"
                        tie_sources.append(f"Vbs_tie_{len(tie_sources)} {node} 0 {target}")
                        conns.append(node)
            lines.append(f"Xbs_dut {' '.join(conns)} {self._sc.name}")
            lines.extend(tie_sources)
            lines.append(f"{LOAD_NAME} {SUBCKT_VOUT_NODE} 0 {load_expr}")
        lines.append(f"{CTL_SOURCE} {CTL_NODE} 0 {pwl_text(ctl_pwl)}")
        lines.append(f".tran {fmt(self.timing.tmax)} {fmt(tstop)} 0 {fmt(self.timing.tmax)}")
        lines.extend(self._extra(self.names))
        lines.append(".end")
        return Bundle(
            name=name,
            netlist="\n".join(lines) + "\n",
            tstop=tstop,
            tmax=self.timing.tmax,
            vin_pwl=vin_pwl,
            ctl_pwl=ctl_pwl,
            signals=self.names,
            design=d,
            events=events,
            params=params or {},
        )

    # ----------------------------------------------------------------- tests
    def steady_state(self, vin: float, iload: float, name: str = "steady_state") -> Bundle:
        t = self.timing
        vin_pwl = PwlBuilder(0.0).ramp(0.0 + 1e-12, t.ramp, vin).build()
        ctl_pwl = PwlBuilder(0.0).ramp(0.0 + 1e-12, t.ramp, iload).build()
        tstop = t.startup + t.meas_window
        events = {"meas_start": t.startup, "meas_end": tstop}
        return self._assemble(name, vin_pwl, ctl_pwl, tstop, events, {"vin_v": vin, "iload_a": iload})

    def load_transient(self) -> Bundle:
        d, t = self.design, self.timing
        assert d.iout_min is not None
        di = d.iout_max - d.iout_min
        ramp_t = di / (d.load_slew_a_per_us * 1e6)
        self._check_edge_time("load slew", ramp_t)
        t_up = t.startup + t.meas_window
        t_dn = t_up + t.hold
        tstop = t_dn + t.hold
        vin_pwl = PwlBuilder(0.0).ramp(1e-12, t.ramp, d.vin_nom).build()
        ctl_pwl = (
            PwlBuilder(0.0)
            .ramp(1e-12, t.ramp, d.iout_min)
            .ramp(t_up, ramp_t, d.iout_max)
            .ramp(t_dn, ramp_t, d.iout_min)
            .build()
        )
        events = {"pre_start": t.startup, "t_up": t_up, "t_down": t_dn, "tstop": tstop}
        return self._assemble("load_transient", vin_pwl, ctl_pwl, tstop, events)

    def line_transient(self) -> Bundle:
        d, t = self.design, self.timing
        dv = d.vin_max - d.vin_min
        if dv <= 0:
            raise TestbenchError("line transient needs vin_max > vin_min")
        ramp_t = dv / (d.line_slew_v_per_us * 1e6)
        self._check_edge_time("line slew", ramp_t)
        t_up = t.startup + t.meas_window
        t_dn = t_up + t.hold
        tstop = t_dn + t.hold
        vin_pwl = (
            PwlBuilder(0.0)
            .ramp(1e-12, t.ramp, d.vin_min)
            .ramp(t_up, ramp_t, d.vin_max)
            .ramp(t_dn, ramp_t, d.vin_min)
            .build()
        )
        ctl_pwl = PwlBuilder(0.0).ramp(1e-12, t.ramp, d.iout_max).build()
        events = {"pre_start": t.startup, "t_up": t_up, "t_down": t_dn, "tstop": tstop}
        return self._assemble("line_transient", vin_pwl, ctl_pwl, tstop, events)

    def line_sweep(self) -> list[Bundle]:
        d = self.design
        if d.vin_max <= d.vin_min:
            raise TestbenchError("line regulation needs vin_max > vin_min")
        vins = np.linspace(d.vin_min, d.vin_max, d.sweep_points_line)
        return [self.steady_state(float(v), d.iout_max, f"line_reg_{i:02d}") for i, v in enumerate(vins)]

    def load_sweep(self) -> list[Bundle]:
        d = self.design
        assert d.iout_min is not None
        loads = np.linspace(d.iout_min, d.iout_max, d.sweep_points_load)
        return [self.steady_state(d.vin_nom, float(i), f"load_reg_{k:02d}") for k, i in enumerate(loads)]

    # ----------------------------------------------------------------- utils
    def _check_edge_time(self, what: str, ramp_t: float) -> None:
        if ramp_t > self.timing.hold / 4:
            raise TestbenchError(
                f"{what} is too slow: the step takes {ramp_t * 1e6:.3g} us but each level is only held "
                f"{self.timing.hold * 1e6:.3g} us. Increase the slew rate or hold_time_s."
            )

    def indices_outside_subckts(self) -> list[int]:
        return top_level_indices(self.stmts)

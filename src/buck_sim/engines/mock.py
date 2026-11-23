"""Synthetic engine for demos, UI development and pipeline tests.

NOT A SPICE SIMULATION. It integrates a simple averaged current-mode buck model
(inductor + capacitor + series loss, PI voltage loop, load feed-forward) driven by
the exact same stimulus the real testbench uses, then superimposes an analytic
switching ripple. Every result produced with it is flagged ``synthetic``.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from ..testbench import Bundle
from .base import Engine, Waveforms


class MockEngine(Engine):
    name = "mock"
    synthetic = True

    def version(self) -> str:
        return "mock averaged model (synthetic data, not a SPICE simulation)"

    def simulate(self, bundle: Bundle, workdir: Path, timeout_s: float) -> Waveforms:  # noqa: ARG002
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "tb.cir").write_text(bundle.netlist, encoding="ascii", errors="replace")
        d = bundle.design
        fsw = d.fsw_hz
        period = 1.0 / fsw
        dt = period / 50.0
        n = int(round(bundle.tstop / dt))
        t = np.arange(n + 1) * dt

        vin_t = np.array([p[0] for p in bundle.vin_pwl])
        vin_v = np.array([p[1] for p in bundle.vin_pwl])
        ctl_t = np.array([p[0] for p in bundle.ctl_pwl])
        ctl_v = np.array([p[1] for p in bundle.ctl_pwl])
        vin = np.interp(t, vin_t, vin_v)
        ctl = np.interp(t, ctl_t, ctl_v)

        vnom = d.vout_nom
        d0 = vnom / d.vin_nom
        ind = vnom * (1.0 - d0) / (0.3 * d.iout_max * fsw)
        f0 = fsw / 40.0
        cap = 1.0 / ((2 * math.pi * f0) ** 2 * ind)
        r_loss = 0.03 * vnom / d.iout_max
        esr = 0.005 * vnom / d.iout_max

        w_c = 2 * math.pi * fsw / 20.0
        kv = w_c * cap
        ki = kv * w_c / 5.0
        w_i = 2 * math.pi * fsw / 6.0
        i_cap = 1.5 * d.iout_max
        soft_start = 0.4 * bundle.design.timing().startup

        vc = 0.0
        il = 0.0
        integ = 0.0
        vout_prev = 0.0
        vout = np.zeros(n + 1)
        iin = np.zeros(n + 1)
        duty = np.zeros(n + 1)
        for k in range(1, n + 1):
            vref = vnom * min(1.0, t[k] / soft_start)
            iload = ctl[k] * vout_prev / vnom
            err = vref - vout_prev
            integ += ki * err * dt
            il_ref = iload + kv * err + integ
            il_ref = min(max(il_ref, 0.0), i_cap)
            dil_des = w_i * (il_ref - il)
            vin_eff = max(vin[k], 1e-3)
            dty = (ind * dil_des + vout_prev + r_loss * il) / vin_eff
            dty = min(max(dty, 0.0), 0.95)
            dil = (dty * vin[k] - vout_prev - r_loss * il) / ind
            il = max(il + dil * dt, 0.0)
            vc += (il - iload) / cap * dt
            vout_prev = max(vc + esr * (il - iload), 0.0)
            vout[k] = vout_prev
            iin[k] = -dty * il
            duty[k] = dty

        # analytic switching ripple (triangular, at fsw)
        delta_il = vout * (1.0 - duty) / (ind * fsw)
        ripple_pp = delta_il * (esr + 1.0 / (8.0 * fsw * cap))
        phase = (t / period) % 1.0
        tri = 4.0 * np.abs(phase - 0.5) - 1.0
        vout_total = vout + 0.5 * ripple_pp * tri

        return Waveforms(
            time=t,
            signals={"vin": vin, "vout": vout_total, "ctl": ctl, "iin": iin},
            engine=self.name,
            synthetic=True,
            notes=["SYNTHETIC DATA from an averaged model - not a SPICE simulation."],
        )

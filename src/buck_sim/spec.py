"""Validated job specification (what the user asks us to simulate)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .registry import METRICS, TEST_NAMES

_NODE_RE = re.compile(r"^[A-Za-z0-9_.$\-]{1,64}$")
_ELEM_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.$\-]{0,63}$")
_TIE_RE = re.compile(r"^(vin|vout|gnd|[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?)$")


def _check_node(value: str, what: str) -> str:
    if not _NODE_RE.match(value):
        raise ValueError(f"{what} {value!r} has characters that are not allowed (A-Z a-z 0-9 _ . $ -)")
    return value


class CircuitMapping(BaseModel):
    """Mode 'circuit': the upload is a complete circuit (source + load included).

    We replace the named input source and load element with our own stimulus.
    """

    model_config = ConfigDict(extra="forbid")

    vin_source: str = Field(description="Name of the input voltage source element, e.g. V1")
    vout_node: str = Field(description="Name of the output node, e.g. out")
    load_element: str = Field(description="Name of the load element between vout and ground, e.g. Rload")
    gnd_node: str = Field(default="0", description="Name of the ground node")

    @field_validator("vin_source", "load_element")
    @classmethod
    def _elem(cls, v: str) -> str:
        if not _ELEM_RE.match(v):
            raise ValueError(f"element name {v!r} is not valid")
        return v

    @field_validator("vout_node", "gnd_node")
    @classmethod
    def _node(cls, v: str) -> str:
        return _check_node(v, "node name")


class SubcktMapping(BaseModel):
    """Mode 'subckt': the upload defines a .subckt; we build source/load around it."""

    model_config = ConfigDict(extra="forbid")

    subckt: str
    vin_pin: str
    vout_pin: str
    gnd_pin: str
    ties: dict[str, str] = Field(
        default_factory=dict,
        description="Other pins -> 'vin' | 'vout' | 'gnd' | a constant voltage such as '5'",
    )

    @field_validator("subckt", "vin_pin", "vout_pin", "gnd_pin")
    @classmethod
    def _names(cls, v: str) -> str:
        return _check_node(v, "name")

    @field_validator("ties")
    @classmethod
    def _ties(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > 32:
            raise ValueError("too many tied pins (max 32)")
        out: dict[str, str] = {}
        for pin, target in v.items():
            _check_node(pin, "pin name")
            t = str(target).strip()
            if not _TIE_RE.match(t.lower()):
                raise ValueError(f"tie target {target!r} for pin {pin!r} must be vin, vout, gnd or a number")
            out[pin] = t.lower() if t.lower() in ("vin", "vout", "gnd") else t
        return out


@dataclass(frozen=True)
class Timing:
    """Derived simulation timing (all in seconds)."""

    period: float  # switching period
    tmax: float  # maximum time step handed to the simulator
    meas_window: float  # averaging window for steady values
    startup: float  # time allowed for start-up before measuring
    hold: float  # time held at each level in transient tests
    ramp: float  # ramp time for the initial Vin / load ramp


class Design(BaseModel):
    """Converter operating range and measurement settings."""

    model_config = ConfigDict(extra="forbid")

    vin_min: float = Field(gt=0, le=1000)
    vin_nom: float = Field(gt=0, le=1000)
    vin_max: float = Field(gt=0, le=1000)
    vout_nom: float = Field(gt=0, le=1000)
    iout_max: float = Field(gt=0, le=1000)
    iout_min: float | None = Field(default=None, ge=0, description="Defaults to 10% of iout_max")
    fsw_hz: float = Field(gt=1e3, le=1e8, description="Switching frequency (Hz)")

    startup_time_s: float | None = Field(default=None, gt=0, le=0.1)
    hold_time_s: float | None = Field(default=None, gt=0, le=0.1)
    measure_cycles: int = Field(default=50, ge=10, le=2000)
    steps_per_cycle: int = Field(default=100, ge=20, le=1000)

    load_slew_a_per_us: float = Field(default=1.0, gt=0, le=1000)
    line_slew_v_per_us: float = Field(default=1.0, gt=0, le=1000)
    settle_band_pct: float = Field(default=2.0, gt=0, le=50)

    sweep_points_line: int = Field(default=5, ge=2, le=21)
    sweep_points_load: int = Field(default=6, ge=2, le=21)

    @model_validator(mode="after")
    def _consistency(self) -> Design:
        if not (self.vin_min <= self.vin_nom <= self.vin_max):
            raise ValueError("require vin_min <= vin_nom <= vin_max")
        if self.vout_nom >= self.vin_min:
            raise ValueError("vout_nom must be below vin_min for a buck converter")
        if self.iout_min is None:
            object.__setattr__(self, "iout_min", round(0.1 * self.iout_max, 12))
        assert self.iout_min is not None
        if self.iout_min >= self.iout_max:
            raise ValueError("iout_min must be below iout_max")
        t = self.timing()
        if self.hold_time_s is not None and self.hold_time_s < 2 * t.meas_window:
            raise ValueError(
                f"hold_time_s must be at least 2 x the measurement window "
                f"({2 * t.meas_window:.3g} s = 2 x {self.measure_cycles} cycles)"
            )
        if self.startup_time_s is not None and self.startup_time_s < 10 * t.ramp:
            raise ValueError("startup_time_s is too short for the Vin ramp; increase it")
        return self

    def timing(self) -> Timing:
        period = 1.0 / self.fsw_hz
        meas = self.measure_cycles * period
        startup = self.startup_time_s if self.startup_time_s is not None else max(1e-3, 200 * period)
        hold = self.hold_time_s if self.hold_time_s is not None else max(5e-4, 4 * meas)
        ramp = min(100e-6, max(1e-6, startup / 10))
        return Timing(
            period=period,
            tmax=period / self.steps_per_cycle,
            meas_window=meas,
            startup=startup,
            hold=hold,
            ramp=ramp,
        )


class Limit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: float | None = None
    max: float | None = None

    @model_validator(mode="after")
    def _check(self) -> Limit:
        if self.min is None and self.max is None:
            raise ValueError("a limit needs min and/or max")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("limit min must be <= max")
        return self


class JobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(default="", max_length=80)
    mode: Literal["circuit", "subckt"] = "circuit"
    circuit: CircuitMapping | None = None
    subckt: SubcktMapping | None = None
    design: Design
    tests: list[str] = Field(default_factory=lambda: list(TEST_NAMES))
    limits: dict[str, Limit] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return re.sub(r"[\x00-\x1f\x7f]", "", v).strip()

    @field_validator("tests")
    @classmethod
    def _tests(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("select at least one test")
        unknown = [t for t in v if t not in TEST_NAMES]
        if unknown:
            raise ValueError(f"unknown tests: {unknown}; valid: {list(TEST_NAMES)}")
        if len(set(v)) != len(v):
            raise ValueError("duplicate tests")
        return [t for t in TEST_NAMES if t in v]  # canonical order

    @field_validator("limits")
    @classmethod
    def _limits(cls, v: dict[str, Limit]) -> dict[str, Limit]:
        bad = [k for k in v if k not in METRICS]
        if bad:
            raise ValueError(f"unknown metric keys in limits: {bad}")
        return v

    @model_validator(mode="after")
    def _mode(self) -> JobSpec:
        if self.mode == "circuit":
            if self.circuit is None:
                raise ValueError("mode 'circuit' needs a 'circuit' mapping")
            if self.subckt is not None:
                raise ValueError("do not give 'subckt' in mode 'circuit'")
        else:
            if self.subckt is None:
                raise ValueError("mode 'subckt' needs a 'subckt' mapping")
            if self.circuit is not None:
                raise ValueError("do not give 'circuit' in mode 'subckt'")
        return self

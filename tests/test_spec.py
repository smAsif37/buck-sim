import copy

import pytest
from pydantic import ValidationError

from buck_sim.spec import Design, JobSpec

BASE = dict(vin_min=9, vin_nom=12, vin_max=15, vout_nom=5, iout_max=2, fsw_hz=500e3)


def test_defaults_and_timing():
    d = Design(**BASE)
    assert d.iout_min == pytest.approx(0.2)
    t = d.timing()
    assert t.period == pytest.approx(2e-6)
    assert t.meas_window == pytest.approx(100e-6)
    assert t.startup == pytest.approx(1e-3)
    assert t.hold == pytest.approx(5e-4)
    assert t.tmax == pytest.approx(2e-8)
    assert 1e-6 <= t.ramp <= 100e-6


def test_low_frequency_scales_windows():
    t = Design(**{**BASE, "fsw_hz": 50e3}).timing()
    assert t.meas_window == pytest.approx(1e-3)
    assert t.hold >= 2 * t.meas_window


@pytest.mark.parametrize(
    "patch,msg",
    [
        ({"vin_min": 13}, "vin_min <= vin_nom"),
        ({"vout_nom": 9}, "below vin_min"),
        ({"iout_min": 3}, "iout_min must be below"),
        ({"hold_time_s": 50e-6}, "hold_time_s"),
        ({"startup_time_s": 5e-6}, "startup_time_s"),
        ({"fsw_hz": 10}, "greater than 1000"),
        ({"surprise": 1}, "Extra inputs"),
    ],
)
def test_design_validation(patch, msg):
    with pytest.raises(ValidationError, match=msg):
        Design(**{**BASE, **patch})


def test_jobspec_mode_rules(spec_dict):
    d = copy.deepcopy(spec_dict)
    d["circuit"] = None
    with pytest.raises(ValidationError, match="needs a 'circuit'"):
        JobSpec.model_validate(d)
    d = copy.deepcopy(spec_dict)
    d["subckt"] = {"subckt": "A", "vin_pin": "a", "vout_pin": "b", "gnd_pin": "c"}
    with pytest.raises(ValidationError, match="do not give 'subckt'"):
        JobSpec.model_validate(d)


def test_tests_and_limits_validation(spec_dict):
    d = copy.deepcopy(spec_dict)
    d["tests"] = ["load_transient", "steady_state"]
    assert JobSpec.model_validate(d).tests == ["steady_state", "load_transient"]  # canonical order
    for bad in ([], ["nope"], ["steady_state", "steady_state"]):
        d["tests"] = bad
        with pytest.raises(ValidationError):
            JobSpec.model_validate(d)
    d = copy.deepcopy(spec_dict)
    d["limits"] = {"steady_state.nope": {"max": 1}}
    with pytest.raises(ValidationError, match="unknown metric"):
        JobSpec.model_validate(d)
    d["limits"] = {"steady_state.ripple_pp_v": {}}
    with pytest.raises(ValidationError, match="min and/or max"):
        JobSpec.model_validate(d)
    d["limits"] = {"steady_state.ripple_pp_v": {"min": 2, "max": 1}}
    with pytest.raises(ValidationError, match="min must be"):
        JobSpec.model_validate(d)


def test_names_are_validated(spec_dict):
    d = copy.deepcopy(spec_dict)
    d["circuit"]["vout_node"] = "out; rm -rf"
    with pytest.raises(ValidationError):
        JobSpec.model_validate(d)
    d = copy.deepcopy(spec_dict)
    d["circuit"]["vin_source"] = "1V"
    with pytest.raises(ValidationError):
        JobSpec.model_validate(d)
    d = copy.deepcopy(spec_dict)
    d["name"] = "a\x00b\nc"
    assert JobSpec.model_validate(d).name == "abc"


def test_tie_values():
    from buck_sim.spec import SubcktMapping

    m = SubcktMapping(subckt="A", vin_pin="a", vout_pin="b", gnd_pin="c", ties={"en": "VIN", "x": "3.3", "y": "-1e-1"})
    assert m.ties == {"en": "vin", "x": "3.3", "y": "-1e-1"}
    with pytest.raises(ValidationError):
        SubcktMapping(subckt="A", vin_pin="a", vout_pin="b", gnd_pin="c", ties={"en": "5V; x"})

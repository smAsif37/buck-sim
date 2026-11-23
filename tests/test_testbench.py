import copy

import pytest

from buck_sim.netlist import NetlistLoader
from buck_sim.spec import Design, JobSpec
from buck_sim.testbench import PwlBuilder, TestbenchBuilder, TestbenchError


def build(files, main, spec, extra=None):
    parsed = NetlistLoader(files).load_main(main)
    return TestbenchBuilder(parsed, spec, extra)


def with_spec(spec_dict, **patch):
    d = copy.deepcopy(spec_dict)
    for k, v in patch.items():
        if k in Design.model_fields:
            d["design"][k] = v
        else:
            d[k] = v
    return JobSpec.model_validate(d)


def test_circuit_mode_netlist(example_files, spec):
    tb = build(example_files, "buck_demo.cir", spec)
    b = tb.steady_state(12.0, 2.0)
    lines = b.netlist.strip().splitlines()
    assert lines[0].startswith("* buck-sim testbench")
    assert lines[-1] == ".end"
    # source replaced (nodes and name preserved), load replaced by the programmable load
    assert any(ln.startswith("V1 in 0 PWL(") for ln in lines)
    assert not any(ln.startswith("Rload ") for ln in lines)
    assert "Bbs_load out 0 I=V(bs_ctl)*V(out)/5" in lines
    assert any(ln.startswith("Vbs_ctl bs_ctl 0 PWL(") for ln in lines)
    assert sum(ln.startswith(".tran") for ln in lines) == 1
    # user's controller IC was inlined; no .include left
    assert not any(ln.lower().startswith(".include") for ln in lines)
    assert ".subckt TOYCTRL fb comp gate gnd" in lines
    assert b.signals.vin_source == "V1" and b.signals.vout_node == "out" and b.signals.vin_node == "in"


def test_steady_timeline(example_files, spec):
    tb = build(example_files, "buck_demo.cir", spec)
    b = tb.steady_state(12.0, 2.0)
    d = spec.design
    t = d.timing()
    assert b.tstop == pytest.approx(t.startup + t.meas_window)
    assert b.events["meas_start"] == pytest.approx(t.startup)
    assert b.tmax == pytest.approx(1 / d.fsw_hz / d.steps_per_cycle)
    assert b.vin_pwl[0] == (0.0, 0.0) and b.vin_pwl[-1][1] == 12.0
    assert b.ctl_pwl[-1][1] == 2.0


def test_load_transient_timeline(example_files, spec):
    tb = build(example_files, "buck_demo.cir", spec)
    b = tb.load_transient()
    d = spec.design
    t = d.timing()
    assert b.events["t_up"] == pytest.approx(t.startup + t.meas_window)
    assert b.events["t_down"] == pytest.approx(b.events["t_up"] + t.hold)
    assert b.tstop == pytest.approx(b.events["t_down"] + t.hold)
    levels = [v for _, v in b.ctl_pwl]
    assert max(levels) == d.iout_max and d.iout_min in levels
    times = [p[0] for p in b.ctl_pwl]
    assert all(b > a for a, b in zip(times, times[1:], strict=False))
    # 1.5 A at 1 A/us -> 1.5 us ramp
    up = [p for p in b.ctl_pwl if p[0] >= b.events["t_up"]][:2]
    assert up[1][0] - up[0][0] == pytest.approx(1.5e-6)


def test_line_transient_and_sweeps(example_files, spec):
    tb = build(example_files, "buck_demo.cir", spec)
    b = tb.line_transient()
    assert min(v for _, v in b.vin_pwl[2:]) == 9 and max(v for _, v in b.vin_pwl) == 15
    line = tb.line_sweep()
    assert [x.params["vin_v"] for x in line] == pytest.approx([9, 10.5, 12, 13.5, 15])
    assert all(x.params["iload_a"] == 2 for x in line)
    load = tb.load_sweep()
    assert len(load) == 6
    assert load[0].params["iload_a"] == pytest.approx(0.5) and load[-1].params["iload_a"] == pytest.approx(2)
    assert len({x.name for x in line + load}) == 11


def test_engine_extra_lines_are_inserted_before_end(example_files, spec):
    tb = build(example_files, "buck_demo.cir", spec, lambda s: [f".save V({s.vout_node})"])
    lines = tb.steady_state(12, 2).netlist.strip().splitlines()
    assert lines[-2] == ".save V(out)" and lines[-1] == ".end"


def test_circuit_mapping_errors(example_files, spec_dict):
    def attempt(**circuit):
        d = copy.deepcopy(spec_dict)
        d["circuit"].update(circuit)
        return build(example_files, "buck_demo.cir", JobSpec.model_validate(d))

    with pytest.raises(TestbenchError, match="not found"):
        attempt(vin_source="V9")
    with pytest.raises(TestbenchError, match="not a voltage source"):
        attempt(vin_source="Rload")
    with pytest.raises(TestbenchError, match="must connect"):
        attempt(load_element="RL")  # RL is the inductor DCR (lx-out)
    with pytest.raises(TestbenchError, match="must connect"):
        attempt(vout_node="sw")
    with pytest.raises(TestbenchError, match="ground node"):
        attempt(gnd_node="gnd")


def test_reserved_prefix_rejected(spec):
    files = {"x.cir": b"* t\nV1 in 0 1\nRload out 0 1\nRx in bs_ctl 1\n"}
    with pytest.raises(TestbenchError, match="reserved"):
        build(files, "x.cir", spec)


def test_subckt_mode(subckt_files, subckt_spec):
    tb = build(subckt_files, "buck_demo_subckt.cir", subckt_spec)
    b = tb.steady_state(12.0, 2.0)
    lines = b.netlist.splitlines()
    assert "Xbs_dut bs_vin bs_vout 0 BUCK" in lines
    assert any(ln.startswith("Vbs_in bs_vin 0 PWL(") for ln in lines)
    assert "Bbs_load bs_vout 0 I=V(bs_ctl)*V(bs_vout)/5" in lines
    assert b.signals.vin_source == "Vbs_in" and b.signals.vin_node == "bs_vin"


def test_subckt_ties_and_errors(spec_dict):
    files = {"ic.cir": b"* t\n.subckt IC vin vout gnd en pg\nR1 vin vout 1\nR2 vout gnd 1\nR3 en gnd 1\nR4 pg gnd 1\n.ends\n"}

    def spec_with(ties, **over):
        sc = {"subckt": "IC", "vin_pin": "vin", "vout_pin": "vout", "gnd_pin": "gnd", "ties": ties}
        sc.update(over)
        return JobSpec.model_validate({"mode": "subckt", "subckt": sc, "design": spec_dict["design"], "tests": ["steady_state"]})

    tb = build(files, "ic.cir", spec_with({"en": "vin", "pg": "3.3"}))
    lines = tb.steady_state(12, 2).netlist.splitlines()
    assert "Xbs_dut bs_vin bs_vout 0 bs_vin bs_tie_0 IC" in lines
    assert "Vbs_tie_0 bs_tie_0 0 3.3" in lines
    with pytest.raises(TestbenchError, match="not connected"):
        build(files, "ic.cir", spec_with({"en": "vin"}))
    with pytest.raises(TestbenchError, match="not a pin"):
        build(files, "ic.cir", spec_with({"en": "vin", "pg": "gnd", "zz": "gnd"}))
    with pytest.raises(TestbenchError, match="not a pin"):
        build(files, "ic.cir", spec_with({"en": "vin", "pg": "gnd"}, vin_pin="nope"))
    with pytest.raises(TestbenchError, match="three different"):
        build(files, "ic.cir", spec_with({"en": "vin", "pg": "gnd"}, vout_pin="vin"))
    with pytest.raises(TestbenchError, match="not found"):
        build(files, "ic.cir", spec_with({}, subckt="NOPE"))


def test_slow_slew_is_reported(example_files, spec_dict):
    s = with_spec(spec_dict, load_slew_a_per_us=0.001)
    tb = build(example_files, "buck_demo.cir", s)
    with pytest.raises(TestbenchError, match="too slow"):
        tb.load_transient()


def test_line_tests_need_a_range(example_files, spec_dict):
    s = with_spec(spec_dict, vin_min=12, vin_max=12)
    tb = build(example_files, "buck_demo.cir", s)
    with pytest.raises(TestbenchError, match="vin_max > vin_min"):
        tb.line_transient()
    with pytest.raises(TestbenchError):
        tb.line_sweep()


def test_pwl_builder_rejects_unordered():
    b = PwlBuilder(0).ramp(1e-6, 1e-6, 5)
    with pytest.raises(TestbenchError):
        b.ramp(0.5e-6, 1e-6, 1)
    with pytest.raises(TestbenchError):
        PwlBuilder(0).ramp(1e-6, 0, 5)

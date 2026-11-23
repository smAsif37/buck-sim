import os
import stat
import sys
import textwrap

import numpy as np
import pytest

from buck_sim.config import Settings
from buck_sim.engines import create_engine
from buck_sim.engines import ltspice as lt_mod
from buck_sim.engines import ngspice as ng_mod
from buck_sim.engines.base import EngineError, run_process, split_command
from buck_sim.engines.ltspice import LTspiceEngine
from buck_sim.engines.ngspice import NgspiceEngine
from buck_sim.engines.rawio import resolve_trace, waveforms_from_traces
from buck_sim.netlist import NetlistLoader
from buck_sim.testbench import SignalNames, TestbenchBuilder

NAMES = SignalNames(vin_node="in", vout_node="out", vin_source="V1")


def fake_traces(n=100, ltspice_style=True):
    t = np.linspace(0, 1e-3, n)
    src = "I(V1)" if ltspice_style else "v1#branch"
    return t, {
        "time": t,
        "V(in)" if ltspice_style else "v(in)": np.full(n, 12.0),
        "V(out)" if ltspice_style else "v(out)": np.full(n, 5.0),
        "V(bs_ctl)" if ltspice_style else "v(bs_ctl)": np.full(n, 2.0),
        src: np.full(n, -0.9),
        "V(sw)" if ltspice_style else "v(sw)": np.zeros(n),
    }


# -------------------------------------------------------------------- raw mapping
@pytest.mark.parametrize("style", [True, False])
def test_trace_mapping_both_dialects(style):
    t, traces = fake_traces(ltspice_style=style)
    wf = waveforms_from_traces(t, traces, NAMES, "x")
    assert set(wf.signals) == {"vin", "vout", "ctl", "iin"}
    assert wf.signals["vout"][0] == 5.0 and wf.signals["iin"][0] == -0.9


def test_case_insensitive_names():
    t, traces = fake_traces()
    traces = {k.upper() if k != "time" else k: v for k, v in traces.items()}
    wf = waveforms_from_traces(t, traces, SignalNames("IN", "Out", "v1"), "x")
    assert wf.signals["vin"][0] == 12.0


def test_missing_trace_lists_available():
    t, traces = fake_traces()
    del traces["I(V1)"]
    with pytest.raises(EngineError, match=r"I\(V1\).*Available"):
        waveforms_from_traces(t, traces, NAMES, "x")
    assert resolve_trace(["A", "b"], ["x", "B"]) == "b"


def test_nan_and_unsorted_and_duplicate_time_handling():
    t, traces = fake_traces()
    traces["V(out)"][10] = np.nan
    with pytest.raises(EngineError, match="NaN"):
        waveforms_from_traces(t, traces, NAMES, "x")
    t, traces = fake_traces(50)
    t2 = t.copy()
    t2[20] = t2[19]  # duplicate breakpoint
    wf = waveforms_from_traces(t2, traces, NAMES, "x")
    assert np.all(np.diff(wf.time) > 0) and len(wf.time) == 49
    with pytest.raises(EngineError, match="only"):
        waveforms_from_traces(t[:5], {k: v[:5] for k, v in traces.items()}, NAMES, "x")


# ------------------------------------------------------------------- subprocess
def write_script(path, body):
    """Write a fake simulator and return a command string that runs it on any OS."""
    path.write_text("#!" + sys.executable + "\n" + textwrap.dedent(body))
    if os.name == "nt":  # no shebang support on Windows: run it through the interpreter
        return f'"{sys.executable}" "{path}"'
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def make_bundle(example_files, spec):
    tb = TestbenchBuilder(NetlistLoader(example_files).load_main("buck_demo.cir"), spec, lambda s: [])
    return tb.steady_state(12, 2)


def test_run_process_timeout_kills_process_group(tmp_path):
    script = write_script(tmp_path / "slow.py", "import time\ntime.sleep(30)\n")
    with pytest.raises(EngineError, match="timed out"):
        run_process(split_command(script), cwd=tmp_path, timeout_s=0.5)
    with pytest.raises(EngineError, match="not found"):
        run_process([str(tmp_path / "nope")], cwd=tmp_path, timeout_s=1)


def test_ltspice_engine_command_and_outputs(tmp_path, monkeypatch, example_files, spec):
    fake = write_script(
        tmp_path / "fake_ltspice.py",
        """
        import sys, pathlib
        net = pathlib.Path(sys.argv[-1])
        assert sys.argv[1:-1] == ["-b"], sys.argv
        net.with_suffix(".raw").write_bytes(b"RAW")
        net.with_suffix(".log").write_text("Total elapsed time: 1 s\\n")
        """,
    )
    monkeypatch.setattr(lt_mod, "read_raw_traces", lambda p, **kw: fake_traces_for(p))
    t, tr = fake_traces()

    def fake_traces_for(path):
        assert path.name == "tb.raw" and path.read_bytes() == b"RAW"
        return t, tr

    bundle = make_bundle(example_files, spec)
    eng = LTspiceEngine(fake, "-b")
    assert eng.args == ["-b"]
    wf = eng.simulate(bundle, tmp_path / "w", 20)
    assert wf.engine == "ltspice" and "Total elapsed" in wf.log_tail
    assert (tmp_path / "w" / "tb.cir").read_text() == bundle.netlist
    extra = eng.extra_lines(bundle.signals)
    assert ".options plotwinsize=0" in extra and any(x.startswith(".save V(in) V(out) V(bs_ctl) I(V1)") for x in extra)


def test_ltspice_engine_reports_missing_output_with_log(tmp_path, example_files, spec):
    fake = write_script(
        tmp_path / "bad.py",
        """
        import sys, pathlib
        net = pathlib.Path(sys.argv[-1])
        net.with_suffix(".log").write_text("Fatal Error: Singular matrix\\n")
        sys.exit(3)
        """,
    )
    with pytest.raises(EngineError, match="no output") as e:
        LTspiceEngine(fake).simulate(make_bundle(example_files, spec), tmp_path / "w", 20)
    assert "Singular matrix" in e.value.log_tail


@pytest.mark.skipif(os.name == "nt", reason="Wine paths only apply on a POSIX host")
def test_ltspice_wine_command_building(tmp_path, monkeypatch, example_files, spec):
    seen = {}

    def fake_run(cmd, *, cwd, timeout_s, env=None):
        seen.update(cmd=cmd, env=env)
        return 0, ""

    monkeypatch.setattr(lt_mod, "run_process", fake_run)
    eng = LTspiceEngine("xvfb-run -a wine /opt/ltspice/LTspice.exe")
    assert eng.use_wine and eng.args == ["-Run", "-b"]
    with pytest.raises(EngineError):  # no raw produced -> error, but command was recorded
        eng.simulate(make_bundle(example_files, spec), tmp_path / "w", 5)
    assert seen["cmd"][:5] == ["xvfb-run", "-a", "wine", "/opt/ltspice/LTspice.exe", "-Run"]
    assert seen["cmd"][-1].startswith("Z:\\") and seen["cmd"][-1].endswith("tb.cir")
    assert seen["env"] == {"WINEDEBUG": "-all"}
    assert LTspiceEngine("/x/LTspice", "-b -ascii").args == ["-b", "-ascii"]


def test_split_command_handles_quoted_windows_paths():
    if os.name == "nt":
        cmd = split_command(r'"C:\Program Files\ADI\LTspice\LTspice.exe" -b')
        assert cmd == [r"C:\Program Files\ADI\LTspice\LTspice.exe", "-b"]
    else:
        assert split_command("'/opt/my tools/ngspice' -b") == ["/opt/my tools/ngspice", "-b"]
    with pytest.raises(EngineError, match="empty"):
        split_command("   ")


def test_ngspice_engine_command_and_outputs(tmp_path, monkeypatch, example_files, spec):
    fake = write_script(
        tmp_path / "fake_ngspice.py",
        """
        import sys, pathlib
        a = sys.argv[1:]
        assert a[0] == "-b" and a[1] == "-r" and a[3] == "-o", a
        pathlib.Path(a[2]).write_bytes(b"RAW")
        pathlib.Path(a[4]).write_text("ngspice log ok\\n")
        """,
    )
    t, tr = fake_traces(ltspice_style=False)
    monkeypatch.setattr(ng_mod, "read_raw_traces", lambda p, **kw: (t, tr))
    wf = NgspiceEngine(fake).simulate(make_bundle(example_files, spec), tmp_path / "w", 20)
    assert wf.engine == "ngspice" and wf.log_tail == "ngspice log ok"
    with pytest.raises(EngineError, match="not found"):
        NgspiceEngine(str(tmp_path / "missing.exe")).simulate(make_bundle(example_files, spec), tmp_path / "w2", 5)


def test_create_engine_factory(tmp_path):
    base = Settings(data_dir=tmp_path)
    assert create_engine(Settings(data_dir=tmp_path, engine="ngspice")).name == "ngspice"
    assert create_engine(Settings(data_dir=tmp_path, engine="ltspice", ltspice_cmd="x")).name == "ltspice"
    assert create_engine(base.with_engine("mock")).synthetic is True


def test_settings_validation(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="LTSPICE_CMD"):
        Settings(data_dir=tmp_path, engine="ltspice").validate()
    with pytest.raises(ValueError, match="ALLOW_MOCK_ENGINE"):
        Settings(data_dir=tmp_path, engine="mock").validate()
    with pytest.raises(ValueError, match="ENGINE must"):
        Settings(data_dir=tmp_path, engine="spice3").validate()
    monkeypatch.setenv("ENGINE", "ngspice")
    monkeypatch.setenv("MAX_FILES", "7")
    monkeypatch.setenv("ALLOWED_BUILTIN_LIBS", "A.lib, b.lib")
    s = Settings.from_env()
    assert s.engine == "ngspice" and s.max_files == 7 and s.builtin_libs == frozenset({"a.lib", "b.lib"})
    monkeypatch.setenv("MAX_FILES", "seven")
    with pytest.raises(ValueError, match="MAX_FILES"):
        Settings.from_env()

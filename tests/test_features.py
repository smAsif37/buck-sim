"""Tests for engine selection, tolerance limits, topology, BOM, AI help and reports."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from live import LiveServer

from buck_sim import ai
from buck_sim.bom import BomError, cross_check, parse_bom, parse_value
from buck_sim.engines import EngineRegistry
from buck_sim.netlist import NetlistError, NetlistLoader
from buck_sim.registry import METRICS, TEST_NAMES
from buck_sim.spec import JobSpec
from buck_sim.topology import describe

EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "buck_sim" / "examples"


# --------------------------------------------------------------------------- tolerance
def test_tolerance_derives_valid_limits(spec_dict):
    d = dict(spec_dict, limits={}, tolerance={
        "vout_pct": 2, "transient_pct": 5, "ripple_pct": 1, "efficiency_min_pct": 85, "settle_max_us": 200})
    spec = JobSpec.model_validate(d)
    lim = spec.effective_limits()
    assert set(lim) <= set(METRICS)
    assert lim["steady_state.vout_error_pct"].min == -2 and lim["steady_state.vout_error_pct"].max == 2
    assert lim["line_regulation.vout_min_v"].min == pytest.approx(4.9)
    assert lim["steady_state.ripple_pp_v"].max == pytest.approx(0.05)
    assert lim["load_regulation.efficiency_min_pct"].min == 85
    assert lim["load_transient.up_settle_us"].max == 200


def test_tolerance_only_selected_tests_and_explicit_limit_wins(spec_dict):
    d = dict(spec_dict, tests=["steady_state"], tolerance={"vout_pct": 3},
             limits={"steady_state.vout_error_pct": {"min": -1, "max": 1}})
    lim = JobSpec.model_validate(d).effective_limits()
    assert all(k.startswith("steady_state.") for k in lim)
    assert lim["steady_state.vout_error_pct"].max == 1  # explicit beats derived
    with pytest.raises(ValueError):
        JobSpec.model_validate(dict(spec_dict, tolerance={"vout_pct": -1}))


# --------------------------------------------------------------------------- engines
def test_engine_registry(settings, tmp_path):
    s = replace(settings, engine="ngspice", allow_mock=False, ltspice_cmd="", ngspice_cmd="definitely-not-a-cmd-xyz")
    reg = EngineRegistry(s)
    assert reg.default == "ngspice" and "ngspice" in reg.names()  # the default is always offered
    assert "ltspice" not in reg.names() and "mock" not in reg.names()
    with pytest.raises(ValueError):
        reg.get("mock")
    reg2 = EngineRegistry(replace(s, ltspice_cmd='"C:/x/LTspice.exe"', allow_mock=True))
    assert reg2.names()[0] == "ngspice" and {"ltspice", "mock"} <= set(reg2.names())
    assert reg2.get("ltspice") is reg2.get("ltspice")  # cached


def test_spec_engine_field(spec_dict):
    assert JobSpec.model_validate(dict(spec_dict, engine="ltspice")).engine == "ltspice"
    with pytest.raises(ValueError):
        JobSpec.model_validate(dict(spec_dict, engine="spectre"))


# --------------------------------------------------------------------------- topology
def _parsed(main, files):
    return NetlistLoader({f: (EXAMPLES / f).read_bytes() for f in files}).load_main(main).statements


def test_topology_circuit_mode():
    d = describe(_parsed("buck_demo.cir", ["buck_demo.cir", "toyctrl.lib"]))
    assert d["roles"] == {"vin": "in", "sw": "sw", "vout": "out", "fb": "fb", "gnd": "0"}
    assert d["guess"] == {"vin_source": "V1", "vout_node": "out", "load_element": "Rload", "inductor": "L1"}
    ic = d["ics"][0]
    assert ic["subckt"] == "TOYCTRL" and ic["defined"]
    assert {p["pin"]: p["role"] for p in ic["pins"]}["fb"] == "fb"
    assert {m["model"]: m["source"] for m in d["models"]["vendor"]} == {"SWH": "defined", "DSCH": "defined", "TOYCTRL": "defined"}
    assert d["models"]["passives_builtin"] == 12


def test_topology_subckt_mode():
    d = describe(_parsed("buck_demo_subckt.cir", ["buck_demo_subckt.cir", "toyctrl.lib"]))
    assert d["scope"].upper() == "BUCK"
    assert d["roles"]["vin"] == "VIN" and d["roles"]["vout"] == "VOUT" and d["roles"]["sw"] == "sw"


# --------------------------------------------------------------------------- BOM
@pytest.mark.parametrize("text,kind,value", [
    ("10uF", "C", 10e-6), ("4k7", "R", 4700), ("2R2", "R", 2.2), ("1Meg", "R", 1e6), ("1M", "R", 1e6),
    ("20m", "R", 0.02), ("4.7µH", "L", 4.7e-6), ("2.7nF", "C", 2.7e-9), ("100", "R", 100), ("n/a", "R", None),
])
def test_parse_value(text, kind, value):
    v = parse_value(text, kind)
    assert v == (pytest.approx(value) if value is not None else None)


def test_bom_parse_and_cross_check():
    items = parse_bom((EXAMPLES / "buck_demo_bom.csv").read_bytes())
    refs = [i["ref"] for i in items]
    assert "Rtop" in refs and "Rbot" in refs and len(items) == 11
    elements = describe(_parsed("buck_demo.cir", ["buck_demo.cir", "toyctrl.lib"]))["elements"]
    c = cross_check(items, elements)
    assert c["mismatched"] == [] and c["matched"] == 11 and "Rload" in c["only_in_netlist"]
    bad = parse_bom(b"Ref;Value;Tol\nL1;22uH;20%\nC1-C3;1u;10%\n")
    assert [i["ref"] for i in bad] == ["L1", "C1", "C2", "C3"]
    c2 = cross_check(bad, elements)
    assert c2["mismatched"][0]["ref"] == "L1" and set(c2["only_in_bom"]) == {"C1", "C2", "C3"}
    with pytest.raises(BomError):
        parse_bom(b"just some text\nwithout a header\n")


# --------------------------------------------------------------------------- API
@pytest.fixture()
def srv(settings):
    s = LiveServer(settings)
    yield s
    s.stop()


def _files(example_files, with_bom=True):
    f = [("netlist", ("buck_demo.cir", example_files["buck_demo.cir"])),
         ("models", ("toyctrl.lib", example_files["toyctrl.lib"]))]
    if with_bom:
        f.append(("bom", ("buck_demo_bom.csv", (EXAMPLES / "buck_demo_bom.csv").read_bytes())))
    return f


def test_config_lists_engines(srv):
    cfg = requests.get(f"{srv.url}/api/config").json()
    names = [e["name"] for e in cfg["engines"]]
    assert names[0] == "mock" and cfg["engines"][0]["default"] is True
    assert cfg["ai_enabled"] in (True, False) and ".csv" in cfg["bom_extensions"]


def test_inspect_returns_topology_and_bom(srv, example_files):
    r = requests.post(f"{srv.url}/api/inspect", files=_files(example_files))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["topology"]["guess"]["load_element"] == "Rload"
    assert body["bom"]["matched"] == 11 and body["bom"]["mismatched"] == []
    bad = _files(example_files, with_bom=False) + [("bom", ("bom.xlsx", b"PK\x03\x04"))]
    r = requests.post(f"{srv.url}/api/inspect", files=bad)
    assert r.status_code == 422 and "CSV" in r.json()["error"]
    r = requests.post(f"{srv.url}/api/inspect", files=_files(example_files), data={"engine": "nonexistent"})
    assert r.status_code == 422


def test_job_with_bom_tolerance_and_reports(srv, example_files, spec_dict):
    spec = dict(spec_dict, name="Report test", notes="hello report", limits={},
                tolerance={"vout_pct": 2, "ripple_pct": 1, "efficiency_min_pct": 50})
    r = requests.post(f"{srv.url}/api/jobs", files=_files(example_files), data={"spec": json.dumps(spec)})
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    assert srv.worker.run_once()
    job = requests.get(f"{srv.url}/api/jobs/{jid}").json()
    assert job["status"] == "succeeded"
    keys = {c["key"] for t in job["results"]["tests"] for c in t["checks"]}
    assert "steady_state.vout_error_pct" in keys and "load_regulation.efficiency_min_pct" in keys
    assert job["results"]["limits_applied"]["steady_state.ripple_pp_v"]["max"] == pytest.approx(0.05)

    pdf = requests.get(f"{srv.url}/api/jobs/{jid}/report.pdf")
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-"
    assert pdf.headers["content-type"] == "application/pdf"
    assert 'filename="Report_test-' in pdf.headers["content-disposition"]
    docx = requests.get(f"{srv.url}/api/jobs/{jid}/report.docx")
    assert docx.status_code == 200 and docx.content[:2] == b"PK"

    import io

    import docx as python_docx

    d = python_docx.Document(io.BytesIO(docx.content))
    text = "\n".join(p.text for p in d.paragraphs)
    assert "Bill of materials" in text and "hello report" in text and "SYNTHETIC" in text
    assert len(d.inline_shapes) >= 8  # charts
    assert requests.get(f"{srv.url}/api/jobs/{jid}/report.exe").status_code == 404


def test_job_rejects_unavailable_engine(srv, example_files, spec_dict):
    spec = dict(spec_dict, engine="ltspice")
    r = requests.post(f"{srv.url}/api/jobs", files=_files(example_files, False), data={"spec": json.dumps(spec)})
    if "ltspice" in [e["name"] for e in requests.get(f"{srv.url}/api/config").json()["engines"]]:
        pytest.skip("LTspice is installed here")
    assert r.status_code == 422 and "not available" in r.json()["error"]


# --------------------------------------------------------------------------- AI
class FakeMessages:
    def __init__(self, payload, stop="end_turn"):
        self.payload, self.stop, self.calls = payload, stop, []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            stop_reason=self.stop, model=kw["model"],
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))],
        )


AI_ANSWER = {
    "summary": "A 12 V to 5 V buck.", "topology": "async buck", "issues": ["ideal switch"],
    "controller": {"element": "X1", "model": "TOYCTRL", "description": "PWM controller"},
    "pins": [{"pin": "fb", "net": "fb", "function": "feedback"}],
    "suggested_mapping": {"vin_source": "V1", "vout_node": "out", "load_element": "Rload", "gnd_node": "0"},
    "design_estimates": {"vin_nom_v": 12, "vout_nom_v": 5, "iout_max_a": 2, "fsw_khz": 500},
}


def test_ai_disabled_without_key(srv, example_files, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    r = requests.post(f"{srv.url}/api/ai/explain", files=_files(example_files))
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["error"]


def test_ai_explain_with_fake_client(settings, example_files):
    fake = FakeMessages(AI_ANSWER)
    s = LiveServer(settings, ai_client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    try:
        assert requests.get(f"{s.url}/api/config").json()["ai_enabled"] is True
        r = requests.post(f"{s.url}/api/ai/explain", files=_files(example_files))
        assert r.status_code == 200, r.text
        assert r.json()["suggested_mapping"]["load_element"] == "Rload"
        call = fake.calls[0]
        assert call["model"] == ai.DEFAULT_MODEL
        assert call["output_config"]["format"]["type"] == "json_schema"
        assert "Rload out 0 2.5" in call["messages"][0]["content"] and "<bom>" in call["messages"][0]["content"]
    finally:
        s.stop()


def test_ai_refusal_is_reported():
    fake = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages(AI_ANSWER, stop="refusal")))
    with pytest.raises(ai.AiUnavailable, match="declined"):
        ai.explain("V1 in 0 1", {}, None, client=fake)


def test_all_tests_known():
    assert set(TEST_NAMES) == {"steady_state", "load_transient", "line_transient", "line_regulation", "load_regulation"}


# --------------------------------------------------------------------------- model files
USER_NET = b"""* buck with vendor parts, no .include line (as LTspice exports it)
V1 in 0 12
XU1 in sw fb 0 MYCTRL
M1 in gate sw BSC010 m=1
Q1 c b e s QMOD
L1 sw out 10u
C1 out 0 22u
Rload out 0 5
.end
"""
VENDOR_LIB = b"""* vendor
.subckt MYCTRL vin sw fb gnd
R1 vin sw 1
.ends MYCTRL
.model BSC010 VDMOS(Rds=1m)
.model QMOD NPN
"""


def test_uploaded_lib_without_include_is_added():
    p = NetlistLoader({"buck.cir": USER_NET, "vendor.lib": VENDOR_LIB,
                       "other.lib": b"* x\n.tran 1u\n.model OTHER D\n"}).load_main("buck.cir")
    srcs = {m["element"]: (m["model"], m["source"]) for m in describe(p.statements)["models"]["vendor"]}
    assert srcs == {"XU1": ("MYCTRL", "defined"), "M1": ("BSC010", "defined"), "Q1": ("QMOD", "defined")}
    assert any("vendor.lib" in w and "added automatically" in w for w in p.warnings)
    assert not any("OTHER" in s for s in p.statements)  # unrelated file left out
    assert not any("other.lib" in w for w in p.warnings)  # and leaves no notes behind


def test_encrypted_lib_without_include_reports_encryption():
    enc = b"**$ENCRYPTED_LIB\n.SUBCKT MYCTRL A B\n$CDNENCSTART_ADV2\n0123abcd\n$CDNENCFINISH_ADV2\n.ENDS\n"
    unrelated = b"* x\n.SUBCKT NOTUSED A B\n$CDNENCSTART_ADV2\nff\n$CDNENCFINISH_ADV2\n.ENDS\n"
    with pytest.raises(NetlistError, match="encrypted"):
        NetlistLoader({"buck.cir": USER_NET, "vendor.lib": VENDOR_LIB.replace(b"MYCTRL", b"XXX"),
                       "mps.lib": enc}).load_main("buck.cir")
    # an encrypted file that defines nothing the circuit uses is still just skipped
    NetlistLoader({"buck.cir": USER_NET, "vendor.lib": VENDOR_LIB, "u.lib": unrelated}).load_main("buck.cir")


def test_missing_model_names_the_checked_files(srv):
    files = [("netlist", ("buck.cir", USER_NET)), ("models", ("wrong.lib", b"* nothing here\n.model X D\n"))]
    r = requests.post(f"{srv.url}/api/inspect", files=files)
    assert r.status_code == 200, r.text
    w = " ".join(r.json()["warnings"])
    assert "'MYCTRL' is not defined in any uploaded file (checked: wrong.lib)" in w


@pytest.mark.ngspice
@pytest.mark.skipif(__import__("shutil").which("ngspice") is None, reason="ngspice is not installed")
def test_ngspice_runs_demo_without_include_line(tmp_path, settings, spec_dict):
    from buck_sim.engines.ngspice import NgspiceEngine
    from buck_sim.runner import RunContext, run_job

    net = (EXAMPLES / "buck_demo.cir").read_text().replace('.include "toyctrl.lib"', "")
    files = {"buck_demo.cir": net.encode(), "toyctrl.lib": (EXAMPLES / "toyctrl.lib").read_bytes()}
    spec = JobSpec.model_validate(dict(spec_dict, tests=["steady_state"], limits={}))
    res = run_job(tmp_path / "job", spec, files, "buck_demo.cir", RunContext(NgspiceEngine(), replace(settings, engine="ngspice")))
    t = res["tests"][0]
    assert t["status"] == "ok", t["error"]
    assert t["metrics"]["vout_mean_v"] == pytest.approx(5.0, abs=0.15)
    assert any("added automatically" in w for w in res["warnings"])

"""End-to-end tests against a real HTTP server (uvicorn in a thread) and the mock engine."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
import requests
from live import LiveServer


@pytest.fixture()
def srv(settings):
    s = LiveServer(settings)
    yield s
    s.stop()


def files_payload(example_files, spec_dict, **spec_patch):
    spec = json.loads(json.dumps(spec_dict))
    spec.update(spec_patch)
    files = [("netlist", ("buck_demo.cir", example_files["buck_demo.cir"])),
             ("models", ("toyctrl.lib", example_files["toyctrl.lib"]))]
    return files, {"spec": json.dumps(spec)}


def submit(srv, example_files, spec_dict, headers=None, **patch):
    files, data = files_payload(example_files, spec_dict, **patch)
    return requests.post(f"{srv.url}/api/jobs", files=files, data=data, headers=headers or {}, timeout=30)


def test_health_config_and_security_headers(srv):
    assert requests.get(f"{srv.url}/healthz").text == "ok"
    assert requests.get(f"{srv.url}/readyz").status_code == 200
    r = requests.get(f"{srv.url}/api/config")
    cfg = r.json()
    assert cfg["engine"]["synthetic"] is True and cfg["auth_required"] is False
    assert {t["name"] for t in cfg["tests"]} >= {"steady_state", "load_regulation"}
    assert "steady_state.ripple_pp_v" in cfg["metrics"]
    h = r.headers
    assert h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in h["Content-Security-Policy"] and h["Cache-Control"] == "no-store"


def test_static_ui_is_served(srv):
    r = requests.get(srv.url + "/")
    assert r.status_code == 200 and "Buck Converter Simulator" in r.text
    assert requests.get(srv.url + "/app.js").status_code == 200
    assert requests.get(srv.url + "/style.css").status_code == 200
    assert requests.get(srv.url + "/nope.txt").status_code == 404


def test_examples_endpoint(srv):
    r = requests.get(f"{srv.url}/api/examples/buck_demo.cir")
    assert r.status_code == 200 and "V1 in 0 12" in r.text
    assert requests.get(f"{srv.url}/api/examples/buck_demo.spec.json").json()["mode"] == "circuit"
    assert requests.get(f"{srv.url}/api/examples/..%2Fconfig.py").status_code == 404
    assert requests.get(f"{srv.url}/api/examples/secret.txt").status_code == 404


def test_inspect(srv, example_files):
    files = [("netlist", ("buck_demo.cir", example_files["buck_demo.cir"])),
             ("models", ("toyctrl.lib", example_files["toyctrl.lib"]))]
    r = requests.post(f"{srv.url}/api/inspect", files=files)
    assert r.status_code == 200
    body = r.json()
    assert body["voltage_sources"][0]["name"] == "V1" and body["subckts"][0]["name"] == "TOYCTRL"
    # missing model -> clear 422
    r = requests.post(f"{srv.url}/api/inspect", files=files[:1])
    assert r.status_code == 422 and "toyctrl.lib" in r.json()["error"]


def test_full_job_lifecycle(srv, example_files, spec_dict):
    r = submit(srv, example_files, spec_dict)
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    assert requests.get(f"{srv.url}/api/jobs/{jid}").json()["status"] == "queued"

    assert srv.worker.run_once() is True
    job = requests.get(f"{srv.url}/api/jobs/{jid}").json()
    assert job["status"] == "succeeded" and job["results"]["summary"]["verdict"] in ("pass", "fail")
    assert job["results"]["engine"]["synthetic"] is True
    assert [t["test"] for t in job["results"]["tests"]][0] == "steady_state"

    w = requests.get(f"{srv.url}/api/jobs/{jid}/waveforms/steady_state")
    assert w.status_code == 200 and w.json()["synthetic"] is True
    assert requests.get(f"{srv.url}/api/jobs/{jid}/waveforms/line_regulation").status_code == 404
    assert requests.get(f"{srv.url}/api/jobs/{jid}/waveforms/../../etc").status_code == 404

    csv_text = requests.get(f"{srv.url}/api/jobs/{jid}/report.csv").text
    assert csv_text.splitlines()[0].startswith("test,metric,label,value")
    assert "steady_state,vout_mean_v" in csv_text
    assert "simulating" in requests.get(f"{srv.url}/api/jobs/{jid}/log").text
    tb = requests.get(f"{srv.url}/api/jobs/{jid}/testbench/steady_state/steady_state")
    assert tb.status_code == 200 and "Bbs_load" in tb.text
    assert requests.get(f"{srv.url}/api/jobs/{jid}/testbench/steady_state/..%2F..").status_code == 404

    listing = requests.get(f"{srv.url}/api/jobs").json()["jobs"]
    assert listing[0]["id"] == jid and listing[0]["summary"]["tests_total"] == 5
    assert requests.post(f"{srv.url}/api/jobs/{jid}/cancel").status_code == 409
    assert requests.delete(f"{srv.url}/api/jobs/{jid}").status_code == 204
    assert requests.get(f"{srv.url}/api/jobs/{jid}").status_code == 404


def test_cancel_queued_job_and_cannot_delete_queued(srv, example_files, spec_dict):
    jid = submit(srv, example_files, spec_dict).json()["id"]
    assert requests.delete(f"{srv.url}/api/jobs/{jid}").status_code == 409
    r = requests.post(f"{srv.url}/api/jobs/{jid}/cancel")
    assert r.status_code == 200 and r.json()["state"] == "canceled"
    assert srv.worker.run_once() is False
    assert requests.get(f"{srv.url}/api/jobs/{jid}").json()["status"] == "canceled"


def test_validation_errors(srv, example_files, spec_dict):
    files, data = files_payload(example_files, spec_dict)
    # no spec
    r = requests.post(f"{srv.url}/api/jobs", files=files)
    assert r.status_code == 422 and "spec" in r.json()["error"]
    # invalid spec -> field level details
    bad = json.loads(data["spec"])
    bad["design"]["vin_min"] = 20
    r = requests.post(f"{srv.url}/api/jobs", files=files, data={"spec": json.dumps(bad)})
    assert r.status_code == 422 and r.json()["details"]
    r = requests.post(f"{srv.url}/api/jobs", files=files, data={"spec": "{not json"})
    assert r.status_code == 422
    # unknown element mapping surfaces at submit time, not after queueing
    bad = json.loads(data["spec"])
    bad["circuit"]["load_element"] = "Rnone"
    r = requests.post(f"{srv.url}/api/jobs", files=files, data={"spec": json.dumps(bad)})
    assert r.status_code == 422 and "Rnone" in r.json()["error"]
    assert requests.get(f"{srv.url}/api/jobs").json()["jobs"] == []


def test_upload_restrictions(srv, example_files, spec_dict):
    spec = {"spec": json.dumps(spec_dict)}
    r = requests.post(f"{srv.url}/api/jobs", files=[("netlist", ("x.asc", b"Version 4"))], data=spec)
    assert r.status_code == 422 and "SPICE Netlist" in r.json()["error"]
    r = requests.post(f"{srv.url}/api/jobs", files=[("netlist", ("x.exe", b"MZ"))], data=spec)
    assert r.status_code == 422 and "netlist must be one of" in r.json()["error"]
    files = [("netlist", ("buck_demo.cir", example_files["buck_demo.cir"])), ("models", ("evil.sh", b"rm -rf /"))]
    r = requests.post(f"{srv.url}/api/jobs", files=files, data=spec)
    assert r.status_code == 422 and "extension" in r.json()["error"]
    files = [("netlist", ("buck_demo.cir", b"x")), ("models", ("BUCK_DEMO.CIR", b"y"))]
    r = requests.post(f"{srv.url}/api/jobs", files=files, data=spec)
    assert r.status_code == 422 and "duplicate" in r.json()["error"]
    big = [("netlist", ("buck_demo.cir", b"*" * (srv.app.state.settings.max_netlist_bytes + 1)))]
    r = requests.post(f"{srv.url}/api/jobs", files=big, data=spec)
    assert r.status_code == 413
    r = requests.post(f"{srv.url}/api/jobs", data=spec)  # not multipart
    assert r.status_code in (400, 422)


def test_path_traversal_in_filenames_is_neutralised(srv, example_files, spec_dict):
    files = [("netlist", ("../../evil/buck_demo.cir", example_files["buck_demo.cir"])),
             ("models", ("..\\..\\toyctrl.lib", example_files["toyctrl.lib"]))]
    r = requests.post(f"{srv.url}/api/jobs", files=files, data={"spec": json.dumps(spec_dict)})
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    jd = srv.app.state.store.job_dir(jid) / "input"
    assert sorted(p.name for p in jd.iterdir()) == ["buck_demo.cir", "toyctrl.lib"]


def test_invalid_job_ids_are_404(srv):
    for bad in ("nope", "A" * 32, "%2e%2e", "0" * 32):
        assert requests.get(f"{srv.url}/api/jobs/{bad}").status_code == 404
        assert requests.get(f"{srv.url}/api/jobs/{bad}/log").status_code == 404


def test_api_key_auth(settings, example_files, spec_dict):
    srv = LiveServer(replace(settings, api_key="s3cret-key"))
    try:
        assert requests.get(f"{srv.url}/healthz").status_code == 200
        assert requests.get(f"{srv.url}/").status_code == 200  # UI shell is public; API is not
        assert requests.get(f"{srv.url}/api/config").status_code == 401
        assert requests.get(f"{srv.url}/api/config", headers={"X-API-Key": "wrong"}).status_code == 401
        assert requests.get(f"{srv.url}/api/config", headers={"X-API-Key": "s3cret-key"}).status_code == 200
        assert requests.get(f"{srv.url}/api/config", headers={"Authorization": "Bearer s3cret-key"}).status_code == 200
        r = submit(srv, example_files, spec_dict)
        assert r.status_code == 401
        assert submit(srv, example_files, spec_dict, headers={"X-API-Key": "s3cret-key"}).status_code == 202
        assert "X-Content-Type-Options" in requests.get(f"{srv.url}/api/config").headers  # also on 401s
    finally:
        srv.stop()


def test_rate_limit_and_queue_limit(settings, example_files, spec_dict):
    srv = LiveServer(replace(settings, rate_limit_per_min=2, queue_max=2))
    try:
        codes = [submit(srv, example_files, spec_dict).status_code for _ in range(3)]
        assert codes == [202, 202, 429]
    finally:
        srv.stop()
    srv = LiveServer(replace(settings, queue_max=1, data_dir=settings.data_dir.parent / "data2"))
    try:
        assert submit(srv, example_files, spec_dict).status_code == 202
        r = submit(srv, example_files, spec_dict)
        assert r.status_code == 503 and "queue is full" in r.json()["error"]
    finally:
        srv.stop()


def test_subckt_mode_job(srv, subckt_files, subckt_spec):
    files = [("netlist", ("buck_demo_subckt.cir", subckt_files["buck_demo_subckt.cir"])),
             ("models", ("toyctrl.lib", subckt_files["toyctrl.lib"]))]
    r = requests.post(f"{srv.url}/api/jobs", files=files, data={"spec": subckt_spec.model_dump_json()})
    assert r.status_code == 202, r.text
    srv.worker.run_once()
    job = requests.get(f"{srv.url}/api/jobs/{r.json()['id']}").json()
    assert job["status"] == "succeeded"
    assert [t["status"] for t in job["results"]["tests"]] == ["ok", "ok"]

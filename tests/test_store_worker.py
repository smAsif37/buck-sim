import json
import threading
import time

import pytest

from buck_sim.engines.base import Engine, EngineError
from buck_sim.store import CANCELED, FAILED, QUEUED, RUNNING, SUCCEEDED, JobStore, new_job_id, valid_job_id
from buck_sim.worker import Worker


def make_job(store: JobStore, spec_json: str, files: dict[str, bytes], netlist: str, name="job") -> str:
    jid = new_job_id()
    store.write_inputs(jid, files)
    store.insert(jid, name=name, spec_json=spec_json,
                 files={"netlist": netlist, "models": [n for n in files if n != netlist], "all": list(files)}, engine="mock")
    return jid


@pytest.fixture()
def store(tmp_path):
    return JobStore(tmp_path / "data")


@pytest.fixture()
def ready_job(store, example_files, spec):
    return make_job(store, spec.model_dump_json(), example_files, "buck_demo.cir")


def test_job_id_validation(store):
    assert valid_job_id(new_job_id())
    for bad in ("", "../etc", "A" * 32, "g" * 32, "0" * 31):
        assert not valid_job_id(bad)
        assert store.get(bad) is None
    with pytest.raises(ValueError):
        store.job_dir("../x")


def test_lifecycle(store, ready_job):
    job = store.get(ready_job)
    assert job["status"] == QUEUED and job["attempts"] == 0 and store.count_queued() == 1
    claimed = store.claim_next("w1")
    assert claimed["id"] == ready_job and claimed["status"] == RUNNING and claimed["attempts"] == 1
    assert store.claim_next("w2") is None
    store.heartbeat(ready_job, "w1", "step 1")
    assert store.get(ready_job)["progress"] == "step 1"
    store.heartbeat(ready_job, "other-worker", "hijack")  # ignored
    assert store.get(ready_job)["progress"] == "step 1"
    store.finish(ready_job, SUCCEEDED, summary={"verdict": "pass"})
    done = store.get(ready_job)
    assert done["status"] == SUCCEEDED and done["summary"] == {"verdict": "pass"} and done["finished_at"]
    store.finish(ready_job, FAILED, error="late")  # no effect once finished
    assert store.get(ready_job)["status"] == SUCCEEDED


def test_concurrent_claims_never_double_assign(store, example_files, spec):
    ids = [make_job(store, spec.model_dump_json(), example_files, "buck_demo.cir") for _ in range(12)]
    got: list[str] = []
    lock = threading.Lock()

    def work(wid):
        while True:
            j = store.claim_next(wid)
            if j is None:
                return
            with lock:
                got.append(j["id"])

    threads = [threading.Thread(target=work, args=(f"w{i}",)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(got) == sorted(ids) and len(set(got)) == len(ids)


def test_claim_order_is_fifo(store, example_files, spec):
    a = make_job(store, spec.model_dump_json(), example_files, "buck_demo.cir")
    time.sleep(0.01)
    b = make_job(store, spec.model_dump_json(), example_files, "buck_demo.cir")
    assert store.claim_next("w")["id"] == a and store.claim_next("w")["id"] == b


def test_cancel_paths(store, ready_job):
    assert store.cancel(ready_job) == CANCELED
    assert store.get(ready_job)["status"] == CANCELED
    assert store.claim_next("w") is None
    assert store.cancel(ready_job) is None and store.cancel("0" * 32) is None


def test_cancel_running_sets_flag(store, ready_job):
    store.claim_next("w")
    assert store.cancel(ready_job) == "canceling"
    assert store.is_cancel_requested(ready_job)
    assert store.get(ready_job)["status"] == RUNNING


def test_stale_recovery_and_giving_up(store, ready_job):
    store.claim_next("w1")
    assert store.recover_stale(3600, 2) == 0  # still fresh
    time.sleep(0.02)
    assert store.recover_stale(0.01, 2) == 1
    assert store.get(ready_job)["status"] == QUEUED
    store.claim_next("w2")  # attempt 2
    time.sleep(0.02)
    assert store.recover_stale(0.01, 2) == 1
    j = store.get(ready_job)
    assert j["status"] == FAILED and "stopped responding" in j["error"]


def test_requeue_restores_attempt(store, ready_job):
    store.claim_next("w1")
    store.requeue(ready_job, "someone-else")  # wrong worker: no effect
    assert store.get(ready_job)["status"] == RUNNING
    store.requeue(ready_job, "w1")
    j = store.get(ready_job)
    assert j["status"] == QUEUED and j["attempts"] == 0


def test_delete_and_cleanup(store, ready_job):
    assert store.delete(ready_job) is False  # queued
    store.claim_next("w")
    assert store.delete(ready_job) is False  # running
    store.finish(ready_job, SUCCEEDED, summary={})
    assert store.job_dir(ready_job).exists()
    assert store.cleanup_old(30) == 0  # too recent
    with store._conn() as c:
        c.execute("UPDATE jobs SET finished_at=? WHERE id=?", (time.time() - 40 * 86400, ready_job))
    assert store.cleanup_old(30) == 1
    assert store.get(ready_job) is None and not store.job_dir(ready_job).exists()


def test_list_ordering_and_limits(store, example_files, spec):
    ids = []
    for i in range(5):
        ids.append(make_job(store, spec.model_dump_json(), example_files, "buck_demo.cir", name=f"j{i}"))
        time.sleep(0.005)
    names = [j["name"] for j in store.list(limit=3)]
    assert names == ["j4", "j3", "j2"]
    assert [j["name"] for j in store.list(limit=3, offset=3)] == ["j1", "j0"]
    assert len(store.list(limit=10_000)) == 5


# ----------------------------------------------------------------------- worker
def test_worker_runs_job_and_prunes_raw(store, settings, mock_engine, ready_job):
    jd = store.job_dir(ready_job)
    stale = jd / "work" / "steady_state" / "steady_state"
    stale.mkdir(parents=True)
    (stale / "tb.raw").write_bytes(b"x" * 10)
    w = Worker(settings, store, mock_engine, "wk")
    assert w.run_once() is True and w.run_once() is False
    job = store.get(ready_job)
    assert job["status"] == SUCCEEDED and job["summary"]["tests_error"] == 0
    assert (jd / "results.json").exists() and (jd / "job.log").read_text().count("simulating") >= 5
    assert not list(jd.rglob("*.raw"))  # bulky raw files are pruned ...
    assert (stale / "tb.cir").exists()  # ... netlists are kept for debugging


def test_worker_marks_input_errors_failed(store, settings, mock_engine, example_files, spec_dict):
    bad = json.loads(json.dumps(spec_dict))
    bad["circuit"]["vin_source"] = "V99"
    from buck_sim.spec import JobSpec

    jid = make_job(store, JobSpec.model_validate(bad).model_dump_json(), example_files, "buck_demo.cir")
    Worker(settings, store, mock_engine, "wk").run_once()
    j = store.get(jid)
    assert j["status"] == FAILED and "V99" in j["error"]


def test_worker_survives_engine_crash(store, settings, ready_job):
    class Boom(Engine):
        name = "boom"

        def simulate(self, bundle, workdir, timeout_s):
            raise RuntimeError("bug in engine")

    Worker(settings, store, Boom(), "wk").run_once()
    j = store.get(ready_job)
    # EngineError is per-test; a non-EngineError bug fails the whole job without leaking details
    assert j["status"] == FAILED and j["error"] == "internal error: RuntimeError"


def test_worker_honours_user_cancel_and_shutdown(store, settings, mock_engine, ready_job, example_files, spec):
    # user cancel while running
    w = Worker(settings, store, mock_engine, "wk")
    original = store.is_cancel_requested
    store.is_cancel_requested = lambda jid: True  # type: ignore[method-assign]
    w.run_once()
    store.is_cancel_requested = original  # type: ignore[method-assign]
    assert store.get(ready_job)["status"] == CANCELED
    # graceful shutdown re-queues
    jid = make_job(store, spec.model_dump_json(), example_files, "buck_demo.cir")
    w2 = Worker(settings, store, mock_engine, "wk2")
    w2.stop()
    w2.run_once()
    j = store.get(jid)
    assert j["status"] == QUEUED and j["attempts"] == 0


def test_engine_error_type_exported():
    assert issubclass(EngineError, RuntimeError)

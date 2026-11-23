"""Background worker: claims queued jobs and runs them."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import traceback
from pathlib import Path

from .config import Settings
from .engines import EngineRegistry
from .engines.base import Engine
from .runner import JobCancelled, JobInputError, RunContext, run_job
from .spec import JobSpec
from .store import CANCELED, FAILED, SUCCEEDED, JobStore

log = logging.getLogger(__name__)

HEARTBEAT_EVERY_S = 15.0
RECOVER_EVERY_S = 30.0


def prune_work_dir(job_dir: Path) -> None:
    """Delete bulky raw files, keeping netlists and logs for debugging."""
    work = job_dir / "work"
    if not work.exists():
        return
    for raw in work.rglob("*.raw"):
        try:
            raw.unlink()
        except OSError:
            pass


class Worker:
    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        engine: Engine | EngineRegistry,
        worker_id: str | None = None,
    ):
        self.settings = settings
        self.store = store
        # A single Engine (tests) or a registry so each job can use the engine it asked for.
        self.engines = engine if isinstance(engine, EngineRegistry) else EngineRegistry(settings, engine)
        self.engine = self.engines.get()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.stop_event = threading.Event()
        self._last_recover = 0.0

    # ----------------------------------------------------------------- control
    def stop(self) -> None:
        self.stop_event.set()

    def run_forever(self) -> None:
        log.info("worker %s started (engines=%s, default=%s)", self.worker_id, ",".join(self.engines.names()), self.engines.default)
        while not self.stop_event.is_set():
            try:
                did = self.run_once()
            except Exception:  # keep the worker alive whatever happens
                log.exception("unexpected worker error")
                did = False
            if not did:
                self.stop_event.wait(self.settings.poll_interval_s)
        log.info("worker %s stopped", self.worker_id)

    # --------------------------------------------------------------------- one
    def run_once(self) -> bool:
        now = time.monotonic()
        if now - self._last_recover > RECOVER_EVERY_S:
            self._last_recover = now
            n = self.store.recover_stale(self.settings.stale_after_s, self.settings.max_attempts)
            if n:
                log.warning("recovered %d stale job(s)", n)
        job = self.store.claim_next(self.worker_id)
        if job is None:
            return False
        self._execute(job)
        return True

    def _execute(self, job: dict) -> None:
        job_id = job["id"]
        job_dir = self.store.job_dir(job_id)
        log.info("job %s started", job_id)
        hb_stop = threading.Event()

        def beat() -> None:
            while not hb_stop.wait(HEARTBEAT_EVERY_S):
                try:
                    self.store.heartbeat(job_id, self.worker_id)
                except Exception:
                    log.exception("heartbeat failed")

        hb = threading.Thread(target=beat, daemon=True, name=f"hb-{job_id[:8]}")
        hb.start()
        job_log = job_dir / "job.log"

        def progress(msg: str) -> None:
            self.store.heartbeat(job_id, self.worker_id, msg)
            with job_log.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")

        def cancelled() -> bool:
            return self.stop_event.is_set() or self.store.is_cancel_requested(job_id)

        try:
            spec = JobSpec.model_validate(job["spec"])
            try:
                engine = self.engines.get(job.get("engine") or spec.engine)
            except ValueError as e:
                raise JobInputError(f"{e}. Choose another engine.") from e
            files = self.store.read_inputs(job_id, job["files"]["all"])
            ctx = RunContext(engine, self.settings, progress, cancelled)
            results = run_job(job_dir, spec, files, job["files"]["netlist"], ctx)
            self.store.finish(job_id, SUCCEEDED, summary=results["summary"])
            log.info("job %s finished: %s", job_id, results["summary"]["verdict"])
        except JobCancelled:
            if self.stop_event.is_set() and not self.store.is_cancel_requested(job_id):
                self.store.requeue(job_id, self.worker_id)
                log.info("job %s re-queued (worker shutting down)", job_id)
            else:
                self.store.finish(job_id, CANCELED, error="cancelled by user")
                log.info("job %s cancelled", job_id)
        except JobInputError as e:
            self.store.finish(job_id, FAILED, error=str(e))
            log.info("job %s failed (input): %s", job_id, e)
        except Exception as e:
            log.error("job %s crashed:\n%s", job_id, traceback.format_exc())
            self.store.finish(job_id, FAILED, error=f"internal error: {type(e).__name__}")
        finally:
            hb_stop.set()
            prune_work_dir(job_dir)

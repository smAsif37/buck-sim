"""HTTP API (Starlette) and static web UI."""

from __future__ import annotations

import csv
import hmac
import io
import json
import logging
import re
import time
from collections import defaultdict, deque
from pathlib import Path

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Receive, Scope, Send

from . import __version__
from .config import Settings
from .engines import create_engine
from .engines.base import Engine
from .netlist import NetlistError, NetlistLoader, inspect as inspect_netlist
from .registry import METRICS, TEST_LABELS, TEST_NAMES
from .runner import JobInputError, prepare
from .spec import JobSpec
from .store import FINISHED, JobStore, new_job_id, valid_job_id

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
NETLIST_EXTS = {".cir", ".net", ".sp", ".spi", ".ckt", ".txt"}
MODEL_EXTS = {".lib", ".sub", ".mod", ".model", ".mdl", ".inc", ".cir", ".sp", ".spi", ".ckt", ".txt"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-]")
_BUNDLE_RE = re.compile(r"^[a-z_]+(_\d{2})?$")

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    def __init__(self, status: int, message: str, details: list | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or []


def safe_filename(name: str) -> str:
    base = re.split(r"[\\/]", name or "")[-1]
    base = _SAFE_NAME_RE.sub("_", base).strip("._")[:100]
    return base or "file"


def json_response(data: object, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})


class RateLimiter:
    """Tiny in-memory sliding-window limiter (per API process)."""

    def __init__(self, per_minute: int) -> None:
        self.limit = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        if self.limit <= 0:
            return True
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        if len(self._hits) > 5000:  # bound memory
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
                self._hits.pop(k, None)
        return True


class SecurityMiddleware:
    """API-key auth for /api/* and security headers on every response."""

    def __init__(self, app: ASGIApp, api_key: str) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope.get("path", "")
        if self.api_key and path.startswith("/api/") and not self._authorised(scope):
            resp = JSONResponse({"error": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
            await self._send_with_headers(resp, scope, receive, send)
            return

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}
                for k, v in (
                    (b"content-security-policy", CSP.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                ):
                    if k not in existing:
                        headers.append((k, v))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _authorised(self, scope: Scope) -> bool:
        supplied = ""
        for k, v in scope.get("headers", []):
            if k == b"x-api-key":
                supplied = v.decode("latin-1")
                break
            if k == b"authorization" and v[:7].lower() == b"bearer ":
                supplied = v[7:].decode("latin-1").strip()
                break
        return hmac.compare_digest(supplied.encode(), self.api_key.encode())

    @staticmethod
    async def _send_with_headers(resp: Response, scope: Scope, receive: Receive, send: Send) -> None:
        resp.headers["X-Content-Type-Options"] = "nosniff"
        await resp(scope, receive, send)


# --------------------------------------------------------------------------- #
# Upload parsing
# --------------------------------------------------------------------------- #
async def _read_upload(f: UploadFile, limit: int, label: str) -> bytes:
    data = await f.read(limit + 1)
    if len(data) > limit:
        raise ApiError(413, f"{label} is larger than {limit // 1024} KB")
    return data


async def parse_upload(request: Request, *, need_spec: bool) -> tuple[dict[str, bytes], str, str | None]:
    """Return (files, netlist_name, spec_json). Files are keyed by sanitised name."""
    s: Settings = request.app.state.settings
    cl = request.headers.get("content-length")
    if cl is None:
        raise ApiError(411, "Content-Length header is required")
    try:
        if int(cl) > s.max_total_upload_bytes + 1024 * 1024:
            raise ApiError(413, f"upload is larger than {s.max_total_upload_bytes // (1024 * 1024)} MB")
    except ValueError as e:
        raise ApiError(400, "invalid Content-Length") from e

    try:
        form = await request.form(max_files=s.max_files + 2, max_fields=20)
    except HTTPException as e:
        raise ApiError(400, f"could not read the upload: {e.detail}") from e
    except Exception as e:
        raise ApiError(400, "could not read the upload (is it multipart/form-data?)") from e

    try:
        netlist_up = form.get("netlist")
        if not isinstance(netlist_up, UploadFile) or not netlist_up.filename:
            raise ApiError(422, "missing file field 'netlist'")
        model_ups = form.getlist("models")
        if len(model_ups) + 1 > s.max_files:
            raise ApiError(422, f"too many files (max {s.max_files})")

        files: dict[str, bytes] = {}
        seen: set[str] = set()
        total = 0

        netlist_name = safe_filename(netlist_up.filename)
        ext = Path(netlist_name).suffix.lower()
        if ext == ".asc":
            raise ApiError(
                422,
                "LTspice schematics (.asc) cannot be uploaded directly. In LTspice use "
                "View > SPICE Netlist (or Tools > Export Netlist), save the .cir/.net file and upload that.",
            )
        if ext not in NETLIST_EXTS:
            raise ApiError(422, f"netlist must be one of {sorted(NETLIST_EXTS)}, got {ext or 'no extension'}")
        data = await _read_upload(netlist_up, s.max_netlist_bytes, "netlist")
        files[netlist_name] = data
        seen.add(netlist_name.lower())
        total += len(data)

        for up in model_ups:
            if not isinstance(up, UploadFile) or not up.filename:
                raise ApiError(422, "'models' must be files")
            name = safe_filename(up.filename)
            mext = Path(name).suffix.lower()
            if mext not in MODEL_EXTS:
                raise ApiError(422, f"model file {name!r}: extension must be one of {sorted(MODEL_EXTS)}")
            if name.lower() in seen:
                raise ApiError(422, f"duplicate file name {name!r}")
            data = await _read_upload(up, s.max_model_bytes, f"model {name!r}")
            seen.add(name.lower())
            files[name] = data
            total += len(data)
        if total > s.max_total_upload_bytes:
            raise ApiError(413, "total upload is too large")

        spec_json: str | None = None
        if need_spec:
            raw = form.get("spec")
            if raw is None or isinstance(raw, UploadFile):
                raise ApiError(422, "missing text field 'spec' (JSON)")
            if len(raw) > 100_000:
                raise ApiError(413, "spec is too large")
            spec_json = raw
        return files, netlist_name, spec_json
    finally:
        await form.close()


def _validation_details(e: ValidationError) -> list[dict]:
    out = []
    for err in e.errors():
        out.append({"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]})
    return out


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
async def healthz(_: Request) -> Response:
    return PlainTextResponse("ok", headers={"Cache-Control": "no-store"})


async def readyz(request: Request) -> Response:
    try:
        await run_in_threadpool(request.app.state.store.check)
    except Exception:
        log.exception("readiness check failed")
        return PlainTextResponse("database not ready", status_code=503)
    return PlainTextResponse("ready", headers={"Cache-Control": "no-store"})


async def get_config(request: Request) -> Response:
    app = request.app
    s: Settings = app.state.settings
    engine: Engine = app.state.engine
    if "engine_version" not in app.state.cache:
        app.state.cache["engine_version"] = await run_in_threadpool(engine.version)
    return json_response(
        {
            "app_version": __version__,
            "engine": {
                "name": engine.name,
                "version": app.state.cache["engine_version"],
                "synthetic": engine.synthetic,
            },
            "auth_required": bool(s.api_key),
            "tests": [{"name": t, "label": TEST_LABELS[t]} for t in TEST_NAMES],
            "metrics": {k: {"unit": u, "label": lbl} for k, (u, lbl) in METRICS.items()},
            "limits": {
                "max_netlist_kb": s.max_netlist_bytes // 1024,
                "max_model_mb": s.max_model_bytes // (1024 * 1024),
                "max_files": s.max_files,
            },
            "netlist_extensions": sorted(NETLIST_EXTS),
            "model_extensions": sorted(MODEL_EXTS),
        }
    )


EXAMPLE_FILES = {
    "buck_demo.cir": "text/plain",
    "buck_demo_subckt.cir": "text/plain",
    "toyctrl.lib": "text/plain",
    "buck_demo.spec.json": "application/json",
    "buck_demo_subckt.spec.json": "application/json",
}


async def get_example(request: Request) -> Response:
    name = request.path_params["name"]
    if name not in EXAMPLE_FILES:
        raise ApiError(404, "unknown example file")
    return FileResponse(
        Path(__file__).parent / "examples" / name,
        media_type=EXAMPLE_FILES[name],
        headers={"Cache-Control": "no-store"},
    )


async def post_inspect(request: Request) -> Response:
    s: Settings = request.app.state.settings
    engine: Engine = request.app.state.engine
    files, netlist_name, _ = await parse_upload(request, need_spec=False)
    libs = s.builtin_libs if engine.supports_builtin_libs else frozenset()

    def work() -> dict:
        parsed = NetlistLoader(files, builtin_libs=libs).load_main(netlist_name)
        info = inspect_netlist(parsed.statements)
        info["warnings"] = parsed.warnings
        info["statements"] = len(parsed.statements)
        return info

    try:
        info = await run_in_threadpool(work)
    except NetlistError as e:
        raise ApiError(422, str(e)) from e
    return json_response(info)


async def post_job(request: Request) -> Response:
    app = request.app
    s: Settings = app.state.settings
    store: JobStore = app.state.store
    engine: Engine = app.state.engine
    client = request.client.host if request.client else "unknown"
    if not app.state.limiter.allow(client):
        raise ApiError(429, "too many requests; please wait a minute")
    if await run_in_threadpool(store.count_queued) >= s.queue_max:
        raise ApiError(503, "the job queue is full; try again later")

    files, netlist_name, spec_json = await parse_upload(request, need_spec=True)
    assert spec_json is not None
    try:
        spec = JobSpec.model_validate_json(spec_json)
    except ValidationError as e:
        raise ApiError(422, "invalid job specification", _validation_details(e)) from e

    try:
        _, warnings = await run_in_threadpool(prepare, files, netlist_name, spec, engine, s)
    except JobInputError as e:
        raise ApiError(422, str(e)) from e

    job_id = new_job_id()
    models = [n for n in files if n != netlist_name]
    manifest = {"netlist": netlist_name, "models": models, "all": [netlist_name, *models]}

    def persist() -> None:
        try:
            store.write_inputs(job_id, files)
            store.insert(
                job_id,
                name=spec.name or netlist_name,
                spec_json=spec.model_dump_json(),
                files=manifest,
                engine=engine.name,
            )
        except Exception:
            store.discard_files(job_id)
            raise

    await run_in_threadpool(persist)
    log.info("job %s queued (%s)", job_id, spec.name or netlist_name)
    return json_response({"id": job_id, "status": "queued", "warnings": warnings}, 202)


def _job_public(job: dict) -> dict:
    return {
        "id": job["id"],
        "name": job["name"],
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "progress": job["progress"],
        "error": job["error"],
        "engine": job["engine"],
        "summary": job["summary"],
        "cancel_requested": job["cancel_requested"],
    }


async def list_jobs(request: Request) -> Response:
    store: JobStore = request.app.state.store
    try:
        limit = int(request.query_params.get("limit", "30"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError as e:
        raise ApiError(400, "limit/offset must be integers") from e
    jobs = await run_in_threadpool(store.list, limit, offset)
    return json_response({"jobs": [_job_public(j) for j in jobs]})


async def _require_job(request: Request) -> dict:
    job_id = request.path_params["job_id"]
    if not valid_job_id(job_id):
        raise ApiError(404, "job not found")
    job = await run_in_threadpool(request.app.state.store.get, job_id)
    if job is None:
        raise ApiError(404, "job not found")
    return job


async def get_job(request: Request) -> Response:
    job = await _require_job(request)
    out = _job_public(job)
    out["spec"] = job["spec"]
    out["results"] = None
    if job["status"] in FINISHED:
        rpath = request.app.state.store.job_dir(job["id"]) / "results.json"
        if rpath.exists():
            out["results"] = json.loads(await run_in_threadpool(rpath.read_text, "utf-8"))
    return json_response(out)


async def get_waveform(request: Request) -> Response:
    job = await _require_job(request)
    test = request.path_params["test"]
    if test not in TEST_NAMES:
        raise ApiError(404, "unknown test")
    path = request.app.state.store.job_dir(job["id"]) / "waveforms" / f"{test}.json"
    if not path.exists():
        raise ApiError(404, "no waveform data for this test")
    return FileResponse(path, media_type="application/json", headers={"Cache-Control": "no-store"})


async def get_log(request: Request) -> Response:
    job = await _require_job(request)
    path = request.app.state.store.job_dir(job["id"]) / "job.log"
    text = ""
    if path.exists():
        text = (await run_in_threadpool(path.read_text, "utf-8", "replace"))[-200_000:]
    return PlainTextResponse(text, headers={"Cache-Control": "no-store"})


async def get_testbench(request: Request) -> Response:
    job = await _require_job(request)
    test, bundle = request.path_params["test"], request.path_params["bundle"]
    if test not in TEST_NAMES or not _BUNDLE_RE.match(bundle):
        raise ApiError(404, "not found")
    path = request.app.state.store.job_dir(job["id"]) / "work" / test / bundle / "tb.cir"
    if not path.exists():
        raise ApiError(404, "testbench not available")
    return FileResponse(
        path,
        media_type="text/plain",
        filename=f"{bundle}.cir",
        headers={"Cache-Control": "no-store"},
    )


async def get_report_csv(request: Request) -> Response:
    job = await _require_job(request)
    rpath = request.app.state.store.job_dir(job["id"]) / "results.json"
    if job["status"] not in FINISHED or not rpath.exists():
        raise ApiError(404, "results not available")
    results = json.loads(await run_in_threadpool(rpath.read_text, "utf-8"))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["test", "metric", "label", "value", "unit", "limit_min", "limit_max", "passed"])
    for t in results["tests"]:
        checks = {c["key"]: c for c in t["checks"]}
        for m, v in t["metrics"].items():
            key = f"{t['test']}.{m}"
            info = results["metric_info"].get(key, {"unit": "", "label": m})
            c = checks.get(key)
            w.writerow(
                [t["test"], m, info["label"], "" if v is None else v, info["unit"],
                 "" if not c or c["min"] is None else c["min"],
                 "" if not c or c["max"] is None else c["max"],
                 "" if not c else ("pass" if c["passed"] else "fail")]
            )  # fmt: skip
    text = buf.getvalue()
    return Response(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="buck-sim-{job["id"][:8]}.csv"'},
    )


async def cancel_job(request: Request) -> Response:
    job = await _require_job(request)
    state = await run_in_threadpool(request.app.state.store.cancel, job["id"])
    if state is None:
        raise ApiError(409, "job already finished")
    return json_response({"id": job["id"], "state": state})


async def delete_job(request: Request) -> Response:
    job = await _require_job(request)
    ok = await run_in_threadpool(request.app.state.store.delete, job["id"])
    if not ok:
        raise ApiError(409, "a queued or running job cannot be deleted; cancel it first")
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
async def api_error_handler(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, ApiError)
    body: dict = {"error": exc.message}
    if exc.details:
        body["details"] = exc.details
    return json_response(body, exc.status)


async def unhandled_error_handler(request: Request, exc: Exception) -> Response:
    log.error("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return json_response({"error": "internal server error"}, 500)


async def not_found_api(_: Request, exc: Exception) -> Response:
    assert isinstance(exc, HTTPException)
    return json_response({"error": exc.detail}, exc.status_code)


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    settings.validate()
    engine = engine or create_engine(settings)
    store = JobStore(settings.data_dir)

    routes = [
        Route("/healthz", healthz),
        Route("/readyz", readyz),
        Route("/api/config", get_config),
        Route("/api/examples/{name}", get_example),
        Route("/api/inspect", post_inspect, methods=["POST"]),
        Route("/api/jobs", post_job, methods=["POST"]),
        Route("/api/jobs", list_jobs, methods=["GET"]),
        Route("/api/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/api/jobs/{job_id}", delete_job, methods=["DELETE"]),
        Route("/api/jobs/{job_id}/cancel", cancel_job, methods=["POST"]),
        Route("/api/jobs/{job_id}/waveforms/{test}", get_waveform),
        Route("/api/jobs/{job_id}/log", get_log),
        Route("/api/jobs/{job_id}/testbench/{test}/{bundle}", get_testbench),
        Route("/api/jobs/{job_id}/report.csv", get_report_csv),
        Mount("/", app=StaticFiles(directory=str(STATIC_DIR), html=True), name="static"),
    ]
    app = Starlette(
        routes=routes,
        exception_handlers={
            ApiError: api_error_handler,
            HTTPException: not_found_api,
            Exception: unhandled_error_handler,
        },
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.store = store
    app.state.limiter = RateLimiter(settings.rate_limit_per_min)
    app.state.cache = {}
    app.add_middleware(SecurityMiddleware, api_key=settings.api_key)
    return app

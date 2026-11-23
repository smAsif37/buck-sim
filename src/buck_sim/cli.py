"""Command line entry point: ``buck-sim serve|worker|run|selftest|cleanup``."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import tempfile
import time
from pathlib import Path

from . import __version__
from .config import Settings
from .engines import create_engine
from .runner import JobInputError, RunContext, run_job
from .spec import JobSpec
from .store import JobStore

EXAMPLES = Path(__file__).parent / "examples"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data)


def setup_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stderr)
    if settings.log_format == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))


# --------------------------------------------------------------------------- #
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = Settings.from_env()
    setup_logging(settings)
    uvicorn.run(
        "buck_sim.api:create_app",
        factory=True,
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_config=None,
        proxy_headers=False,
    )
    return 0


def cmd_worker(_: argparse.Namespace) -> int:
    from .worker import Worker

    settings = Settings.from_env()
    setup_logging(settings)
    store = JobStore(settings.data_dir)
    worker = Worker(settings, store, create_engine(settings))

    def handle(_sig: int, _frame: object) -> None:
        logging.getLogger(__name__).info("shutdown requested")
        worker.stop()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    worker.run_forever()
    return 0


def _print_results(results: dict) -> None:
    print(f"engine : {results['engine']['version']}" + ("  [SYNTHETIC]" if results["engine"]["synthetic"] else ""))
    for w in results["warnings"]:
        print(f"warning: {w}")
    for t in results["tests"]:
        print(f"\n== {t['label']}: {t['status']}  ({t['duration_s']} s)")
        if t["error"]:
            print(f"   ERROR: {t['error']}")
            if t["log_tail"]:
                print("   --- simulator log (tail) ---")
                print("\n".join("   " + ln for ln in t["log_tail"].splitlines()))
        for k, v in t["metrics"].items():
            info = results["metric_info"].get(f"{t['test']}.{k}", {"unit": "", "label": k})
            print(f"   {info['label']:<52} {'-' if v is None else v:>14} {info['unit']}")
        for c in t["checks"]:
            print(f"   [{'PASS' if c['passed'] else 'FAIL'}] {c['label']}: {c['value']} (min={c['min']}, max={c['max']})")
    s = results["summary"]
    print(f"\nverdict: {s['verdict']}  (checks failed {s['checks_failed']}/{s['checks_total']}, tests with errors {s['tests_error']})")


def cmd_run(args: argparse.Namespace) -> int:
    settings = Settings.from_env(validate=False)
    if args.engine:
        settings = settings.with_engine(args.engine)
    settings.validate()
    setup_logging(settings)
    engine = create_engine(settings)
    spec = JobSpec.model_validate(json.loads(Path(args.spec).read_text(encoding="utf-8")))
    netlist = Path(args.netlist)
    files = {netlist.name: netlist.read_bytes()}
    for m in args.model or []:
        files[Path(m).name] = Path(m).read_bytes()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        results = run_job(out, spec, files, netlist.name, RunContext(engine, settings, progress=lambda m: print(f"... {m}", file=sys.stderr)))
    except JobInputError as e:
        print(f"input error: {e}", file=sys.stderr)
        return 2
    _print_results(results)
    print(f"\nresults written to {out / 'results.json'}")
    return 0 if results["summary"]["verdict"] in ("pass", "n/a") else 1


def _sanity(results: dict) -> list[str]:
    """Loose physical sanity checks for the bundled 12 V -> 5 V demo."""
    problems: list[str] = []
    by = {t["test"]: t for t in results["tests"]}
    ss = by.get("steady_state")
    if not ss or ss["status"] != "ok":
        return ["steady_state did not complete"]
    m = ss["metrics"]
    if m["vout_mean_v"] is None or abs(m["vout_mean_v"] - 5.0) > 0.5:
        problems.append(f"Vout mean {m['vout_mean_v']} V is not within 0.5 V of 5 V")
    if m["efficiency_pct"] is None or not (50 <= m["efficiency_pct"] <= 100):
        problems.append(f"efficiency {m['efficiency_pct']} % is outside 50..100 %")
    if m["ripple_pp_v"] is None or m["ripple_pp_v"] > 1.0:
        problems.append(f"ripple {m['ripple_pp_v']} V is implausibly large")
    lt = by.get("load_transient")
    if lt and lt["status"] != "ok":
        problems.append("load_transient did not complete")
    return problems


def cmd_selftest(args: argparse.Namespace) -> int:
    settings = Settings.from_env(validate=False)
    if args.engine:
        settings = settings.with_engine(args.engine)
    try:
        settings.validate()
    except ValueError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    setup_logging(settings)
    engine = create_engine(settings)
    spec = JobSpec.model_validate(json.loads((EXAMPLES / "buck_demo.spec.json").read_text(encoding="utf-8")))
    spec = spec.model_copy(update={"tests": ["steady_state", "load_transient"], "limits": {}})
    files = {n: (EXAMPLES / n).read_bytes() for n in ("buck_demo.cir", "toyctrl.lib")}
    with tempfile.TemporaryDirectory(prefix="buck-sim-selftest-") as tmp:
        print(f"Running self-test with engine '{engine.name}' ...", file=sys.stderr)
        try:
            results = run_job(Path(tmp), spec, files, "buck_demo.cir", RunContext(engine, settings))
        except JobInputError as e:
            print(f"input error: {e}", file=sys.stderr)
            return 2
        _print_results(results)
    problems = _sanity(results)
    if problems:
        print("\nSELF-TEST FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    if engine.synthetic:
        print("\nSELF-TEST PASSED for the software pipeline only: the mock engine is not a SPICE simulator.")
    else:
        print("\nSELF-TEST PASSED (loose sanity checks on the bundled demo converter)")
    return 0


def cmd_cleanup(_: argparse.Namespace) -> int:
    settings = Settings.from_env()
    setup_logging(settings)
    n = JobStore(settings.data_dir).cleanup_old(settings.retention_days)
    print(f"deleted {n} job(s) older than {settings.retention_days} days")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="buck-sim", description="Buck-converter SPICE test-suite service")
    p.add_argument("--version", action="version", version=f"buck-sim {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the web API + UI")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("worker", help="run a simulation worker")
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("run", help="run one job from files, without the web service")
    s.add_argument("--netlist", required=True)
    s.add_argument("--model", action="append", help="model file (repeatable)")
    s.add_argument("--spec", required=True, help="job spec JSON")
    s.add_argument("--out", required=True, help="output directory")
    s.add_argument("--engine", choices=["ltspice", "ngspice", "mock"])
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("selftest", help="simulate the bundled demo buck and sanity-check the result")
    s.add_argument("--engine", choices=["ltspice", "ngspice", "mock"])
    s.set_defaults(fn=cmd_selftest)

    s = sub.add_parser("cleanup", help="delete finished jobs older than RETENTION_DAYS")
    s.set_defaults(fn=cmd_cleanup)

    args = p.parse_args(argv)
    try:
        return int(args.fn(args))
    except ValueError as e:  # configuration errors
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

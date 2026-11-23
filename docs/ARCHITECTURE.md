# Architecture

```
Browser (static HTML/JS/SVG)
   │  HTTP (JSON, multipart upload)
   ▼
API process  ── Starlette/uvicorn ──  security middleware (API key, headers, rate limit)
   │  validates upload + JobSpec (pydantic), parses netlist, stores files, inserts job
   ▼
DATA_DIR
   ├── jobs.db            SQLite (WAL): job table, status, heartbeat, results pointer
   └── jobs/<id>/{input,work,waveforms,results.json,job.log}
   ▲
   │  atomic claim (BEGIN IMMEDIATE)
Worker process(es) ── run_job()
   │   netlist.py   sanitise + inline includes
   │   testbench.py generate one bench per test point (PWL Vin, programmable load)
   │   engines/     LTspice | ngspice | mock  → .raw → waveforms
   │   metrics.py / analysis.py  ripple, mean, efficiency, step response, regulation
   │   runner.py    per-test isolation, limits → verdict
   ▼
results.json + decimated waveforms  → served by the API → drawn in the UI
```

## Components

| Module | Responsibility |
|---|---|
| `spec.py` | Validated input model (`JobSpec`, `Design`, mapping, limits) and timing defaults |
| `netlist.py` | Parse, allow-list directives, inline includes from uploads only, inspect circuit |
| `testbench.py` | Build the simulation deck for each test; engine-neutral |
| `engines/` | Run the simulator as a subprocess (timeout, process-group kill), read `.raw` via spicelib |
| `metrics.py`, `analysis.py` | Numerics on non-uniform time grids |
| `runner.py` | Orchestrates tests (thread-pooled sweeps), time-point budget, cancellation |
| `store.py` | Job queue and persistence |
| `worker.py` | Claim loop, heartbeat, requeue on SIGTERM, recovery of stale jobs, `.raw` pruning |
| `api.py` | HTTP interface, validation, security headers |
| `static/` | UI with no third-party scripts (strict CSP) |

## Key decisions

- **Engine-neutral test bench.** Vin and the load are driven by PWL sources and a behavioural current source,
  so the same deck runs on LTspice and ngspice. Vin ramps from 0 for a realistic start-up.
- **Measure on the cycle average.** Step metrics (deviation, settling) are computed on a centred switching-cycle
  average so ripple does not trigger false settling.
- **Queue in SQLite.** One file, no extra service, atomic claims, crash recovery. Limit: one host.
- **Files are the source of truth.** Uploads are immutable; results and logs are plain files that are easy to back up.
- **Fail loudly.** A test that cannot be measured is reported as an error with a reason, never as a guessed number.
- **Mock engine is labelled.** Synthetic results carry a flag that the UI shows as a banner.

## Security model

- Optional API key (constant-time compare); strict CSP, `nosniff`, `X-Frame-Options: DENY`.
- Uploads: extension allow-list, size and count limits, sanitised names, required `Content-Length`.
- Netlists: directive allow-list; includes resolved only against uploaded files; no network or shell directives.
- Simulator runs in a subprocess with a timeout; in Docker, the worker is non-root, read-only root filesystem,
  capabilities dropped, memory/PID limited.
- Job ids are validated against a strict pattern before touching the filesystem.

The simulator still parses untrusted input. Treat it as a risk: run the worker in the provided container, on an
isolated network, and do not expose it without the API key and TLS.

## Scaling path

1. Several workers on one host: works now (shared SQLite and `DATA_DIR`).
2. Many hosts: replace `store.py` with Postgres (same interface) and `DATA_DIR` with object storage.
3. Heavy users: add per-user auth (OIDC) and quotas in front of `POST /api/jobs`.

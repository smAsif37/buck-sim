# Setup guide: step by step

Follow the steps in order. Each step says what to run and what you should see.
Steps 1–4 need no simulator. Steps 5–6 add a real simulator (ngspice, then LTspice).

> **Honest status.** The software pipeline (parsing, test-bench generation, metrics, job queue, API) is covered by
> automated tests that use a *mock* simulator. A real ngspice or LTspice run has **not** been verified by the
> author. Steps 5 and 6 are where you verify it. Do them before trusting any result.

---

## Step 0 – What you need

| Item | Needed for | Notes |
|---|---|---|
| Python 3.11+ | everything | `python3 --version` |
| Git + a GitHub account | hosting | |
| ngspice 38+ | real simulation, free | `apt install ngspice` / `brew install ngspice` |
| LTspice | optional second engine | free download from Analog Devices; **check its license for server/commercial use** |
| Docker + Compose | optional | for the containerised setup |
| Your buck netlist (+ IC models) | real use | see `docs/NETLIST_GUIDE.md` |

---

## Step 1 – Put the project on GitHub

```bash
cd buck-sim
git init -b main
git add .
git commit -m "Initial commit"
# create an empty repo on github.com (no README), then:
git remote add origin https://github.com/<you>/buck-sim.git
git push -u origin main
```

Expected: the **CI** workflow starts under the repo's *Actions* tab.
The `unit` jobs should go green. The `ngspice` and `docker` jobs are the first real-world check; if they fail,
read the log and see *Troubleshooting* below.

Do **not** commit proprietary IC models or the LTspice installer (`.gitignore` already blocks `*.msi`, `*.exe`
and `models-private/`). Keep vendor models in a private location (see Step 8).

## Step 2 – Local install and unit tests

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest -m "not ngspice and not ltspice"
```

Expected: all tests pass (115 on Linux/macOS; 114 plus 1 skipped on Windows). Tests marked `ngspice`/`ltspice` are skipped here.

## Step 3 – Demo with the mock engine (no simulator)

```bash
export ENGINE=mock ALLOW_MOCK_ENGINE=1 DATA_DIR=./data
buck-sim serve            # terminal 1: web UI + API on http://localhost:8000
buck-sim worker           # terminal 2: executes queued jobs
```

Open <http://localhost:8000>, press **Load demo example**, then **Run**.
Results appear after a few seconds.

**These numbers are synthetic.** The mock engine only proves the plumbing works; the UI labels such results as
synthetic.

## Step 4 – Verify the self-test

```bash
buck-sim selftest --engine mock
```

Expected last line: `SELF-TEST PASSED for the software pipeline only`.

## Step 5 – Real simulation with ngspice (free, recommended first)

```bash
# Debian/Ubuntu
sudo apt-get install -y ngspice
# macOS
brew install ngspice
# Windows: download ngspice-<ver>_64.7z from https://sourceforge.net/projects/ngspice/files/ng-spice-rework/,
#   extract the Spice64 folder (e.g. to %LOCALAPPDATA%\Programs\Spice64) and add Spice64\bin to PATH.
#   The app uses the console build ngspice_con.exe automatically (ngspice.exe is a GUI build with no stdout),
#   or set NGSPICE_CMD to its full path.

ngspice --version
buck-sim selftest --engine ngspice
pytest -m ngspice -v
```

Expected: the self-test simulates the bundled demo buck and checks sanity (Vout within 0.5 V of 5 V, efficiency between
50 % and 100 %, ripple below 1 V). If it **fails**, send the full output; the likeliest cause is SPICE syntax
that your ngspice version rejects. The log tail is printed.

Then run the app against ngspice:

```bash
export ENGINE=ngspice
buck-sim serve &   buck-sim worker
```

## Step 6 – LTspice (optional)

1. Install LTspice from Analog Devices.
2. **Licensing check.** LTspice is free to download, but read its current license terms before running it on a
   shared server or inside a commercial product. If unsure, use ngspice, or run LTspice only on your own machine.
3. Find the command that starts it and set `LTSPICE_CMD`:

| Platform | `LTSPICE_CMD` |
|---|---|
| macOS | `/Applications/LTspice.app/Contents/MacOS/LTspice` |
| Windows | `C:\Program Files\ADI\LTspice\LTspice.exe` (quote it if it has spaces) |
| Linux | `xvfb-run -a wine /opt/ltspice/LTspice.exe` (needs Wine + Xvfb) |

4. If the default batch arguments (`-b`) do not suit your version, override them with `LTSPICE_ARGS`.
5. Verify:

```bash
export LTSPICE_CMD="<value from the table>"
buck-sim selftest --engine ltspice
pytest -m ltspice -v
```

If LTspice exits without writing a `.raw` file, open `data/jobs/<id>/job.log` (also downloadable in the UI) and
check the path/arguments. On Linux/Wine, the first run can take much longer while the Wine prefix initialises.

## Step 7 – Use your own buck converter

Read `docs/NETLIST_GUIDE.md` first. In short:

1. Export a **netlist** from LTspice (*View → SPICE Netlist*) or your tool. `.asc` schematics are not accepted.
2. Upload it plus any IC model files (`.lib`, `.sub`, `.cir`, `.mod`, `.sp`).
3. Press **Inspect**, then map: input source, output node, load element (circuit mode) or the pins (sub-circuit mode).
4. Enter the design values: Vin range, Vout, load range, switching frequency.
5. Choose tests, optionally set limits, and Run.
6. **Validate** one result against your own LTspice/ngspice run of the same circuit before relying on it
   (compare ripple, Vout, efficiency within a few percent).

Encrypted vendor models (PSpice `.olb`, encrypted `.lib`) cannot be used by ngspice. Use the vendor's unencrypted
LTspice model, or a behavioural model.

Automatic download of models from supplier websites is **not implemented** (terms of use and anti-bot measures
differ per vendor). Download models yourself and upload them.

## Step 8 – Where things are stored

| What | Where |
|---|---|
| Code | GitHub |
| Jobs, uploads, results, logs | `DATA_DIR/jobs/<job-id>/` and `DATA_DIR/jobs.db` (SQLite) |
| Simulators | installed on the host or in the Docker image, never in Git |
| Vendor / private IC models | a private location (private repo, S3-style bucket, internal share); upload per job |

Back up `DATA_DIR` if results matter. `buck-sim cleanup` deletes jobs older than `RETENTION_DAYS`; run it from cron.

## Step 9 – Docker

```bash
cp .env.example .env     # edit if needed
docker compose up --build -d
docker compose logs -f
```

Open <http://localhost:8000>. Two containers run: `api` and `worker`, sharing the `data` volume. This image uses
ngspice. The LTspice-under-Wine image (`docker/Dockerfile.ltspice`, override file `docker-compose.ltspice.yml`) is
**experimental**; you must supply the installer yourself and accept the license. Never publish that image.

## Step 10 – Production checklist

- [ ] Put the app behind a TLS-terminating reverse proxy (Caddy, nginx, a cloud load balancer).
- [ ] Set `API_KEY` to a long random value (`openssl rand -hex 32`). The UI prompts for it when the server requires it.
- [ ] Set resource limits (compose already sets memory/PID limits for the worker).
- [ ] Keep `ENGINE` at `ngspice` or `ltspice`; never leave `mock` on.
- [ ] Schedule `buck-sim cleanup` and back up `DATA_DIR`.
- [ ] Monitor `/healthz` (process alive) and `/readyz` (job database reachable).
- [ ] Tag a release (`git tag v0.1.0 && git push --tags`) to publish the ngspice image to GHCR.
- [ ] Scale: SQLite on one host is fine for a team. For many concurrent users, move to Postgres and a shared
      object store; that is outside this version.

## Known limitations

- No `.asc` schematic input; export a netlist.
- Single-host job store (SQLite). Multiple workers on the **same** host work; across hosts they do not.
- API key only; no per-user accounts or OIDC.
- Supplier model auto-download not implemented.
- Real engine behaviour and the Docker/CI definitions are unverified until you run Steps 5, 6 and 9.
- Only buck converters are modelled by the test benches.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| `ENGINE=mock produces SYNTHETIC data` on start | Add `ALLOW_MOCK_ENGINE=1` (demo only) or choose a real engine |
| `ENGINE=ltspice needs LTSPICE_CMD` | Set `LTSPICE_CMD` (Step 6) |
| Job fails: "no raw file" | See `job.log`; check engine path/arguments |
| "trace not found: V(out)" | The output node mapping is wrong; re-inspect and pick the right node |
| "unresolved include" | Upload the referenced model file with the exact name |
| "unsafe directive" | The netlist uses a directive outside the allow-list (see `docs/NETLIST_GUIDE.md`) or a file reference that cannot be resolved from your uploads; remove it |
| 413/422 on upload | File too large or wrong extension; see limits in `.env.example` |
| 401 from the API | `API_KEY` is set; send `X-API-Key` |
| 429 | Rate limit; raise `RATE_LIMIT_PER_MIN` or wait |
| Jobs stay `queued` | The worker is not running |
| Run is very slow | Reduce `measure_cycles`, `steps_per_cycle`, or the sweep points |
| Settling/ripple values look wrong | Not enough start-up/hold time; raise `startup_time_s` / `hold_time_s` |

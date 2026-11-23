# buck-sim

Web app that runs a standard test suite on a buck-converter SPICE netlist and shows the results in the browser.

- **Tests:** steady state (Vout, ripple, efficiency), load transient, line transient, line regulation, load regulation.
- **Engines:** ngspice (free, default) or LTspice; a labelled synthetic mock engine for demos.
- **Input:** SPICE netlist plus optional IC model files. Pass/fail limits per metric.
- **Output:** metric cards, waveforms with zoom, CSV report.

> **Start here: [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)** – the step-by-step guide.

## Quick start (no simulator needed)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -m "not ngspice and not ltspice"
export ENGINE=mock ALLOW_MOCK_ENGINE=1
buck-sim serve &      # http://localhost:8000
buck-sim worker
```

Mock results are synthetic. For real results install ngspice (or LTspice) as described in the guide and run
`buck-sim selftest --engine ngspice`.

## Docs

- [Setup guide](docs/SETUP_GUIDE.md) – GitHub, install, ngspice, LTspice, Docker, production checklist
- [Netlist guide](docs/NETLIST_GUIDE.md) – how to prepare a netlist, tests and metrics
- [Architecture](docs/ARCHITECTURE.md) – components, decisions, security

## Status

Verified by automated tests: parsing, test-bench generation, metrics, queue/worker, API (using the mock engine).
**Not yet verified:** real LTspice/ngspice runs, Docker builds, GitHub Actions, the UI in a browser.
Run the self-test and CI to check these on your side.

## Licensing

LTspice and vendor IC models are not included and are not redistributable from this repository; check their licenses.
Add your own license file for this code before publishing.

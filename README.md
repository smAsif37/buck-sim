# buck-sim

Web app that runs a standard test suite on a buck-converter SPICE netlist and shows the results in the browser.

- **Tests:** steady state (Vout, ripple, efficiency), load transient, line transient, line regulation, load regulation.
- **Engines:** ngspice (free, default) or LTspice, chosen per job in the UI; a labelled synthetic mock engine for demos.
- **Input:** SPICE netlist, optional IC model files (passives need none) and an optional BOM (CSV) for reference.
- **Analysis:** on upload the app detects input, switch node, output, feedback and IC pins, draws a power-stage
  overview and the IC pinout, checks which models are present and compares the BOM with the netlist.
  Optional "Explain with AI" (Claude) gives a written explanation and a suggested mapping.
- **Acceptance:** overall tolerance (Vout band, transient excursion, ripple, efficiency, settling) turned into
  pass/fail limits automatically, plus per-metric limits. Every input has a hover help text.
- **Output:** metric tables, waveforms with zoom, and downloadable **PDF**, **Word (.docx)** and CSV reports.

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

Verified by automated tests: parsing, test-bench generation, metrics, queue/worker, API, topology detection,
BOM parsing, tolerance limits, PDF/Word reports and the AI endpoint (with a stand-in client).
Verified on real simulators: ngspice 39/45/47 (Windows, Linux CI, Docker) and LTspice 24 on Windows; both agree on
the demo converter within a few percent. The AI explanation needs your own Anthropic API key and has not been
exercised against the live API in CI.
Run the self-test and CI to check these on your side.

## Licensing

LTspice and vendor IC models are not included and are not redistributable from this repository; check their licenses.
Add your own license file for this code before publishing.

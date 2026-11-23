# Preparing a netlist

## What the service accepts

- A SPICE **netlist** (`.cir`, `.net`, `.sp`, `.spi`, `.ckt`, `.txt`). Schematics (`.asc`) are rejected; in LTspice use
  *View → SPICE Netlist* and save the text.
- Optional model files: `.lib`, `.sub`, `.mod`, `.model`, `.mdl`, `.inc`, `.cir`, `.sp`, `.spi`, `.ckt`, `.txt`.
- Files referenced by `.include` / `.lib` must be uploaded **with the same file name** (matched case-insensitively,
  directories ignored). Nothing is ever read from the server's disk, except a short allow-list of LTspice built-in
  libraries when the LTspice engine is used.

## Two ways to describe your circuit

### A. Circuit mode (a complete buck with source and load)

Your netlist contains the input source, the converter and the load. You tell the app:

| Field | Meaning | Demo value |
|---|---|---|
| Input source | the voltage source that is Vin | `V1` |
| Output node | node that is Vout | `out` |
| Load element | the resistor (or current source) that is the load | `Rload` |
| Ground node | usually `0` | `0` |

The app removes the source and load, and inserts its own programmable Vin (a PWL source) and load.
The load model draws a current proportional to the output voltage, so a startup ramp does not collapse the output.

### B. Sub-circuit mode (just the converter)

Your netlist contains `.subckt BUCK VIN VOUT GND ...`. You give the sub-circuit name and which pin is Vin, Vout and
ground. Extra pins (enable, power-good, …) can be tied to a node or a voltage in `ties`.

## What is removed or rewritten

Analysis and output directives are dropped (the app generates its own `.tran`): `.tran .ac .dc .op .noise .tf .four
.step .meas .measure .save .probe .plot .print .width .end .title` and similar. A warning is shown for each.

Allowed as-is: `.subckt .ends .model .param .func .global .ic .nodeset .options .temp`.
`.include`, `.inc`, `.lib` are resolved against your uploads and inlined.
**Any other directive raises an "unsafe directive" error.**

Names beginning with `bs_` are reserved for the generated test bench.

## Models

- Use plain-text SPICE models. Encrypted models (`$CDNENCSTART`, PSpice `.olb`) are detected and rejected.
- LTspice-specific syntax works in the LTspice engine. ngspice accepts most of it but not all (for example some
  behavioural-source and `.param` expression forms). If an ngspice run fails to parse, try LTspice or simplify the
  expression.
- Controller ICs are best modelled with the vendor's own average or switching model. Check the vendor model's
  required `.param`/pin conventions.

## Design values to enter

| Field | Notes |
|---|---|
| Vin min / nom / max | must satisfy min ≤ nom ≤ max |
| Vout nominal | must be below Vin min (buck) |
| Iout max / min | min defaults to 10 % of max |
| Switching frequency | used to size the time step and the measurement window |
| Start-up time, hold time | leave blank for automatic: start-up ≥ 200 switching periods (≥ 1 ms) |
| Measure cycles | switching cycles averaged for ripple and mean (default 50) |
| Steps per cycle | maximum time step is period / steps (default 100) |
| Load / line slew | rate of the load or line step, A/µs and V/µs |
| Settle band | ± % around the final value counted as settled |

## The tests

| Test | What it applies | Main results |
|---|---|---|
| `steady_state` | Vin nominal, full load | Vout mean/error, ripple p-p and RMS, Iin, power, efficiency |
| `load_transient` | load steps min → max → min | peak deviation, min/max Vout, settling time, final Vout (up and down) |
| `line_transient` | Vin steps min → max → min | same set of metrics as load transient |
| `line_regulation` | Vin sweep at full load | Vout min/max, line regulation (% and mV/V), min efficiency, max ripple, per-point table |
| `load_regulation` | load sweep at nominal Vin | Vout min/max, load regulation (% and mV/A), peak/min efficiency, max ripple, per-point table |

Efficiency uses the input source current; it is only meaningful if the netlist has no other supply feeding the
converter.

## Limits (pass/fail)

Add `min` and/or `max` per metric (for example `steady_state.ripple_pp_v` max 0.05). The verdict is `pass`, `fail`,
`error` (a test could not be evaluated) or `n/a` (no limits set).

## Command-line use without the web service

```bash
buck-sim run --netlist buck.cir --model ic.lib --spec spec.json --out out/ --engine ngspice
```

See `src/buck_sim/examples/` for a ready netlist and spec.

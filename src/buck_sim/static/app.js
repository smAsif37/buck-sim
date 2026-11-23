"use strict";

/* ------------------------------------------------------------------ helpers */
const $ = (sel, root = document) => root.querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const SVGNS = "http://www.w3.org/2000/svg";
function sv(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    el.setAttribute(k, String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

function fmt(v, digits = 4) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  if (typeof v !== "number") return String(v);
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e5 || a < 1e-3) return v.toExponential(digits - 1);
  return String(Number(v.toPrecision(digits)));
}

function fmtTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString();
}

function cls(s) {
  return String(s).toLowerCase().replace(/[^a-z]/g, "");
}

/* --------------------------------------------------------------------- API */
let apiKey = sessionStorage.getItem("apiKey") || "";

class ApiError extends Error {
  constructor(status, body) {
    let msg = (body && body.error) || (typeof body === "string" ? body : "") || `HTTP ${status}`;
    if (body && Array.isArray(body.details) && body.details.length) {
      msg += "\n" + body.details.map((d) => `  • ${d.field}: ${d.message}`).join("\n");
    }
    super(msg);
    this.status = status;
  }
}

async function api(path, opts = {}) {
  const { as, ...init } = opts;
  const headers = Object.assign({}, init.headers || {});
  if (apiKey) headers["X-API-Key"] = apiKey;
  const resp = await fetch(path, Object.assign({}, init, { headers }));
  if (resp.status === 401) {
    const k = window.prompt("This server needs an API key:");
    if (!k) throw new ApiError(401, { error: "API key required" });
    apiKey = k.trim();
    sessionStorage.setItem("apiKey", apiKey);
    return api(path, opts);
  }
  if (!resp.ok) {
    let body = null;
    try {
      const ct = resp.headers.get("content-type") || "";
      body = ct.includes("json") ? await resp.json() : await resp.text();
    } catch (_e) { /* ignore */ }
    throw new ApiError(resp.status, body);
  }
  if (as === "blob") return resp.blob();
  if (as === "text") return resp.text();
  if (resp.status === 204) return null;
  return resp.json();
}

async function download(path, filename) {
  const blob = await api(path, { as: "blob" });
  const url = URL.createObjectURL(blob);
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

/* ------------------------------------------------------------------- state */
const state = {
  config: null,
  inspect: null, // last analysis result
  inspected: false,
  ai: null, // last AI explanation
  inputs: {}, // design inputs
  tol: {}, // tolerance inputs
  map: {}, // mapping inputs
  autoFilled: new Set(), // mapping fields filled from the analysis
  limits: [],
  testBoxes: {},
  jobId: null,
  poll: null,
};

const DESIGN_FIELDS = [
  { key: "vin_min", label: "Vin min", unit: "V", def: 9, tip: "Lowest input voltage the converter must handle. Used for the line sweep and the line-transient low level." },
  { key: "vin_nom", label: "Vin nominal", unit: "V", def: 12, tip: "Typical input voltage. Steady-state, load-transient and load-regulation tests run here." },
  { key: "vin_max", label: "Vin max", unit: "V", def: 15, tip: "Highest input voltage. Used for the line sweep and the line-transient high level." },
  { key: "vout_nom", label: "Vout nominal", unit: "V", def: 5, tip: "Target output voltage. Errors, deviations and tolerance bands are measured against it. Must be below Vin min." },
  { key: "iout_max", label: "Iout max", unit: "A", def: 2, tip: "Full-load current. Steady state runs at this load; load steps go up to it." },
  { key: "iout_min", label: "Iout min (blank = 10 % of max)", unit: "A", def: "", optional: true, tip: "Light-load current: the low level of the load step and the start of the load sweep." },
  { key: "fsw_hz", label: "Switching frequency", unit: "kHz", scale: 1e3, def: 500, tip: "Nominal switching frequency. Sets the simulation time step (100 points per cycle) and the averaging window." },
  { key: "startup_time_s", label: "Start-up time (blank = auto)", unit: "µs", scale: 1e-6, def: "", optional: true, tip: "Time allowed for soft-start before anything is measured. Auto = max(1 ms, 200 switching cycles). Increase it if Vout has not settled." },
  { key: "hold_time_s", label: "Hold per level (blank = auto)", unit: "µs", scale: 1e-6, def: "", optional: true, tip: "How long each load/line level is held in transient tests. Auto = max(0.5 ms, 4 measurement windows)." },
  { key: "measure_cycles", label: "Measurement window", unit: "cycles", def: 50, int: true, tip: "Number of switching cycles averaged for mean Vout, ripple, currents and efficiency." },
  { key: "load_slew_a_per_us", label: "Load slew rate", unit: "A/µs", def: 1, tip: "How fast the electronic load steps between Iout min and Iout max." },
  { key: "line_slew_v_per_us", label: "Line slew rate", unit: "V/µs", def: 1, tip: "How fast the input steps between Vin min and Vin max in the line transient." },
  { key: "settle_band_pct", label: "Settling band", unit: "%", def: 2, tip: "Vout counts as settled once it stays within ± this % of its final value." },
];

const TOL_FIELDS = [
  { key: "vout_pct", label: "Static Vout tolerance", unit: "± %", ph: "e.g. 2", tip: "Allowed DC error of Vout. Checks steady-state error, min/max Vout over the line and load sweeps, and the final value after each step." },
  { key: "transient_pct", label: "Transient excursion", unit: "± %", ph: "e.g. 5", tip: "Largest Vout overshoot/undershoot allowed during a load or line step, as % of nominal." },
  { key: "ripple_pct", label: "Max ripple (pk-pk)", unit: "% of Vout", ph: "e.g. 1", tip: "Peak-to-peak output ripple limit as % of nominal Vout, checked at steady state and across both sweeps." },
  { key: "efficiency_min_pct", label: "Minimum efficiency", unit: "%", ph: "e.g. 85", tip: "Lowest acceptable efficiency, checked at full load and across both sweeps." },
  { key: "settle_max_us", label: "Max settling time", unit: "µs", ph: "e.g. 200", tip: "Longest allowed recovery time after a load or line step (Vout back inside the settling band)." },
];

const TEST_TIPS = {
  steady_state: "Nominal Vin, full load: mean Vout, error, ripple, input/output power and efficiency.",
  load_transient: "Load steps Iout min → max → min at nominal Vin: peak deviation and settling time.",
  line_transient: "Input steps Vin min → max → min at full load: peak deviation and settling time.",
  line_regulation: "Sweeps Vin from min to max at full load (one run per point): Vout change, efficiency, ripple.",
  load_regulation: "Sweeps the load from Iout min to max at nominal Vin: Vout change, efficiency, ripple.",
};

const ROLE_TEXT = { vin: "input", sw: "switch node", vout: "output", fb: "feedback", gnd: "ground" };

/* -------------------------------------------------------------- small UI */
function tip(text) {
  return h("span", { class: "info", tabindex: "0", "data-tip": text, "aria-label": text });
}

function fieldLabel(text, tipText, extra) {
  return h("span", { class: "lbl" }, text, tipText ? tip(tipText) : null, extra || null);
}

/* --------------------------------------------------------------- navigation */
function showView(name) {
  $("#view-new").classList.toggle("hidden", name !== "new");
  $("#view-jobs").classList.toggle("hidden", name !== "jobs");
  $("#nav-new").classList.toggle("active", name === "new");
  $("#nav-jobs").classList.toggle("active", name === "jobs");
  if (name === "jobs") refreshJobs();
}

function setMsg(el, text, kind) {
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

/* ------------------------------------------------------------------- setup */
function buildEngines() {
  const sel = $("#engine-select");
  sel.replaceChildren();
  const engines = state.config.engines || [{ name: state.config.engine.name, label: state.config.engine.name, default: true }];
  for (const e of engines) {
    sel.append(h("option", { value: e.name, title: e.version || "" }, e.label + (e.default ? "  (default)" : "")));
  }
  const def = engines.find((e) => e.default) || engines[0];
  sel.value = def.name;
  updateEngineInfo();
}

function currentEngine() {
  return $("#engine-select").value || state.config.engine.name;
}

function updateEngineInfo() {
  const e = (state.config.engines || []).find((x) => x.name === currentEngine());
  $("#run-engine").textContent = e ? `Runs on ${e.label}${e.version ? " — " + e.version.replace(/\*/g, "").trim() : ""}` : "";
  const eb = $("#engine-badge");
  const syn = e ? e.synthetic : state.config.engine.synthetic;
  eb.textContent = `engine: ${currentEngine()}${syn ? " (SYNTHETIC DATA)" : ""}`;
  eb.classList.toggle("synthetic", !!syn);
}

function buildDesignFields() {
  const box = $("#design-fields");
  box.replaceChildren();
  for (const f of DESIGN_FIELDS) {
    const input = h("input", { type: "number", step: "any", value: f.def, id: `d-${f.key}` });
    state.inputs[f.key] = input;
    box.append(h("label", {}, fieldLabel(`${f.label} [${f.unit}]`, f.tip), input));
  }
}

function buildTolerance() {
  const box = $("#tol-fields");
  box.replaceChildren();
  for (const f of TOL_FIELDS) {
    const input = h("input", { type: "number", step: "any", min: "0", placeholder: f.ph, id: `t-${f.key}` });
    input.addEventListener("input", renderTolPreview);
    state.tol[f.key] = input;
    box.append(h("label", {}, fieldLabel(`${f.label} [${f.unit}]`, f.tip), input));
  }
}

function tolerance() {
  const out = {};
  for (const f of TOL_FIELDS) {
    const raw = state.tol[f.key].value.trim();
    if (raw === "") continue;
    const v = Number(raw);
    if (!Number.isFinite(v) || v <= 0) throw new Error(`${f.label}: enter a positive number`);
    out[f.key] = v;
  }
  return Object.keys(out).length ? out : null;
}

function renderTolPreview() {
  const ul = $("#tol-preview");
  ul.replaceChildren();
  let t;
  try { t = tolerance(); } catch (e) { ul.append(h("li", { class: "fail-text" }, e.message)); return; }
  if (!t) return;
  const vout = Number(state.inputs.vout_nom.value) || 0;
  const tests = new Set(selectedTests());
  const lines = [];
  if (t.vout_pct) lines.push(`Vout within ${fmt(vout * (1 - t.vout_pct / 100))} … ${fmt(vout * (1 + t.vout_pct / 100))} V (steady state, sweeps, after each step)`);
  if (t.transient_pct && (tests.has("load_transient") || tests.has("line_transient"))) lines.push(`Peak deviation during steps ≤ ${fmt(t.transient_pct)} % (${fmt(vout * t.transient_pct / 100)} V)`);
  if (t.ripple_pct) lines.push(`Ripple ≤ ${fmt(vout * t.ripple_pct * 10)} mV pk-pk`);
  if (t.efficiency_min_pct) lines.push(`Efficiency ≥ ${fmt(t.efficiency_min_pct)} %`);
  if (t.settle_max_us && (tests.has("load_transient") || tests.has("line_transient"))) lines.push(`Settling time ≤ ${fmt(t.settle_max_us)} µs`);
  for (const l of lines) ul.append(h("li", {}, "→ " + l));
}

function buildTests() {
  const box = $("#tests");
  box.replaceChildren();
  state.testBoxes = {};
  for (const t of state.config.tests) {
    const cb = h("input", { type: "checkbox", checked: true });
    state.testBoxes[t.name] = cb;
    cb.addEventListener("change", () => { refreshLimitMetrics(); renderTolPreview(); });
    box.append(h("label", { class: "inline" }, cb, t.label, TEST_TIPS[t.name] ? tip(TEST_TIPS[t.name]) : null));
  }
  refreshLimitMetrics();
}

function selectedTests() {
  return Object.entries(state.testBoxes).filter(([, cb]) => cb.checked).map(([n]) => n);
}

function refreshLimitMetrics() {
  const sel = $("#limit-metric");
  sel.replaceChildren();
  const tests = new Set(selectedTests());
  for (const [key, info] of Object.entries(state.config.metrics)) {
    if (!tests.has(key.split(".")[0])) continue;
    sel.append(h("option", { value: key }, `${key.split(".")[0].replace("_", " ")}: ${info.label} [${info.unit}]`));
  }
}

function renderLimits() {
  const ul = $("#limit-list");
  ul.replaceChildren();
  state.limits.forEach((lim, i) => {
    const info = state.config.metrics[lim.key];
    const parts = [];
    if (lim.min !== null) parts.push(`≥ ${fmt(lim.min)}`);
    if (lim.max !== null) parts.push(`≤ ${fmt(lim.max)}`);
    ul.append(
      h("li", {},
        h("span", {}, `${info ? info.label : lim.key} ${parts.join(" and ")} ${info ? info.unit : ""}`),
        h("button", { type: "button", class: "small", onclick: () => { state.limits.splice(i, 1); renderLimits(); } }, "Remove"))
    );
  });
}

function addLimit() {
  const key = $("#limit-metric").value;
  const minRaw = $("#limit-min").value.trim();
  const maxRaw = $("#limit-max").value.trim();
  if (!key || (minRaw === "" && maxRaw === "")) return;
  const lim = { key, min: minRaw === "" ? null : Number(minRaw), max: maxRaw === "" ? null : Number(maxRaw) };
  if ((lim.min !== null && !Number.isFinite(lim.min)) || (lim.max !== null && !Number.isFinite(lim.max))) return;
  state.limits = state.limits.filter((l) => l.key !== key);
  state.limits.push(lim);
  $("#limit-min").value = "";
  $("#limit-max").value = "";
  renderLimits();
}

function applyVinTolerance() {
  const pct = Number($("#vin-tol").value);
  const nom = Number(state.inputs.vin_nom.value);
  if (!(pct > 0) || !(nom > 0)) { showBanner("Enter Vin nominal and a positive input tolerance first."); return; }
  state.inputs.vin_min.value = String(Number((nom * (1 - pct / 100)).toPrecision(6)));
  state.inputs.vin_max.value = String(Number((nom * (1 + pct / 100)).toPrecision(6)));
}

/* ----------------------------------------------------------------- mapping */
function currentMode() {
  return $("input[name=mode]:checked").value;
}

function optionList(items, valueFn, labelFn) {
  return items.map((it) => h("option", { value: valueFn(it) }, labelFn(it)));
}

function autoTag(key) {
  return state.autoFilled.has(key) ? h("span", { class: "tag auto", title: "Filled in from the analysis" }, "auto") : null;
}

function renderMapping() {
  const box = $("#mapping");
  box.replaceChildren();
  state.map = {};
  const info = state.inspect;
  if (!info) {
    box.append(h("p", { class: "hint" }, "Upload and analyze your files first."));
    return;
  }
  const topo = info.topology || { guess: {}, roles: {} };
  if (currentMode() === "circuit") {
    const src = h("select", {}, optionList(info.voltage_sources, (s) => s.name, (s) => `${s.name}   ${s.nplus} → ${s.nminus}   ${s.value}`));
    const load = h("select", {}, optionList(info.load_candidates, (s) => s.name, (s) => `${s.name}   ${s.nodes.join(" – ")}   ${s.value}`));
    const vout = h("input", { type: "text", list: "node-list", value: topo.guess.vout_node || guessVout(info) });
    const gnd = h("input", { type: "text", list: "node-list", value: topo.roles.gnd || "0" });
    const dl = h("datalist", { id: "node-list" }, optionList(info.nodes, (n) => n, (n) => n));
    state.autoFilled = new Set();
    if (topo.guess.vin_source) { src.value = topo.guess.vin_source; state.autoFilled.add("src"); }
    if (topo.guess.load_element) { load.value = topo.guess.load_element; state.autoFilled.add("load"); }
    if (topo.guess.vout_node) state.autoFilled.add("vout");
    state.map = { src, load, vout, gnd };
    box.append(
      h("label", {}, fieldLabel("Input voltage source", "The source element that supplies the converter (e.g. V1). It is replaced by the simulator's own supply, which steps and sweeps Vin.", autoTag("src")), src),
      h("label", {}, fieldLabel("Output node", "The regulated output net (e.g. out, VOUT). Vout, ripple and output power are measured here.", autoTag("vout")), vout, dl),
      h("label", {}, fieldLabel("Load element (Vout → ground)", "The resistor or current source that represents the load. It is replaced by an electronic load that the tests control.", autoTag("load")), load),
      h("label", {}, fieldLabel("Ground node", "Reference node. Almost always 0 (SPICE ground).", null), gnd)
    );
    if (!info.voltage_sources.length) box.append(h("p", { class: "msg err" }, "No top-level voltage source found. Use subcircuit mode, or add the input source (V1 ...) to the netlist."));
    if (!info.load_candidates.length) box.append(h("p", { class: "msg err" }, "No load element (R, I or B) found. Add the load to the netlist or use subcircuit mode."));
  } else {
    if (!info.subckts.length) {
      box.append(h("p", { class: "msg err" }, "No .subckt definition found in the uploaded files."));
      return;
    }
    const sc = h("select", {}, optionList(info.subckts, (s) => s.name, (s) => `${s.name} (${s.pins.join(" ")})`));
    const conv = info.subckts.find((s) => s.pins.some((p) => /^(vin|in)$/i.test(p)) && s.pins.some((p) => /^(vout|out)$/i.test(p)));
    if (conv) sc.value = conv.name;
    state.map.subckt = sc;
    const pins = h("div", { class: "grid" });
    box.append(h("label", {}, fieldLabel("Subcircuit", "The .subckt that contains the whole converter (not the controller IC)."), sc), pins);
    const fill = () => {
      const def = info.subckts.find((s) => s.name === sc.value);
      pins.replaceChildren();
      state.map.pins = {};
      const guess = (re, i) => def.pins.find((p) => re.test(p)) || def.pins[i] || "";
      const roles = [
        ["vin", "VIN pin", guess(/^(vin|in|vcc|vbat)/i, 0), "Pin that receives the input supply."],
        ["vout", "VOUT pin", guess(/^(vout|out|vo)/i, 1), "Pin where the regulated output and the load connect."],
        ["gnd", "GND pin", guess(/^(gnd|vss|0|agnd|pgnd)/i, 2), "Ground / return pin."],
      ];
      for (const [role, label, pre, t] of roles) {
        const s = h("select", {}, optionList(def.pins, (p) => p, (p) => p));
        s.value = pre;
        s.addEventListener("change", renderTies);
        state.map.pins[role] = s;
        pins.append(h("label", {}, fieldLabel(label, t), s));
      }
      state.map.ties = {};
      state.map.tieBox = h("div", { class: "grid" });
      pins.append(state.map.tieBox);
      renderTies();
    };
    const renderTies = () => {
      const def = info.subckts.find((s) => s.name === sc.value);
      const used = new Set(Object.values(state.map.pins).map((s) => s.value.toLowerCase()));
      state.map.tieBox.replaceChildren();
      state.map.ties = {};
      for (const p of def.pins) {
        if (used.has(p.toLowerCase())) continue;
        const t = h("input", { type: "text", placeholder: "vin | vout | gnd | volts (e.g. 5)", value: "gnd" });
        state.map.ties[p] = t;
        state.map.tieBox.append(h("label", {}, fieldLabel(`Tie pin ${p} to`, "Extra pins (enable, power-good, sync …) must be tied to vin, vout, gnd or a fixed voltage."), t));
      }
    };
    sc.addEventListener("change", fill);
    fill();
  }
}

function guessVout(info) {
  const lower = info.nodes.map((n) => n.toLowerCase());
  for (const pref of ["vout", "out", "output"]) {
    const i = lower.indexOf(pref);
    if (i >= 0) return info.nodes[i];
  }
  const i = lower.findIndex((n) => n.includes("out"));
  return i >= 0 ? info.nodes[i] : "";
}

/* ------------------------------------------------------------------ inspect */
function fileForm() {
  const fd = new FormData();
  const nl = $("#file-netlist").files[0];
  if (!nl) throw new Error("Choose a netlist file first.");
  fd.append("netlist", nl, nl.name);
  for (const f of $("#file-models").files) fd.append("models", f, f.name);
  const bom = $("#file-bom").files[0];
  if (bom) fd.append("bom", bom, bom.name);
  return fd;
}

async function doInspect() {
  const msg = $("#inspect-msg");
  setMsg(msg, "Analyzing…");
  state.inspected = false;
  $("#btn-run").disabled = true;
  try {
    const fd = fileForm();
    fd.append("engine", currentEngine());
    if (currentMode() === "subckt" && state.map.subckt) fd.append("subckt", state.map.subckt.value);
    state.inspect = await api("/api/inspect", { method: "POST", body: fd });
    state.inspected = true;
    const w = state.inspect.warnings.length ? "\nNotes:\n  • " + state.inspect.warnings.join("\n  • ") : "";
    setMsg(msg, `OK: ${state.inspect.statements} statements, ${state.inspect.subckts.length} subcircuit(s), ${state.inspect.voltage_sources.length} source(s).${w}`, "ok");
    if (state.inspect.topology && state.inspect.topology.scope !== "top" && !state.inspect.voltage_sources.length) {
      $("input[name=mode][value=subckt]").checked = true;
    }
    renderMapping();
    renderAnalysis();
    $("#btn-run").disabled = false;
    $("#btn-ai").disabled = !state.config.ai_enabled;
  } catch (e) {
    state.inspect = null;
    setMsg(msg, e.message, "err");
    $("#analysis").classList.add("hidden");
    renderMapping();
  }
}

/* --------------------------------------------------------------- analysis */
function renderAnalysis() {
  const box = $("#analysis");
  box.replaceChildren();
  box.classList.remove("hidden");
  const info = state.inspect;
  const topo = info.topology;
  const kindNames = { R: "resistor", C: "capacitor", L: "inductor", V: "source", D: "diode", M: "MOSFET", S: "switch", X: "IC/subcircuit", Q: "BJT", I: "current source", B: "behavioural" };
  const chips = h("div", { class: "chips" },
    Object.entries(topo.counts).map(([k, n]) => h("span", { class: "chip" }, `${n} × ${kindNames[k] || k}`)),
    topo.scope !== "top" ? h("span", { class: "chip" }, `inside .subckt ${topo.scope}`) : null);
  box.append(h("h3", {}, "What we found"), chips);

  const cols = h("div", { class: "two-col" });
  box.append(cols);
  cols.append(h("div", { class: "panel" },
    h("h4", {}, "Power stage overview", tip("Simplified drawing of the detected buck power stage with your component names. Coloured labels are the nets we identified. It is a guide for checking the mapping, not a full schematic.")),
    schematicSvg(topo),
    h("div", { class: "legend" }, ["vin", "sw", "vout", "fb", "gnd"].map((r) => h("span", {}, h("i", { class: r }), `${ROLE_TEXT[r]}: ${topo.roles[r] || "not found"}`)))));

  for (const ic of topo.ics) {
    cols.append(h("div", { class: "panel" },
      h("h4", {}, `IC ${ic.name}: ${ic.subckt || "?"} — pins`, tip("Each pin of the controller IC with the net it connects to and the parts on that net. Pin roles come from the net analysis.")),
      icSvg(ic),
      ic.defined ? null : h("p", { class: "msg err" }, `Subcircuit ${ic.subckt} is not in the uploaded files: upload its model file.`)));
  }

  // models
  const models = topo.models;
  const mrows = models.vendor.map((m) => h("tr", {},
    h("td", {}, m.element), h("td", {}, m.kind), h("td", {}, m.model),
    h("td", { class: `src-${m.source}` }, m.source === "defined" ? "✓ found in your files" : m.source === "missing" ? "✗ missing" : "built-in")));
  box.append(h("div", { class: "panel" },
    h("h4", {}, "Models", tip("Model files are needed only for ICs and vendor semiconductors. R, L and C use SPICE's ideal built-in models with the values from the netlist (no ESR/DCR unless the netlist adds them).")),
    h("p", { class: "hint" }, `${models.passives_builtin} passive part(s) (R, L, C) use SPICE's ideal built-in models — no model file needed.`),
    mrows.length ? h("table", { class: "data compact" }, h("thead", {}, h("tr", {}, h("th", {}, "Element"), h("th", {}, "Type"), h("th", {}, "Model / subckt"), h("th", {}, "Status"))), h("tbody", {}, mrows)) : h("p", { class: "hint" }, "No vendor models referenced.")));

  if (info.bom) box.append(renderBom(info.bom));
}

function renderBom(bom) {
  const panel = h("div", { class: "panel" }, h("h4", {}, `BOM: ${bom.name || ""}`, tip("Reference designators from the BOM are matched with netlist element names; R/L/C values that differ by more than 1 % are flagged.")));
  if (bom.error) { panel.append(h("p", { class: "msg err" }, bom.error)); return panel; }
  panel.append(h("div", { class: "chips" },
    h("span", { class: "chip" }, `${bom.items.length} BOM line(s)`),
    h("span", { class: "chip" }, `${bom.matched} matched`),
    h("span", { class: "chip" + (bom.mismatched.length ? " fail-text" : "") }, `${bom.mismatched.length} value mismatch(es)`)));
  if (bom.mismatched.length) {
    panel.append(h("table", { class: "data compact" },
      h("thead", {}, h("tr", {}, h("th", {}, "Ref"), h("th", {}, "BOM value"), h("th", {}, "Netlist value"))),
      h("tbody", {}, bom.mismatched.map((m) => h("tr", {}, h("td", {}, m.ref), h("td", { class: "fail-text" }, m.bom_value), h("td", {}, m.netlist_value))))));
  }
  if (bom.only_in_netlist.length) panel.append(h("p", { class: "hint" }, "In the netlist but not in the BOM: " + bom.only_in_netlist.join(", ")));
  if (bom.only_in_bom.length) panel.append(h("p", { class: "hint" }, "In the BOM but not in the netlist: " + bom.only_in_bom.join(", ")));
  panel.append(h("details", {}, h("summary", {}, "Show BOM"),
    h("table", { class: "data compact" },
      h("thead", {}, h("tr", {}, ["Ref", "Value", "Tol.", "Part number", "Description"].map((x) => h("th", {}, x)))),
      h("tbody", {}, bom.items.map((b) => h("tr", {}, h("td", {}, b.ref), h("td", {}, b.value), h("td", {}, b.tolerance), h("td", {}, b.mpn), h("td", {}, b.description)))))));
  return panel;
}

function roleCls(role) { return role ? `r-${role}` : ""; }

function schematicSvg(topo) {
  // Fixed buck layout, labelled with what was detected.
  const W = 520, H = 230;
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, class: "schem", role: "img", "aria-label": "Detected power stage" });
  const els = Object.fromEntries(topo.elements.map((e) => [e.name.toLowerCase(), e]));
  const g = topo.guess, r = topo.roles;
  const find = (pred) => topo.elements.find(pred);
  const swEl = find((e) => "SMXQ".includes(e.kind) && e.nodes.includes(r.sw)) || null;
  const lowEl = find((e) => (e.kind === "D" || e.kind === "M" || e.kind === "S") && e !== swEl && e.nodes.includes(r.sw) && e.nodes.some((n) => n === r.gnd)) || null;
  const cout = find((e) => e.kind === "C" && e.nodes.includes(r.vout)) || null;
  const ind = g.inductor ? els[g.inductor.toLowerCase()] : null;
  const load = g.load_element ? els[g.load_element.toLowerCase()] : null;
  const src = g.vin_source ? els[g.vin_source.toLowerCase()] : null;
  const ic = topo.ics[0];
  const lbl = (e, fallback) => (e ? `${e.name}${e.value ? " " + e.value : ""}` : fallback);
  const t = (x, y, text, c, anchor) => sv("text", { x, y, class: c || "", "text-anchor": anchor || "middle" }, text);
  const yT = 60, yG = 190;
  // wires
  svg.append(
    sv("path", { d: `M40 ${yT} H150 M210 ${yT} H260 M330 ${yT} H470 M40 ${yG} H470`, class: "w" }),
    sv("path", { d: `M40 ${yT} V105 M40 145 V${yG}`, class: "w" }),
    sv("path", { d: `M235 ${yT} V105 M235 145 V${yG}`, class: "w" }),
    sv("path", { d: `M390 ${yT} V105 M390 145 V${yG}`, class: "w" }),
    sv("path", { d: `M470 ${yT} V100 M470 150 V${yG}`, class: "w" })
  );
  // source
  svg.append(sv("circle", { cx: 40, cy: 125, r: 20, class: "box" }), t(40, 121, "+"), t(40, 136, "−"), t(40, 214, lbl(src, "Vin source"), "lbl2"));
  // high-side switch / IC
  svg.append(sv("rect", { x: 150, y: yT - 16, width: 60, height: 32, rx: 4, class: "box" }), t(180, yT + 4, swEl ? swEl.name : "switch"));
  svg.append(t(180, yT - 22, swEl ? (swEl.model || swEl.kind_label) : "high side", "lbl2"));
  // low-side diode / FET
  svg.append(sv("rect", { x: 220, y: 105, width: 30, height: 40, rx: 4, class: "box" }), t(235, 129, lowEl ? lowEl.kind : "D"), t(258, 129, lbl(lowEl, "low side"), "lbl2", "start"));
  // inductor
  svg.append(sv("path", { d: `M260 ${yT} q8.75 -14 17.5 0 q8.75 -14 17.5 0 q8.75 -14 17.5 0 q8.75 -14 17.5 0`, class: "w" }), t(295, yT - 20, lbl(ind, "L")));
  // output cap
  svg.append(sv("path", { d: "M375 105 H405 M375 115 H405", class: "w" }), sv("path", { d: "M390 115 V145", class: "w" }), t(410, 140, lbl(cout, "Cout"), "lbl2", "start"));
  // load
  svg.append(sv("rect", { x: 458, y: 100, width: 24, height: 50, class: "box" }), t(470, 168, "load", "lbl2"), t(470, 214, lbl(load, "load"), "lbl2"));
  // controller / feedback hint
  if (ic) {
    const fbPin = ic.pins.find((p) => p.role === "fb");
    svg.append(sv("path", { d: `M180 ${yT + 16} V96`, class: "w", "stroke-dasharray": "3 3" }));
    svg.append(sv("rect", { x: 132, y: 96, width: 76, height: 34, rx: 4, class: "box" }));
    svg.append(t(170, 110, `${ic.name} ${ic.subckt || ""}`), t(170, 124, fbPin ? `FB ← ${fbPin.net}` : "controller", "lbl2"));
  }
  // nodes
  const node = (x, y, name, role) => {
    if (!name) return;
    svg.append(sv("circle", { cx: x, cy: y, r: 3.5, class: "dot" }), t(x, y - 9, name, `node ${roleCls(role)}`));
  };
  node(40, yT, r.vin, "vin");
  node(235, yT, r.sw, "sw");
  node(390, yT, r.vout, "vout");
  node(255, yG, r.gnd, "gnd");
  if (r.fb) svg.append(t(470, 30, `feedback: ${r.fb}`, `node ${roleCls("fb")}`, "end"));
  return svg;
}

function icSvg(ic) {
  const n = ic.pins.length;
  const left = ic.pins.slice(0, Math.ceil(n / 2));
  const right = ic.pins.slice(Math.ceil(n / 2));
  const rows = Math.max(left.length, right.length, 1);
  const W = 520, boxX = 200, boxW = 120, top = 24, pitch = 30, H = top + rows * pitch + 20;
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, class: "schem", role: "img", "aria-label": `Pins of ${ic.name}` });
  svg.append(sv("rect", { x: boxX, y: top - 8, width: boxW, height: rows * pitch + 4, rx: 5, class: "box" }));
  svg.append(sv("text", { x: boxX + boxW / 2, y: top - 12, "text-anchor": "middle" }, `${ic.name} · ${ic.subckt || ""}`));
  const pinRow = (p, i, side) => {
    const y = top + i * pitch + 10;
    const x0 = side === "l" ? boxX : boxX + boxW;
    const x1 = side === "l" ? boxX - 40 : boxX + boxW + 40;
    svg.append(sv("line", { x1: x0, y1: y, x2: x1, y2: y, class: `w ${p.role ? "s-" + p.role : ""}` }));
    svg.append(sv("text", { x: side === "l" ? boxX + 6 : boxX + boxW - 6, y: y + 4, "text-anchor": side === "l" ? "start" : "end" }, p.pin));
    const tx = side === "l" ? x1 - 6 : x1 + 6;
    const anchor = side === "l" ? "end" : "start";
    svg.append(sv("text", { x: tx, y: y - 1, "text-anchor": anchor, class: `node ${roleCls(p.role)}` }, p.net + (p.role ? ` (${ROLE_TEXT[p.role]})` : "")));
    if (p.attached.length) svg.append(sv("text", { x: tx, y: y + 11, "text-anchor": anchor, class: "lbl2" }, p.attached.join(", ")));
  };
  left.forEach((p, i) => pinRow(p, i, "l"));
  right.forEach((p, i) => pinRow(p, i, "r"));
  return svg;
}

/* --------------------------------------------------------------------- AI */
async function doExplain() {
  const panel = $("#ai-panel");
  panel.classList.remove("hidden");
  panel.replaceChildren(h("div", { class: "msg" }, "Asking Claude about this circuit… (this can take up to a minute)"));
  $("#btn-ai").disabled = true;
  try {
    const fd = fileForm();
    fd.append("engine", currentEngine());
    state.ai = await api("/api/ai/explain", { method: "POST", body: fd });
    renderAi(state.ai);
  } catch (e) {
    panel.replaceChildren(h("div", { class: "msg err" }, e.message));
  } finally {
    $("#btn-ai").disabled = !state.config.ai_enabled;
  }
}

function renderAi(a) {
  const panel = $("#ai-panel");
  panel.replaceChildren();
  const box = h("div", { class: "panel ai-box" }, h("h4", {}, "AI explanation", h("span", { class: "tag ai" }, a.model || "Claude"),
    tip("Generated by an AI model from your netlist and BOM. Treat it as a second opinion and check it against the datasheet.")));
  box.append(h("p", {}, a.summary), h("p", { class: "hint" }, a.topology));
  if (a.controller && a.controller.element) box.append(h("p", {}, h("b", {}, `Controller ${a.controller.element} (${a.controller.model}): `), a.controller.description));
  if (a.pins && a.pins.length) {
    box.append(h("table", { class: "data compact" },
      h("thead", {}, h("tr", {}, h("th", {}, "Pin"), h("th", {}, "Net"), h("th", {}, "Function"))),
      h("tbody", {}, a.pins.map((p) => h("tr", {}, h("td", {}, p.pin), h("td", {}, p.net), h("td", {}, p.function))))));
  }
  const m = a.suggested_mapping || {};
  const d = a.design_estimates || {};
  box.append(h("dl", { class: "kv2" },
    h("dt", {}, "Suggested mapping"), h("dd", {}, `source ${m.vin_source || "?"}, output ${m.vout_node || "?"}, load ${m.load_element || "?"}, ground ${m.gnd_node || "?"}`),
    h("dt", {}, "Estimated design"), h("dd", {}, [d.vin_nom_v != null ? `Vin ${fmt(d.vin_nom_v)} V` : null, d.vout_nom_v != null ? `Vout ${fmt(d.vout_nom_v)} V` : null, d.iout_max_a != null ? `Iout ${fmt(d.iout_max_a)} A` : null, d.fsw_khz != null ? `fsw ${fmt(d.fsw_khz)} kHz` : null].filter(Boolean).join(", ") || "—")));
  if (a.issues && a.issues.length) box.append(h("h4", {}, "Points to check"), h("ul", { class: "warnlist" }, a.issues.map((x) => h("li", {}, x))));
  box.append(h("div", { class: "row" }, h("button", { type: "button", onclick: applyAiSuggestion }, "Apply suggested mapping and values")));
  panel.append(box);
}

function applyAiSuggestion() {
  const a = state.ai;
  if (!a) return;
  const m = a.suggested_mapping || {};
  if (currentMode() === "circuit" && state.map.src) {
    const has = (sel, v) => [...sel.options].some((o) => o.value === v);
    if (m.vin_source && has(state.map.src, m.vin_source)) state.map.src.value = m.vin_source;
    if (m.load_element && has(state.map.load, m.load_element)) state.map.load.value = m.load_element;
    if (m.vout_node) state.map.vout.value = m.vout_node;
    if (m.gnd_node) state.map.gnd.value = m.gnd_node;
  }
  const d = a.design_estimates || {};
  if (d.vin_nom_v) state.inputs.vin_nom.value = String(d.vin_nom_v);
  if (d.vout_nom_v) state.inputs.vout_nom.value = String(d.vout_nom_v);
  if (d.iout_max_a) state.inputs.iout_max.value = String(d.iout_max_a);
  if (d.fsw_khz) state.inputs.fsw_hz.value = String(d.fsw_khz);
  showBanner("Applied the AI suggestion. Please review sections 2 and 3.", true);
}

/* --------------------------------------------------------------- build spec */
function num(raw, label) {
  const v = Number(raw);
  if (raw === "" || !Number.isFinite(v)) throw new Error(`${label}: enter a number`);
  return v;
}

function buildSpec() {
  const design = {};
  for (const f of DESIGN_FIELDS) {
    const raw = state.inputs[f.key].value.trim();
    if (raw === "") {
      if (!f.optional) throw new Error(`${f.label}: required`);
      continue;
    }
    const v = num(raw, f.label);
    design[f.key] = f.int ? Math.round(v) : Number((v * (f.scale || 1)).toPrecision(12));
  }
  const spec = {
    name: $("#job-name").value.trim(),
    engine: currentEngine(),
    mode: currentMode(),
    design,
    tests: selectedTests(),
    limits: {},
    notes: $("#job-notes").value.trim(),
  };
  if (!spec.tests.length) throw new Error("Select at least one test.");
  const tol = tolerance();
  if (tol) spec.tolerance = tol;
  for (const l of state.limits) {
    const o = {};
    if (l.min !== null) o.min = l.min;
    if (l.max !== null) o.max = l.max;
    spec.limits[l.key] = o;
  }
  if (spec.mode === "circuit") {
    const m = state.map;
    if (!m.src || !m.src.value) throw new Error("Choose the input voltage source.");
    if (!m.load || !m.load.value) throw new Error("Choose the load element.");
    if (!m.vout.value.trim()) throw new Error("Enter the output node name.");
    spec.circuit = { vin_source: m.src.value, vout_node: m.vout.value.trim(), load_element: m.load.value, gnd_node: m.gnd.value.trim() || "0" };
  } else {
    const m = state.map;
    if (!m.subckt) throw new Error("No subcircuit available.");
    const ties = {};
    for (const [pin, input] of Object.entries(m.ties || {})) ties[pin] = input.value.trim();
    spec.subckt = { subckt: m.subckt.value, vin_pin: m.pins.vin.value, vout_pin: m.pins.vout.value, gnd_pin: m.pins.gnd.value, ties };
  }
  return spec;
}

async function runJob() {
  const msg = $("#run-msg");
  try {
    const spec = buildSpec();
    const fd = fileForm();
    fd.append("spec", JSON.stringify(spec));
    $("#btn-run").disabled = true;
    setMsg(msg, "Submitting…");
    const res = await api("/api/jobs", { method: "POST", body: fd });
    setMsg(msg, "");
    showView("jobs");
    openJob(res.id);
  } catch (e) {
    setMsg(msg, e.message, "err");
  } finally {
    $("#btn-run").disabled = !state.inspected;
  }
}

/* -------------------------------------------------------------------- demo */
async function loadExample() {
  const msg = $("#inspect-msg");
  try {
    setMsg(msg, "Loading demo files…");
    const put = async (sel, names) => {
      const dt = new DataTransfer();
      for (const n of names) dt.items.add(new File([await api(`/api/examples/${n}`, { as: "blob" })], n));
      $(sel).files = dt.files;
    };
    await put("#file-netlist", ["buck_demo.cir"]);
    await put("#file-models", ["toyctrl.lib"]);
    await put("#file-bom", ["buck_demo_bom.csv"]);
    const spec = await api("/api/examples/buck_demo.spec.json");
    $("input[name=mode][value=circuit]").checked = true;
    await doInspect();
    applySpec(spec);
    setMsg($("#run-msg"), "Demo loaded. Review the settings and press Run.", "ok");
  } catch (e) {
    setMsg(msg, e.message, "err");
  }
}

function applySpec(spec) {
  for (const f of DESIGN_FIELDS) {
    const v = spec.design[f.key];
    // Fields the spec does not set fall back to the form default (optional ones stay blank).
    state.inputs[f.key].value = v === undefined ? String(f.def) : String(Number((v / (f.scale || 1)).toPrecision(10)));
  }
  $("#job-name").value = spec.name || "";
  $("#job-notes").value = spec.notes || "";
  for (const [n, cb] of Object.entries(state.testBoxes)) cb.checked = (spec.tests || []).includes(n);
  refreshLimitMetrics();
  for (const f of TOL_FIELDS) {
    const v = spec.tolerance ? spec.tolerance[f.key] : undefined;
    state.tol[f.key].value = v === undefined || v === null ? "" : String(v);
  }
  renderTolPreview();
  state.limits = Object.entries(spec.limits || {}).map(([key, l]) => ({ key, min: l.min ?? null, max: l.max ?? null }));
  renderLimits();
  if (spec.engine && [...$("#engine-select").options].some((o) => o.value === spec.engine)) {
    $("#engine-select").value = spec.engine;
    updateEngineInfo();
  }
  const radio = document.querySelector(`input[name=mode][value=${spec.mode}]`);
  if (radio) radio.checked = true;
  renderMapping();
  if (spec.mode === "circuit" && spec.circuit && state.map.src) {
    state.map.src.value = spec.circuit.vin_source;
    state.map.load.value = spec.circuit.load_element;
    state.map.vout.value = spec.circuit.vout_node;
    state.map.gnd.value = spec.circuit.gnd_node;
  }
}

/* -------------------------------------------------------------------- jobs */
async function refreshJobs() {
  try {
    const { jobs } = await api("/api/jobs?limit=40");
    const tb = $("#jobs-table tbody");
    tb.replaceChildren();
    for (const j of jobs) {
      const verdict = j.summary ? j.summary.verdict : "";
      tb.append(
        h("tr", { class: "clickable", onclick: () => openJob(j.id) },
          h("td", {}, j.name || j.id.slice(0, 8)),
          h("td", {}, j.engine || ""),
          h("td", {}, h("span", { class: `pill ${cls(j.status)}` }, j.status)),
          h("td", {}, verdict ? h("span", { class: `pill ${cls(verdict)}` }, verdict) : ""),
          h("td", {}, fmtTime(j.created_at)),
          h("td", {}, h("button", { type: "button", class: "small", onclick: (ev) => { ev.stopPropagation(); removeJob(j); } }, j.status === "queued" || j.status === "running" ? "Cancel" : "Delete")))
      );
    }
  } catch (e) {
    showBanner(e.message);
  }
}

async function removeJob(j) {
  try {
    if (j.status === "queued" || j.status === "running") await api(`/api/jobs/${j.id}/cancel`, { method: "POST" });
    else await api(`/api/jobs/${j.id}`, { method: "DELETE" });
    if (state.jobId === j.id && !(j.status === "queued" || j.status === "running")) $("#job-detail").replaceChildren();
    refreshJobs();
  } catch (e) {
    showBanner(e.message);
  }
}

function showBanner(text, info) {
  const b = $("#banner");
  b.textContent = text;
  b.className = "banner" + (info ? " info" : "");
  setTimeout(() => b.classList.add("hidden"), 8000);
}

function openJob(id) {
  state.jobId = id;
  if (state.poll) clearTimeout(state.poll);
  const tick = async () => {
    if (state.jobId !== id) return;
    try {
      const job = await api(`/api/jobs/${id}`);
      renderJob(job);
      refreshJobs();
      if (job.status === "queued" || job.status === "running") state.poll = setTimeout(tick, 1500);
    } catch (e) {
      $("#job-detail").replaceChildren(h("div", { class: "card" }, h("div", { class: "msg err" }, e.message)));
    }
  };
  tick();
}

/* ------------------------------------------------------------------ render */
function reportButton(job, kind, label) {
  const stem = (job.name || "buck-sim").replace(/[^A-Za-z0-9_-]+/g, "_").slice(0, 40);
  const btn = h("button", { type: "button" }, label);
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "Preparing…";
    try {
      await download(`/api/jobs/${job.id}/report.${kind}`, `${stem}-${job.id.slice(0, 8)}.${kind}`);
    } catch (e) {
      showBanner(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  });
  return btn;
}

function renderJob(job) {
  const root = $("#job-detail");
  root.replaceChildren();
  const head = h("div", { class: "card" },
    h("h2", {}, job.name || job.id.slice(0, 8)),
    h("div", { class: "kv" },
      h("span", {}, "Status: ", h("b", {}, job.status)),
      h("span", {}, "Engine: ", h("b", {}, job.engine)),
      h("span", {}, "Created: ", h("b", {}, fmtTime(job.created_at))),
      job.progress ? h("span", {}, "Progress: ", h("b", {}, job.progress)) : null
    )
  );
  root.append(head);
  if (job.error) head.append(h("div", { class: "msg err" }, job.error));
  const res = job.results;
  if (!res) return;
  if (res.engine.synthetic) {
    head.append(h("div", { class: "banner" }, "SYNTHETIC DATA – these results come from the built-in mock engine, not from a SPICE simulator. Do not use them for design decisions."));
  }
  const sm = res.summary;
  head.append(
    h("div", { class: "kv" },
      h("span", {}, "Verdict: ", h("span", { class: `pill ${cls(sm.verdict)}` }, sm.verdict)),
      h("span", {}, "Checks failed: ", h("b", {}, `${sm.checks_failed} / ${sm.checks_total}`)),
      h("span", {}, "Tests with errors: ", h("b", {}, String(sm.tests_error))),
      h("span", {}, "Simulator: ", h("b", {}, res.engine.version)),
      h("span", {}, "Run time: ", h("b", {}, `${fmt(res.duration_s)} s`))
    ),
    h("div", { class: "row" },
      reportButton(job, "pdf", "Download PDF report"),
      reportButton(job, "docx", "Download Word report"),
      h("button", { type: "button", onclick: () => download(`/api/jobs/${job.id}/report.csv`, `buck-sim-${job.id.slice(0, 8)}.csv`).catch((e) => showBanner(e.message)) }, "Download CSV"),
      h("button", { type: "button", onclick: () => showLog(job.id) }, "Show log"),
      tip("The PDF and Word reports contain the job settings, tolerance, every metric with its limit and result, all charts and the BOM (if one was uploaded).")
    ),
    h("div", { id: "log-box" })
  );
  if (job.spec && job.spec.tolerance) {
    const t = job.spec.tolerance;
    const parts = TOL_FIELDS.filter((f) => t[f.key] != null).map((f) => `${f.label} ${fmt(t[f.key])} ${f.unit}`);
    if (parts.length) head.append(h("p", { class: "hint" }, "Tolerance: " + parts.join(" · ")));
  }
  if (job.spec && job.spec.notes) head.append(h("p", { class: "hint" }, "Notes: " + job.spec.notes));
  if (res.warnings.length) head.append(h("ul", { class: "warnlist" }, res.warnings.map((w) => h("li", {}, w))));
  for (const t of res.tests) root.append(renderTest(job, res, t));
}

async function showLog(id) {
  const box = $("#log-box");
  try {
    const text = await api(`/api/jobs/${id}/log`, { as: "text" });
    box.replaceChildren(h("pre", { class: "log" }, text || "(empty)"));
  } catch (e) {
    box.replaceChildren(h("div", { class: "msg err" }, e.message));
  }
}

function renderTest(job, res, t) {
  const card = h("div", { class: "card testcard" });
  card.append(h("h3", {}, t.label, h("span", { class: `pill ${cls(t.status)}` }, t.status), h("span", { class: "hint" }, `${fmt(t.duration_s)} s`)));
  if (t.error) {
    card.append(h("div", { class: "msg err" }, t.error));
    if (t.log_tail) card.append(h("pre", { class: "log" }, t.log_tail));
  }
  if (t.warnings.length) card.append(h("ul", { class: "warnlist" }, t.warnings.map((w) => h("li", {}, w))));
  const checks = Object.fromEntries(t.checks.map((c) => [c.key, c]));
  const rows = Object.entries(t.metrics).map(([m, v]) => {
    const key = `${t.test}.${m}`;
    const info = res.metric_info[key] || { label: m, unit: "" };
    const c = checks[key];
    const lim = c ? [c.min !== null ? `≥ ${fmt(c.min)}` : "", c.max !== null ? `≤ ${fmt(c.max)}` : ""].filter(Boolean).join("  ") : "";
    return h("tr", {},
      h("td", {}, info.label),
      h("td", { class: "num" }, fmt(v)),
      h("td", {}, info.unit),
      h("td", {}, lim),
      h("td", {}, c ? h("span", { class: c.passed ? "pass-text" : "fail-text" }, c.passed ? "PASS" : "FAIL") : ""));
  });
  if (rows.length) {
    card.append(h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, "Metric"), h("th", {}, "Value"), h("th", {}, "Unit"), h("th", {}, "Limit"), h("th", {}, ""))),
      h("tbody", {}, rows)));
  }
  const charts = h("div", { class: "charts" });
  card.append(charts);
  if (t.points && t.points.length) {
    const xs = t.points.map((p) => p.x);
    charts.append(lineChart({ title: "Output voltage", xlabel: t.x_label, ylabel: "Vout mean (V)", series: [{ name: "Vout", x: xs, y: t.points.map((p) => p.vout_mean_v) }], markers: true }));
    charts.append(lineChart({ title: "Efficiency", xlabel: t.x_label, ylabel: "Efficiency (%)", series: [{ name: "Efficiency", x: xs, y: t.points.map((p) => p.efficiency_pct) }], markers: true }));
    charts.append(lineChart({ title: "Output ripple", xlabel: t.x_label, ylabel: "Ripple pk-pk (mV)", series: [{ name: "Ripple", x: xs, y: t.points.map((p) => (p.ripple_pp_v === null ? null : p.ripple_pp_v * 1000)) }], markers: true }));
  }
  if (t.waveform) {
    const holder = h("div", { class: "msg" }, "Loading waveforms…");
    charts.append(holder);
    api(`/api/jobs/${job.id}/waveforms/${t.test}`).then((w) => {
      holder.remove();
      charts.append(...waveCharts(t.test, w));
    }).catch((e) => { holder.textContent = e.message; holder.className = "msg err"; });
    if (["steady_state", "load_transient", "line_transient"].includes(t.test)) {
      card.append(h("div", { class: "row" }, h("button", { type: "button", onclick: () => download(`/api/jobs/${job.id}/testbench/${t.test}/${t.test}`, `${t.test}.cir`).catch((e) => showBanner(e.message)) }, "Download generated testbench")));
    }
  }
  return card;
}

function waveCharts(test, w) {
  const out = [];
  const x = w.time_us;
  out.push(lineChart({ title: "Output voltage (cycle-averaged)", xlabel: "Time (µs)", ylabel: "Vout (V)", series: [{ name: "Vout", x, y: w.series.vout_v }], events: w.events_us }));
  const second = test === "line_transient"
    ? { name: "Vin", x, y: w.series.vin_v }
    : { name: "Iload", x, y: w.series.iload_a };
  out.push(lineChart({ title: test === "line_transient" ? "Input voltage" : "Load current", xlabel: "Time (µs)", ylabel: test === "line_transient" ? "Vin (V)" : "Iload (A)", series: [second], events: w.events_us }));
  out.push(lineChart({ title: "Input current (cycle-averaged)", xlabel: "Time (µs)", ylabel: "Iin (A)", series: [{ name: "Iin", x, y: w.series.iin_a }], events: w.events_us }));
  if (w.detail) {
    out.push(lineChart({ title: `Output ripple (last 5 cycles; min ${fmt(w.detail.vout_min_v)} V, max ${fmt(w.detail.vout_max_v)} V)`, xlabel: "Time (µs)", ylabel: "Vout (V)", series: [{ name: "Vout", x: w.detail.time_us, y: w.detail.vout_v }] }));
  }
  return out;
}

/* ------------------------------------------------------------------ charts */
function niceTicks(min, max, n) {
  if (!(max > min)) return [min];
  const raw = (max - min) / Math.max(n, 1);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const norm = raw / mag;
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 1e-9; v += step) ticks.push(Number(v.toPrecision(12)));
  return ticks;
}

function lineChart(opts) {
  const W = 560, H = 270, m = { l: 66, r: 14, t: 26, b: 42 };
  const pw = W - m.l - m.r, ph = H - m.t - m.b;
  const svg = sv("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": opts.title });
  const series = opts.series.map((s, i) => ({ ...s, idx: i % 4, pts: s.x.map((x, k) => [x, s.y[k]]).filter((p) => p[1] !== null && Number.isFinite(p[1])) }));
  const allX = series.flatMap((s) => s.pts.map((p) => p[0]));
  if (!allX.length) return h("div", { class: "msg" }, `${opts.title}: no data`);
  const full = { x0: Math.min(...allX), x1: Math.max(...allX) };
  let view = { ...full };
  let drag = null;
  const g = sv("g", {});
  svg.append(g);

  const draw = () => {
    g.replaceChildren();
    const xs = (x) => m.l + ((x - view.x0) / (view.x1 - view.x0 || 1)) * pw;
    const vis = series.map((s) => s.pts.filter((p) => p[0] >= view.x0 && p[0] <= view.x1));
    const ys = vis.flatMap((v) => v.map((p) => p[1]));
    let y0 = ys.length ? Math.min(...ys) : 0, y1 = ys.length ? Math.max(...ys) : 1;
    if (y1 - y0 < 1e-12 * Math.max(1, Math.abs(y1))) { const d = Math.max(Math.abs(y1) * 0.01, 1e-6); y0 -= d; y1 += d; }
    const pad = (y1 - y0) * 0.06; y0 -= pad; y1 += pad;
    const yp = (y) => m.t + ph - ((y - y0) / (y1 - y0)) * ph;
    g.append(sv("text", { x: m.l, y: 15, class: "title" }, opts.title));
    for (const t of niceTicks(y0, y1, 5)) {
      g.append(sv("line", { x1: m.l, x2: W - m.r, y1: yp(t), y2: yp(t), class: "grid" }));
      g.append(sv("text", { x: m.l - 6, y: yp(t) + 4, "text-anchor": "end", class: "tick" }, fmt(t, 4)));
    }
    for (const t of niceTicks(view.x0, view.x1, 6)) {
      g.append(sv("line", { x1: xs(t), x2: xs(t), y1: m.t, y2: m.t + ph, class: "grid" }));
      g.append(sv("text", { x: xs(t), y: m.t + ph + 15, "text-anchor": "middle", class: "tick" }, fmt(t, 4)));
    }
    g.append(sv("rect", { x: m.l, y: m.t, width: pw, height: ph, class: "axis" }));
    g.append(sv("text", { x: m.l + pw / 2, y: H - 6, "text-anchor": "middle" }, opts.xlabel || ""));
    const yl = sv("text", { x: 12, y: m.t + ph / 2, "text-anchor": "middle", transform: `rotate(-90 12 ${m.t + ph / 2})` }, opts.ylabel || "");
    g.append(yl);
    if (opts.events) {
      for (const [name, t] of Object.entries(opts.events)) {
        if (!["t_up", "t_down", "meas_start", "meas_end"].includes(name) || t < view.x0 || t > view.x1) continue;
        g.append(sv("line", { x1: xs(t), x2: xs(t), y1: m.t, y2: m.t + ph, class: "cursor" }));
      }
    }
    vis.forEach((pts, i) => {
      if (!pts.length) return;
      const d = pts.map((p, k) => `${k ? "L" : "M"}${xs(p[0]).toFixed(1)} ${yp(p[1]).toFixed(1)}`).join("");
      g.append(sv("path", { d, class: `line s${series[i].idx}` }));
      if (opts.markers) for (const p of pts) g.append(sv("circle", { cx: xs(p[0]), cy: yp(p[1]), r: 3, class: `f${series[i].idx}` }));
    });
    // interaction layer
    const cursor = sv("line", { y1: m.t, y2: m.t + ph, class: "cursor", visibility: "hidden" });
    const tip = sv("g", { visibility: "hidden" });
    const tipBox = sv("rect", { class: "tipbox", rx: 4, height: 16 });
    const tipText = sv("text", {}, "");
    tip.append(tipBox, tipText);
    const sel = sv("rect", { y: m.t, height: ph, class: "sel", visibility: "hidden" });
    const overlay = sv("rect", { x: m.l, y: m.t, width: pw, height: ph, fill: "transparent" });
    g.append(sel, cursor, tip, overlay);
    const toX = (ev) => {
      const r = svg.getBoundingClientRect();
      const px = ((ev.clientX - r.left) / r.width) * W;
      return Math.min(Math.max(px, m.l), m.l + pw);
    };
    const xval = (px) => view.x0 + ((px - m.l) / pw) * (view.x1 - view.x0);
    overlay.addEventListener("pointermove", (ev) => {
      const px = toX(ev);
      if (drag !== null) {
        sel.setAttribute("x", Math.min(drag, px));
        sel.setAttribute("width", Math.abs(px - drag));
        sel.setAttribute("visibility", "visible");
        return;
      }
      const xv = xval(px);
      const parts = [];
      vis.forEach((pts, i) => {
        if (!pts.length) return;
        let best = pts[0];
        for (const p of pts) if (Math.abs(p[0] - xv) < Math.abs(best[0] - xv)) best = p;
        parts.push(`${series[i].name}: ${fmt(best[1], 5)}`);
        if (i === 0) xv0 = best[0];
      });
      cursor.setAttribute("x1", xs(xv0)); cursor.setAttribute("x2", xs(xv0)); cursor.setAttribute("visibility", "visible");
      tipText.textContent = `x=${fmt(xv0, 5)}  ${parts.join("  ")}`;
      tipText.setAttribute("x", m.l + 6); tipText.setAttribute("y", m.t + 12);
      tipBox.setAttribute("x", m.l + 2); tipBox.setAttribute("y", m.t + 1);
      tipBox.setAttribute("width", Math.min(pw - 4, tipText.textContent.length * 6.2 + 8));
      tip.setAttribute("visibility", "visible");
    });
    let xv0 = view.x0;
    overlay.addEventListener("pointerleave", () => { cursor.setAttribute("visibility", "hidden"); tip.setAttribute("visibility", "hidden"); });
    overlay.addEventListener("pointerdown", (ev) => { drag = toX(ev); overlay.setPointerCapture(ev.pointerId); });
    overlay.addEventListener("pointerup", (ev) => {
      if (drag === null) return;
      const a = drag, b = toX(ev);
      drag = null;
      sel.setAttribute("visibility", "hidden");
      if (Math.abs(b - a) > 6) { const xa = xval(Math.min(a, b)), xb = xval(Math.max(a, b)); view = { x0: xa, x1: xb }; draw(); }
    });
    overlay.addEventListener("dblclick", () => { view = { ...full }; draw(); });
  };
  draw();
  return svg;
}

/* -------------------------------------------------------------------- init */
async function init() {
  $("#nav-new").addEventListener("click", () => showView("new"));
  $("#nav-jobs").addEventListener("click", () => showView("jobs"));
  $("#btn-inspect").addEventListener("click", doInspect);
  $("#btn-ai").addEventListener("click", doExplain);
  $("#btn-add-limit").addEventListener("click", addLimit);
  $("#btn-run").addEventListener("click", runJob);
  $("#btn-example").addEventListener("click", loadExample);
  $("#btn-vin-tol").addEventListener("click", applyVinTolerance);
  $("#engine-select").addEventListener("change", () => {
    updateEngineInfo();
    if (state.inspected) doInspect(); // model availability depends on the engine
  });
  for (const r of document.querySelectorAll("input[name=mode]")) r.addEventListener("change", renderMapping);
  for (const id of ["#file-netlist", "#file-models", "#file-bom"]) {
    $(id).addEventListener("change", () => { state.inspected = false; $("#btn-run").disabled = true; $("#btn-ai").disabled = true; });
  }
  buildDesignFields();
  buildTolerance();
  renderMapping();
  try {
    state.config = await api("/api/config");
  } catch (e) {
    showBanner("Cannot reach the server: " + e.message);
    return;
  }
  buildEngines();
  buildTests();
  state.inputs.vout_nom.addEventListener("input", renderTolPreview);
  const aiBtn = $("#btn-ai");
  aiBtn.title = state.config.ai_enabled
    ? "Send the netlist and BOM to Claude for an explanation and a suggested mapping"
    : "AI help is off: start the server with ANTHROPIC_API_KEY set to enable it";
}

document.addEventListener("DOMContentLoaded", init);

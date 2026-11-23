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
  inspect: null,
  inspected: false,
  inputs: {}, // design inputs
  map: {}, // mapping inputs
  limits: [],
  testBoxes: {},
  jobId: null,
  poll: null,
};

const DESIGN_FIELDS = [
  { key: "vin_min", label: "Vin min", unit: "V", def: 9 },
  { key: "vin_nom", label: "Vin nominal", unit: "V", def: 12 },
  { key: "vin_max", label: "Vin max", unit: "V", def: 15 },
  { key: "vout_nom", label: "Vout nominal", unit: "V", def: 5 },
  { key: "iout_max", label: "Iout max", unit: "A", def: 2 },
  { key: "iout_min", label: "Iout min (blank = 10 % of max)", unit: "A", def: "", optional: true },
  { key: "fsw_hz", label: "Switching frequency", unit: "kHz", scale: 1e3, def: 500 },
  { key: "startup_time_s", label: "Start-up time (blank = auto)", unit: "µs", scale: 1e-6, def: "", optional: true },
  { key: "hold_time_s", label: "Hold per level (blank = auto)", unit: "µs", scale: 1e-6, def: "", optional: true },
  { key: "measure_cycles", label: "Measurement window", unit: "cycles", def: 50, int: true },
  { key: "load_slew_a_per_us", label: "Load slew rate", unit: "A/µs", def: 1 },
  { key: "line_slew_v_per_us", label: "Line slew rate", unit: "V/µs", def: 1 },
  { key: "settle_band_pct", label: "Settling band", unit: "%", def: 2 },
];

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
function buildDesignFields() {
  const box = $("#design-fields");
  box.replaceChildren();
  for (const f of DESIGN_FIELDS) {
    const input = h("input", { type: "number", step: "any", value: f.def, id: `d-${f.key}` });
    state.inputs[f.key] = input;
    box.append(h("label", {}, `${f.label} [${f.unit}]`, input));
  }
}

function buildTests() {
  const box = $("#tests");
  box.replaceChildren();
  state.testBoxes = {};
  for (const t of state.config.tests) {
    const cb = h("input", { type: "checkbox", checked: true });
    state.testBoxes[t.name] = cb;
    cb.addEventListener("change", refreshLimitMetrics);
    box.append(h("label", { class: "inline" }, cb, t.label));
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
        h("button", { type: "button", onclick: () => { state.limits.splice(i, 1); renderLimits(); } }, "Remove"))
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

/* ----------------------------------------------------------------- mapping */
function currentMode() {
  return $("input[name=mode]:checked").value;
}

function optionList(items, valueFn, labelFn) {
  return items.map((it) => h("option", { value: valueFn(it) }, labelFn(it)));
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
  if (currentMode() === "circuit") {
    const src = h("select", {}, optionList(info.voltage_sources, (s) => s.name, (s) => `${s.name}  (${s.nplus} → ${s.nminus})  ${s.value}`));
    const load = h("select", {}, optionList(info.load_candidates, (s) => s.name, (s) => `${s.name}  (${s.nodes.join(" – ")})  ${s.value}`));
    const vout = h("input", { type: "text", list: "node-list", value: guessVout(info) });
    const gnd = h("input", { type: "text", value: "0" });
    const dl = h("datalist", { id: "node-list" }, optionList(info.nodes, (n) => n, (n) => n));
    state.map = { src, load, vout, gnd };
    box.append(
      h("label", {}, "Input voltage source", src),
      h("label", {}, "Output node", vout, dl),
      h("label", {}, "Load element (vout → ground)", load),
      h("label", {}, "Ground node", gnd)
    );
    if (!info.voltage_sources.length) box.append(h("p", { class: "msg err" }, "No top-level voltage source found. Use subcircuit mode, or add the input source (V1 ...) to the netlist."));
    if (!info.load_candidates.length) box.append(h("p", { class: "msg err" }, "No load element (R, I or B) found. Add the load to the netlist or use subcircuit mode."));
  } else {
    if (!info.subckts.length) {
      box.append(h("p", { class: "msg err" }, "No .subckt definition found in the uploaded files."));
      return;
    }
    const sc = h("select", {}, optionList(info.subckts, (s) => s.name, (s) => `${s.name} (${s.pins.join(" ")})`));
    state.map.subckt = sc;
    const pins = h("div", { class: "grid" });
    box.append(h("label", {}, "Subcircuit", sc), pins);
    const fill = () => {
      const def = info.subckts.find((s) => s.name === sc.value);
      pins.replaceChildren();
      state.map.pins = {};
      const guess = (re, i) => def.pins.find((p) => re.test(p)) || def.pins[i] || "";
      const roles = [
        ["vin", "VIN pin", guess(/^(vin|in|vcc|vbat)/i, 0)],
        ["vout", "VOUT pin", guess(/^(vout|out|vo)/i, 1)],
        ["gnd", "GND pin", guess(/^(gnd|vss|0|agnd|pgnd)/i, 2)],
      ];
      for (const [role, label, pre] of roles) {
        const s = h("select", {}, optionList(def.pins, (p) => p, (p) => p));
        s.value = pre;
        s.addEventListener("change", renderTies);
        state.map.pins[role] = s;
        pins.append(h("label", {}, label, s));
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
        state.map.tieBox.append(h("label", {}, `Tie pin ${p} to`, t));
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
  return fd;
}

async function doInspect() {
  const msg = $("#inspect-msg");
  setMsg(msg, "Analyzing…");
  state.inspected = false;
  $("#btn-run").disabled = true;
  try {
    state.inspect = await api("/api/inspect", { method: "POST", body: fileForm() });
    state.inspected = true;
    const w = state.inspect.warnings.length ? "\nNotes:\n  • " + state.inspect.warnings.join("\n  • ") : "";
    setMsg(msg, `OK: ${state.inspect.statements} statements, ${state.inspect.subckts.length} subcircuit(s), ${state.inspect.voltage_sources.length} source(s).${w}`, "ok");
    renderMapping();
    $("#btn-run").disabled = false;
  } catch (e) {
    state.inspect = null;
    setMsg(msg, e.message, "err");
    renderMapping();
  }
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
  const spec = { name: $("#job-name").value.trim(), mode: currentMode(), design, tests: selectedTests(), limits: {} };
  if (!spec.tests.length) throw new Error("Select at least one test.");
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
  const msg = $("#run-msg");
  try {
    setMsg(msg, "Loading demo files…");
    const names = ["buck_demo.cir", "toyctrl.lib"];
    const dtN = new DataTransfer();
    const dtM = new DataTransfer();
    for (const n of names) {
      const blob = await api(`/api/examples/${n}`, { as: "blob" });
      (n.endsWith(".cir") ? dtN : dtM).items.add(new File([blob], n));
    }
    $("#file-netlist").files = dtN.files;
    $("#file-models").files = dtM.files;
    const spec = await api("/api/examples/buck_demo.spec.json");
    await doInspect();
    applySpec(spec);
    setMsg(msg, "Demo loaded. Review the settings and press Run.", "ok");
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
  for (const [n, cb] of Object.entries(state.testBoxes)) cb.checked = (spec.tests || []).includes(n);
  refreshLimitMetrics();
  state.limits = Object.entries(spec.limits || {}).map(([key, l]) => ({ key, min: l.min ?? null, max: l.max ?? null }));
  renderLimits();
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
          h("td", {}, h("span", { class: `pill ${cls(j.status)}` }, j.status)),
          h("td", {}, verdict ? h("span", { class: `pill ${cls(verdict)}` }, verdict) : ""),
          h("td", {}, fmtTime(j.created_at)),
          h("td", {}, h("button", { type: "button", onclick: (ev) => { ev.stopPropagation(); removeJob(j); } }, j.status === "queued" || j.status === "running" ? "Cancel" : "Delete")))
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
      h("button", { type: "button", onclick: () => download(`/api/jobs/${job.id}/report.csv`, `buck-sim-${job.id.slice(0, 8)}.csv`).catch((e) => showBanner(e.message)) }, "Download CSV"),
      h("button", { type: "button", onclick: () => showLog(job.id) }, "Show log")
    ),
    h("div", { id: "log-box" })
  );
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
  $("#btn-add-limit").addEventListener("click", addLimit);
  $("#btn-run").addEventListener("click", runJob);
  $("#btn-example").addEventListener("click", loadExample);
  for (const r of document.querySelectorAll("input[name=mode]")) r.addEventListener("change", renderMapping);
  for (const id of ["#file-netlist", "#file-models"]) {
    $(id).addEventListener("change", () => { state.inspected = false; $("#btn-run").disabled = true; });
  }
  buildDesignFields();
  renderMapping();
  try {
    state.config = await api("/api/config");
  } catch (e) {
    showBanner("Cannot reach the server: " + e.message);
    return;
  }
  const eb = $("#engine-badge");
  eb.textContent = `engine: ${state.config.engine.name}${state.config.engine.synthetic ? " (SYNTHETIC DATA)" : ""}`;
  if (state.config.engine.synthetic) eb.classList.add("synthetic");
  buildTests();
}

document.addEventListener("DOMContentLoaded", init);

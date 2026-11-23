"""PDF and Word (.docx) reports of a finished job.

Both formats share one content model (``build_content``) so they always say the
same thing. Charts are rendered once with matplotlib (Agg backend, no display).
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path

from . import __version__
from .registry import TEST_LABELS

DESIGN_LABELS = [
    ("vin_min", "Vin min", "V", 1), ("vin_nom", "Vin nominal", "V", 1), ("vin_max", "Vin max", "V", 1),
    ("vout_nom", "Vout nominal", "V", 1), ("iout_max", "Iout max", "A", 1), ("iout_min", "Iout min", "A", 1),
    ("fsw_hz", "Switching frequency", "kHz", 1e-3), ("startup_time_s", "Start-up time", "us", 1e6),
    ("hold_time_s", "Hold per level", "us", 1e6), ("measure_cycles", "Measurement window", "cycles", 1),
    ("load_slew_a_per_us", "Load slew rate", "A/us", 1), ("line_slew_v_per_us", "Line slew rate", "V/us", 1),
    ("settle_band_pct", "Settling band", "%", 1),
]  # fmt: skip
TOL_LABELS = [
    ("vout_pct", "Static Vout tolerance", "+/- %"),
    ("transient_pct", "Transient Vout excursion", "+/- %"),
    ("ripple_pct", "Max ripple (pk-pk)", "% of Vout"),
    ("efficiency_min_pct", "Minimum efficiency", "%"),
    ("settle_max_us", "Max settling time", "us"),
]


def fmt(v, digits: int = 4) -> str:
    if v is None:
        return "-"
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v)
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 1e5 or a < 1e-3:
        return f"{v:.{digits - 1}e}"
    return f"{v:.{digits}g}"


# --------------------------------------------------------------------------- #
# Content model
# --------------------------------------------------------------------------- #
def build_content(job: dict, results: dict, waveforms: dict[str, dict], bom: list[dict] | None) -> dict:
    spec = job["spec"]
    design = spec.get("design", {})
    created = dt.datetime.fromtimestamp(job["created_at"]).strftime("%Y-%m-%d %H:%M")
    sm = results["summary"]
    info = [
        ("Job", job.get("name") or job["id"][:8]),
        ("Job ID", job["id"]),
        ("Created", created),
        ("Simulator", results["engine"]["version"] + ("  [SYNTHETIC - mock engine]" if results["engine"]["synthetic"] else "")),
        ("Verdict", sm["verdict"].upper()),
        ("Checks failed", f"{sm['checks_failed']} / {sm['checks_total']}"),
        ("Tests with errors", f"{sm['tests_error']} / {sm['tests_total']}"),
        ("Run time", f"{fmt(results['duration_s'])} s"),
        ("Report generated", dt.datetime.now().strftime("%Y-%m-%d %H:%M") + f" by buck-sim {__version__}"),
    ]
    design_rows = []
    for key, label, unit, scale in DESIGN_LABELS:
        v = design.get(key)
        if v is not None:
            design_rows.append((label, fmt(v * scale), unit))
    if spec.get("mode") == "circuit" and spec.get("circuit"):
        c = spec["circuit"]
        mapping = [("Mode", "Complete circuit"), ("Input source", c["vin_source"]), ("Output node", c["vout_node"]),
                   ("Load element", c["load_element"]), ("Ground node", c.get("gnd_node", "0"))]  # fmt: skip
    elif spec.get("subckt"):
        c = spec["subckt"]
        mapping = [("Mode", "Subcircuit"), ("Subcircuit", c["subckt"]), ("VIN pin", c["vin_pin"]),
                   ("VOUT pin", c["vout_pin"]), ("GND pin", c["gnd_pin"])]  # fmt: skip
        mapping += [(f"Tie {p}", t) for p, t in c.get("ties", {}).items()]
    else:
        mapping = []
    tol = spec.get("tolerance") or {}
    tol_rows = [(label, fmt(tol[k]), unit) for k, label, unit in TOL_LABELS if tol.get(k) is not None]

    tests = []
    for t in results["tests"]:
        checks = {c["key"]: c for c in t["checks"]}
        rows = []
        for m, v in t["metrics"].items():
            key = f"{t['test']}.{m}"
            mi = results["metric_info"].get(key, {"label": m, "unit": ""})
            c = checks.get(key)
            lim = ""
            if c:
                parts = []
                if c["min"] is not None:
                    parts.append(f">= {fmt(c['min'])}")
                if c["max"] is not None:
                    parts.append(f"<= {fmt(c['max'])}")
                lim = "  ".join(parts)
            rows.append((mi["label"], fmt(v), mi["unit"], lim, "" if not c else ("PASS" if c["passed"] else "FAIL")))
        tests.append(
            {
                "label": t["label"],
                "status": t["status"],
                "error": t.get("error") or "",
                "warnings": t.get("warnings", []),
                "rows": rows,
                "charts": render_charts(t, waveforms.get(t["test"])),
            }
        )
    return {
        "title": "Buck converter simulation report",
        "info": info,
        "synthetic": results["engine"]["synthetic"],
        "design": design_rows,
        "mapping": mapping,
        "tolerance": tol_rows,
        "tests_selected": ", ".join(TEST_LABELS.get(x, x) for x in spec.get("tests", [])),
        "notes": spec.get("notes", ""),
        "warnings": results.get("warnings", []),
        "tests": tests,
        "bom": bom or [],
    }


# --------------------------------------------------------------------------- #
# Charts
# --------------------------------------------------------------------------- #
def _plot(title: str, xlabel: str, ylabel: str, x, y, events: dict | None = None, markers: bool = False) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.4, 2.6), dpi=130)
    pts = [(a, b) for a, b in zip(x, y) if b is not None]
    if pts:
        xs, ys = zip(*pts)
        ax.plot(xs, ys, color="#1f6feb", linewidth=1.2, marker="o" if markers else None, markersize=3)
    for ev in (events or {}).values():
        if isinstance(ev, (int, float)):
            ax.axvline(ev, color="#888888", linestyle="--", linewidth=0.8)
    ax.set_title(title, fontsize=9, fontweight="bold")
    ax.set_xlabel(xlabel, fontsize=8)
    ax.set_ylabel(ylabel, fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(True, color="#e3e6eb", linewidth=0.6)
    ax.ticklabel_format(useOffset=False, axis="y")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


def render_charts(t: dict, w: dict | None) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    if t.get("points"):
        xs = [p["x"] for p in t["points"]]
        xl = t.get("x_label") or ("Vin (V)" if t["test"] == "line_regulation" else "Load current (A)")
        out.append(("Output voltage", _plot("Output voltage", xl, "Vout mean (V)", xs, [p.get("vout_mean_v") for p in t["points"]], markers=True)))
        out.append(("Efficiency", _plot("Efficiency", xl, "Efficiency (%)", xs, [p.get("efficiency_pct") for p in t["points"]], markers=True)))
        rip = [None if p.get("ripple_pp_v") is None else p["ripple_pp_v"] * 1000 for p in t["points"]]
        out.append(("Output ripple", _plot("Output ripple", xl, "Ripple pk-pk (mV)", xs, rip, markers=True)))
    if w:
        x = w["time_us"]
        ev = w.get("events_us")
        s = w["series"]
        out.append(("Output voltage", _plot("Output voltage (cycle-averaged)", "Time (us)", "Vout (V)", x, s["vout_v"], ev)))
        if t["test"] == "line_transient":
            out.append(("Input voltage", _plot("Input voltage", "Time (us)", "Vin (V)", x, s["vin_v"], ev)))
        else:
            out.append(("Load current", _plot("Load current", "Time (us)", "Iload (A)", x, s["iload_a"], ev)))
        out.append(("Input current", _plot("Input current (cycle-averaged)", "Time (us)", "Iin (A)", x, s["iin_a"], ev)))
        d = w.get("detail")
        if d:
            out.append(("Ripple", _plot("Output ripple (last cycles)", "Time (us)", "Vout (V)", d["time_us"], d["vout_v"])))
    return out


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
_PDF_SUBS = {"µ": "u", "≥": ">=", "≤": "<=", "Ω": "Ohm", "–": "-", "—": "-", "±": "+/-", "’": "'", "“": '"', "”": '"', "…": "..."}


def _latin(s: str) -> str:
    for a, b in _PDF_SUBS.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def render_pdf(c: dict) -> bytes:
    from fpdf import FPDF
    from fpdf.fonts import FontFace

    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_title(c["title"])
    pdf.set_creator(f"buck-sim {__version__}")
    pdf.add_page()
    head = FontFace(emphasis="BOLD", fill_color=(232, 237, 245))

    def h1(text: str) -> None:
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, _latin(text), new_x="LMARGIN", new_y="NEXT")

    def h2(text: str) -> None:
        if pdf.get_y() > 250:
            pdf.add_page()
        pdf.ln(3)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(31, 80, 160)
        pdf.cell(0, 8, _latin(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    def para(text: str, style: str = "", size: int = 9, color=(0, 0, 0)) -> None:
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)
        pdf.multi_cell(0, 5, _latin(text), new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    def table(header: tuple, rows: list[tuple], widths: tuple | None = None) -> None:
        pdf.set_font("Helvetica", "", 8)
        with pdf.table(col_widths=widths, headings_style=head, line_height=5, text_align="LEFT") as tbl:
            r = tbl.row()
            for hcell in header:
                r.cell(_latin(str(hcell)))
            for row in rows:
                r = tbl.row()
                for cell in row:
                    if cell == "PASS":
                        r.cell("PASS", style=FontFace(emphasis="BOLD", color=(26, 127, 55)))
                    elif cell == "FAIL":
                        r.cell("FAIL", style=FontFace(emphasis="BOLD", color=(207, 34, 46)))
                    else:
                        r.cell(_latin(str(cell)))

    h1(c["title"])
    if c["synthetic"]:
        para("SYNTHETIC DATA - produced by the mock engine, not by a SPICE simulator. Do not use for design decisions.", "B", 9, (190, 30, 40))
    table(("Item", "Value"), c["info"], (35, 115))
    if c["notes"]:
        h2("Notes")
        para(c["notes"])
    h2("Operating range")
    table(("Parameter", "Value", "Unit"), c["design"], (60, 50, 40))
    if c["mapping"]:
        h2("Circuit mapping")
        table(("Item", "Value"), c["mapping"], (50, 100))
    if c["tolerance"]:
        h2("Acceptance criteria (tolerance)")
        table(("Criterion", "Value", "Unit"), c["tolerance"], (70, 40, 40))
    para("Tests run: " + c["tests_selected"], "I", 8, (90, 90, 90))
    if c["warnings"]:
        h2("Warnings")
        for w in c["warnings"]:
            para("- " + w, size=8, color=(150, 100, 0))
    for t in c["tests"]:
        pdf.add_page()
        h2(f"{t['label']}  ({t['status']})")
        if t["error"]:
            para("Error: " + t["error"], "B", 9, (190, 30, 40))
        for w in t["warnings"]:
            para("- " + w, size=8, color=(150, 100, 0))
        if t["rows"]:
            table(("Metric", "Value", "Unit", "Limit", "Result"), t["rows"], (72, 25, 15, 45, 18))
        for _, png in t["charts"]:
            if pdf.get_y() > 215:
                pdf.add_page()
            pdf.image(io.BytesIO(png), w=170)
    if c["bom"]:
        pdf.add_page()
        h2("Bill of materials (reference)")
        rows = [(b["ref"], b["value"], b["tolerance"], b["mpn"], b["description"][:60]) for b in c["bom"]]
        table(("Ref", "Value", "Tol.", "Part number", "Description"), rows, (18, 22, 14, 40, 81))
    return bytes(pdf.output())


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #
def render_docx(c: dict) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    doc.styles["Normal"].font.size = Pt(10)
    doc.core_properties.title = c["title"]
    doc.core_properties.author = f"buck-sim {__version__}"

    def table(header: tuple, rows: list[tuple]) -> None:
        tbl = doc.add_table(rows=1, cols=len(header))
        tbl.style = "Table Grid"
        for i, hcell in enumerate(header):
            cell = tbl.rows[0].cells[i]
            cell.text = str(hcell)
            for run in cell.paragraphs[0].runs:
                run.bold = True
        for row in rows:
            cells = tbl.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = str(v)
                if v in ("PASS", "FAIL"):
                    run = cells[i].paragraphs[0].runs[0]
                    run.bold = True
                    run.font.color.rgb = RGBColor(0x1A, 0x7F, 0x37) if v == "PASS" else RGBColor(0xCF, 0x22, 0x2E)
        doc.add_paragraph()

    doc.add_heading(c["title"], 0)
    if c["synthetic"]:
        p = doc.add_paragraph()
        r = p.add_run("SYNTHETIC DATA - produced by the mock engine, not by a SPICE simulator. Do not use for design decisions.")
        r.bold = True
        r.font.color.rgb = RGBColor(0xCF, 0x22, 0x2E)
    table(("Item", "Value"), c["info"])
    if c["notes"]:
        doc.add_heading("Notes", 1)
        doc.add_paragraph(c["notes"])
    doc.add_heading("Operating range", 1)
    table(("Parameter", "Value", "Unit"), c["design"])
    if c["mapping"]:
        doc.add_heading("Circuit mapping", 1)
        table(("Item", "Value"), c["mapping"])
    if c["tolerance"]:
        doc.add_heading("Acceptance criteria (tolerance)", 1)
        table(("Criterion", "Value", "Unit"), c["tolerance"])
    p = doc.add_paragraph("Tests run: " + c["tests_selected"])
    p.runs[0].italic = True
    if c["warnings"]:
        doc.add_heading("Warnings", 1)
        for w in c["warnings"]:
            doc.add_paragraph(w, style="List Bullet")
    for t in c["tests"]:
        doc.add_page_break()
        doc.add_heading(f"{t['label']} ({t['status']})", 1)
        if t["error"]:
            r = doc.add_paragraph().add_run("Error: " + t["error"])
            r.bold = True
            r.font.color.rgb = RGBColor(0xCF, 0x22, 0x2E)
        for w in t["warnings"]:
            doc.add_paragraph(w, style="List Bullet")
        if t["rows"]:
            table(("Metric", "Value", "Unit", "Limit", "Result"), t["rows"])
        for _, png in t["charts"]:
            doc.add_picture(io.BytesIO(png), width=Inches(6.3))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
    if c["bom"]:
        doc.add_page_break()
        doc.add_heading("Bill of materials (reference)", 1)
        table(
            ("Ref", "Value", "Tol.", "Part number", "Description"),
            [(b["ref"], b["value"], b["tolerance"], b["mpn"], b["description"]) for b in c["bom"]],
        )
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def load_waveforms(job_dir: Path, results: dict) -> dict[str, dict]:
    out = {}
    for t in results["tests"]:
        if t.get("waveform"):
            p = job_dir / "waveforms" / t["waveform"]
            if p.exists():
                out[t["test"]] = json.loads(p.read_text(encoding="utf-8"))
    return out

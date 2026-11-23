"""Bill of materials (BOM) parsing and cross-checking against the netlist.

The BOM is optional reference information: it is shown next to the netlist,
compared with the netlist values, included in the report, and (if AI help is
enabled) given to the model as context. It never changes the simulation.

Accepted: CSV / TSV / semicolon-separated text with a header row, as exported by
KiCad, Altium, OrCAD, Excel ("Save as CSV"), etc.
"""

from __future__ import annotations

import csv
import io
import re

from .netlist import decode_bytes

BOM_EXTS = {".csv", ".tsv", ".txt"}
MAX_ROWS = 2000

_COLUMNS = {
    "ref": ("designator", "designators", "refdes", "ref des", "reference", "references", "ref", "part reference", "parts", "part"),
    "value": ("value", "val", "comment", "nominal value"),
    "mpn": ("mpn", "manufacturer part number", "manufacturer_part_number", "mfr part", "mfr part number",
            "part number", "partnumber", "pn", "manufacturer pn", "mfg part"),
    "manufacturer": ("manufacturer", "mfr", "mfg", "vendor", "manufacturer name"),
    "description": ("description", "desc", "part description"),
    "tolerance": ("tolerance", "tol", "tol."),
    "package": ("package", "footprint", "case", "case/package", "pcb footprint"),
    "qty": ("qty", "quantity", "count", "qnty"),
}  # fmt: skip

_SUFFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "g": 1e9, "t": 1e12}
_NUM_RE = re.compile(r"^\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(meg|[fpnumkgtrR])?", re.IGNORECASE)
_RKM_RE = re.compile(r"^\s*(\d+)([rRkKmMuUnNpP])(\d+)\s*\w*$")  # 4k7, 2R2, 4u7


class BomError(ValueError):
    """The BOM file cannot be read (message is safe to show)."""


def parse_value(text: str, kind: str = "") -> float | None:
    """Parse '10uF', '4k7', '2.7n', '1Meg', '20mΩ' -> float. None when not numeric."""
    if not text:
        return None
    t = text.strip().replace("µ", "u").replace("μ", "u").replace("Ω", "").replace("ohm", "").replace(",", ".")
    m = _RKM_RE.match(t)
    if m:
        whole, mult, frac = m.groups()
        scale = 1.0 if mult.lower() == "r" else _SUFFIX.get(mult.lower(), 1.0)
        if mult == "M" and kind == "R":
            scale = 1e6
        return float(f"{whole}.{frac}") * scale
    m = _NUM_RE.match(t)
    if not m:
        return None
    num, suf = m.group(1), m.group(2)
    v = float(num)
    if not suf or suf.lower() == "r":
        return v
    if suf.lower() == "meg" or (suf == "M" and kind == "R"):
        return v * 1e6
    return v * _SUFFIX[suf.lower()]


def _expand_refs(text: str) -> list[str]:
    """'R1, R2 R5-R7' -> [R1, R2, R5, R6, R7]."""
    out: list[str] = []
    for tok in re.split(r"[,;\s]+", text.strip()):
        if not tok:
            continue
        m = re.match(r"^([A-Za-z_]+)(\d+)\s*-\s*(?:[A-Za-z_]+)?(\d+)$", tok)
        if m and int(m.group(3)) - int(m.group(2)) <= 200:
            pre, a, b = m.group(1), int(m.group(2)), int(m.group(3))
            out.extend(f"{pre}{i}" for i in range(min(a, b), max(a, b) + 1))
        else:
            out.append(tok)
    return out


def parse_bom(data: bytes) -> list[dict]:
    text = decode_bytes(data)
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if not lines:
        raise BomError("the BOM file is empty")
    sample = "\n".join(lines[:20])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO("\n".join(lines)), dialect))
    # header: first row (within the first 10) that names a designator column
    header_idx = None
    colmap: dict[str, int] = {}
    for i, row in enumerate(rows[:10]):
        norm = [c.strip().strip('"').lower() for c in row]
        found = {}
        for key, names in _COLUMNS.items():
            for j, c in enumerate(norm):
                if c in names and key not in found:
                    found[key] = j
        if "ref" in found:
            header_idx, colmap = i, found
            break
    if header_idx is None:
        raise BomError(
            "could not find a header row with a designator column "
            "(e.g. 'Designator', 'Reference', 'RefDes'). Export the BOM as CSV with a header row."
        )
    items: list[dict] = []
    for row in rows[header_idx + 1 :]:
        if len(items) >= MAX_ROWS:
            break

        def col(key: str) -> str:
            j = colmap.get(key)
            return row[j].strip() if j is not None and j < len(row) else ""

        refs = _expand_refs(col("ref"))
        if not refs:
            continue
        for ref in refs:
            items.append(
                {
                    "ref": ref,
                    "value": col("value"),
                    "mpn": col("mpn"),
                    "manufacturer": col("manufacturer"),
                    "description": col("description")[:120],
                    "tolerance": col("tolerance"),
                    "package": col("package"),
                }
            )
    if not items:
        raise BomError("the BOM has a header row but no parts")
    return items


def _netlist_value(elem: dict) -> float | None:
    if elem["kind"] not in "RCL" or not elem["value"]:
        return None
    return parse_value(elem["value"].split()[0], elem["kind"])


def cross_check(items: list[dict], elements: list[dict]) -> dict:
    """Compare BOM entries with netlist elements by reference designator."""
    by_ref = {e["name"].lower(): e for e in elements}
    bom_refs = {i["ref"].lower() for i in items}
    matched, mismatched, only_bom = [], [], []
    for it in items:
        e = by_ref.get(it["ref"].lower())
        if e is None:
            only_bom.append(it["ref"])
            continue
        nv = _netlist_value(e)
        bv = parse_value(it["value"], e["kind"]) if it["value"] else None
        entry = {"ref": it["ref"], "bom_value": it["value"], "netlist_value": e["value"]}
        if nv is not None and bv is not None and nv != 0 and abs(bv - nv) / abs(nv) > 0.01:
            mismatched.append(entry)
        else:
            matched.append(entry)
    only_netlist = [
        e["name"] for e in elements if e["name"].lower() not in bom_refs and e["kind"] in "RCLDMQXSJ"
    ]
    return {
        "items": items,
        "matched": len(matched),
        "mismatched": mismatched,
        "only_in_bom": only_bom[:50],
        "only_in_netlist": only_netlist[:50],
    }

"""Heuristic description of a buck converter netlist, used to draw the overview
picture in the UI and to explain which parts use vendor models.

Nothing here changes how a job is simulated; it only helps the user check the
mapping. Every guess is labelled as a guess.
"""

from __future__ import annotations

import re

from .netlist import _element_nodes, find_subckts, top_level_elements

KIND_LABELS = {
    "R": "Resistor", "C": "Capacitor", "L": "Inductor", "V": "Voltage source",
    "I": "Current source", "D": "Diode", "M": "MOSFET", "Q": "BJT", "J": "JFET",
    "S": "Voltage-controlled switch", "W": "Current-controlled switch", "X": "Subcircuit / IC",
    "B": "Behavioural source", "E": "VCVS", "G": "VCCS", "F": "CCCS", "H": "CCVS", "K": "Coupling",
}  # fmt: skip
PASSIVE = set("RCLK")
SWITCHING = set("SMQDJWX")
GND_NAMES = {"0", "gnd", "agnd", "pgnd", "vss"}

_MODEL_RE = re.compile(r"^\s*\.model\s+(\S+)\s+(\w+)", re.IGNORECASE)


def _is_gnd(node: str) -> bool:
    return node.lower() in GND_NAMES


def _model_of(toks: list[str]) -> str | None:
    """Name of the model / subcircuit an element refers to (None for plain R/L/C/V/I)."""
    kind = toks[0][0].upper()
    if kind == "X":
        body = [t for t in toks[1:] if "=" not in t and not t.lower().startswith("params:")]
        return body[-1] if body else None
    if kind == "D" and len(toks) >= 4:
        return toks[3]
    if kind in "MS" and len(toks) >= 6:
        return toks[5]
    if kind == "Q" and len(toks) >= 5:
        return toks[4]
    if kind == "J" and len(toks) >= 5:
        return toks[4]
    if kind == "W" and len(toks) >= 5:
        return toks[4]
    return None


def _value_of(toks: list[str]) -> str:
    kind = toks[0][0].upper()
    if kind in "RCLVI" and len(toks) >= 4:
        return " ".join(toks[3:])[:60]
    return ""


def _subckt_elements(stmts: list[str], sc) -> list[tuple[int, list[str]]]:
    """Elements directly inside one .subckt block (nested definitions skipped)."""
    out, depth = [], 0
    for i in range(sc.start + 1, sc.end):
        head = stmts[i].split(None, 1)[0].lower()
        if head == ".subckt":
            depth += 1
        elif head == ".ends":
            depth -= 1
        elif depth == 0 and not stmts[i].startswith("."):
            out.append((i, stmts[i].split()))
    return out


def _pick_converter_subckt(subckts: dict, want: str | None):
    if want and want.lower() in subckts:
        return subckts[want.lower()]
    # the one whose pins look like a converter (VIN/VOUT), else the biggest
    for sc in subckts.values():
        pins = {p.lower() for p in sc.pins}
        if pins & {"vin", "in", "vcc"} and pins & {"vout", "out", "vo"}:
            return sc
    return max(subckts.values(), key=lambda sc: sc.end - sc.start, default=None)


def describe(stmts: list[str], subckt: str | None = None) -> dict:
    """Describe the top-level circuit, or (subcircuit mode / no top-level parts)
    the body of the converter subcircuit."""
    subckts = find_subckts(stmts)
    scope = "top"
    source_elements = top_level_elements(stmts)
    if subckt or not any(t[0][0].upper() in "RLC" for _, t in source_elements):
        sc = _pick_converter_subckt(subckts, subckt)
        if sc is not None:
            source_elements = _subckt_elements(stmts, sc)
            scope = sc.name
    model_defs: dict[str, str] = {}
    for s in stmts:
        m = _MODEL_RE.match(s)
        if m:
            model_defs[m.group(1).lower()] = m.group(2).upper()

    elements: list[dict] = []
    net_members: dict[str, list[str]] = {}
    for _, toks in source_elements:
        name = toks[0]
        kind = name[0].upper()
        nodes = _element_nodes(toks)
        model = _model_of(toks)
        if model is None:
            source = "builtin"  # plain R/L/C/V/I: ideal SPICE element
        elif model.lower() in subckts or model.lower() in model_defs:
            source = "defined"  # .subckt / .model found in the netlist or uploaded files
        else:
            source = "missing"  # not found: LTspice may supply it from its own library
        elements.append(
            {
                "name": name, "kind": kind, "kind_label": KIND_LABELS.get(kind, kind),
                "nodes": nodes, "value": _value_of(toks), "model": model, "model_source": source,
            }
        )  # fmt: skip
        for n in nodes:
            net_members.setdefault(n, []).append(name)

    by_name = {e["name"].lower(): e for e in elements}

    def members(node: str, kinds: str | None = None) -> list[dict]:
        out = [by_name[m.lower()] for m in net_members.get(node, [])]
        return [e for e in out if kinds is None or e["kind"] in kinds]

    roles: dict[str, str] = {}
    gnd = next((n for n in net_members if _is_gnd(n)), "0")
    roles[gnd] = "gnd"

    # input: positive node of the first voltage source to ground (or the VIN pin)
    vin_src = next((e for e in elements if e["kind"] == "V" and len(e["nodes"]) == 2 and _is_gnd(e["nodes"][1])), None)
    vin = vin_src["nodes"][0] if vin_src else None
    if vin is None and scope != "top":
        vin = next((n for n in net_members if n.lower() in ("vin", "in", "vcc", "vbat")), None)
    if vin:
        roles[vin] = "vin"

    # switch node: an inductor terminal that also touches a switch, diode or IC
    sw = vout = None
    inductor = None
    for e in elements:
        if e["kind"] != "L" or len(e["nodes"]) != 2:
            continue
        a, b = e["nodes"]
        a_sw = any(m["kind"] in SWITCHING for m in members(a))
        b_sw = any(m["kind"] in SWITCHING for m in members(b))
        if a_sw != b_sw:
            inductor = e
            sw, out = (a, b) if a_sw else (b, a)
            # step over a series DCR resistor (L -> R -> out with capacitor)
            if not members(out, "C"):
                rs = [r for r in members(out, "R") if len(r["nodes"]) == 2]
                for r in rs:
                    other = r["nodes"][1] if r["nodes"][0] == out else r["nodes"][0]
                    if members(other, "C"):
                        out = other
                        break
            vout = out
            break
    names_lower = {n.lower(): n for n in net_members}
    if vout is None:
        for pref in ("vout", "out", "output"):
            if pref in names_lower:
                vout = names_lower[pref]
                break
    if sw:
        roles[sw] = "sw"
    if vout:
        roles[vout] = "vout"

    # feedback: node between two resistors, one to vout and one to ground
    fb = None
    if vout:
        for r in members(vout, "R"):
            other = r["nodes"][1] if r["nodes"][0] == vout else r["nodes"][0]
            if other == vout or _is_gnd(other):
                continue
            if any(_is_gnd(x) for r2 in members(other, "R") for x in r2["nodes"] if r2 is not r):
                fb = other
                break
    if fb:
        roles[fb] = "fb"

    # load candidate: element between vout and ground
    load = None
    if vout:
        cands = [e for e in members(vout, "RIB") if len(e["nodes"]) >= 2 and any(_is_gnd(n) for n in e["nodes"][:2])]
        cands.sort(key=lambda e: (0 if "load" in e["name"].lower() else 1, e["name"]))
        load = cands[0]["name"] if cands else None

    ics = []
    for e in elements:
        if e["kind"] != "X":
            continue
        sc = subckts.get((e["model"] or "").lower())
        pins = sc.pins if sc else [f"pin{i + 1}" for i in range(len(e["nodes"]))]
        ics.append(
            {
                "name": e["name"],
                "subckt": e["model"],
                "defined": sc is not None,
                "pins": [
                    {
                        "pin": p,
                        "net": n,
                        "role": roles.get(n, ""),
                        "attached": [m for m in net_members.get(n, []) if m.lower() != e["name"].lower()][:8],
                    }
                    for p, n in zip(pins, e["nodes"])
                ],
            }
        )

    counts: dict[str, int] = {}
    for e in elements:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    modelled = [e for e in elements if e["model"]]
    return {
        "scope": scope,
        "elements": elements,
        "nets": {n: {"members": m, "role": roles.get(n, "")} for n, m in sorted(net_members.items(), key=lambda kv: kv[0].lower())},
        "roles": {"vin": vin, "sw": sw, "vout": vout, "fb": fb, "gnd": gnd},
        "guess": {
            "vin_source": vin_src["name"] if vin_src else None,
            "vout_node": vout,
            "load_element": load,
            "inductor": inductor["name"] if inductor else None,
        },
        "ics": ics,
        "counts": counts,
        "models": {
            "vendor": [
                {"element": e["name"], "model": e["model"], "kind": e["kind_label"], "source": e["model_source"]}
                for e in modelled
            ],
            "passives_builtin": sum(1 for e in elements if e["kind"] in PASSIVE),
        },
    }

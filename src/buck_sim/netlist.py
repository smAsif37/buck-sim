"""Netlist reading, sanitising and light-weight analysis.

Uploaded netlists and models are UNTRUSTED. This module:

* decodes the bytes (UTF-8 / UTF-16 / cp1252, LTspice's "µ"),
* joins continuation lines and strips comments,
* resolves ``.include`` / ``.lib`` against the files uploaded with the job (by
  base name only; the file system is never consulted) and inlines them,
* allow-lists directives (anything not on the list is rejected),
* drops analysis / output directives (we add our own),
* refuses encrypted models and ``file=`` references.

The result is a flat list of statements that the testbench generator can use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_INCLUDE_DEPTH = 4
MAX_STATEMENTS = 200_000


class NetlistError(ValueError):
    """The netlist cannot be used (message is safe to show to the user)."""


class UnsafeNetlistError(NetlistError):
    """The netlist uses a construct we refuse to run."""


class UnresolvedIncludeError(NetlistError):
    """An .include/.lib refers to a file that was not uploaded."""


# Directives we keep as-is.
ALLOWED_DIRECTIVES = frozenset(
    {
        ".subckt", ".ends", ".model", ".param", ".params", ".func", ".global", ".ic",
        ".nodeset", ".options", ".option", ".temp", ".machine", ".endmachine",
    }
)  # fmt: skip
# Directives that define analyses / outputs: dropped (we generate our own).
DROPPED_DIRECTIVES = frozenset(
    {
        ".tran", ".ac", ".dc", ".op", ".noise", ".tf", ".four", ".step", ".meas", ".measure",
        ".save", ".probe", ".plot", ".print", ".width", ".backanno", ".savebias", ".loadbias",
        ".wave", ".end", ".endl", ".title",
    }
)  # fmt: skip

_FILE_REF_RE = re.compile(r"\bfile\s*=", re.IGNORECASE)
_ENCRYPTED_MARKERS = ("$CDNENCSTART", "$CDNENCFINISH", "$ENCRYPTED")
# raw-text scan for names a file declares, used when the file itself can't be parsed
_DECLARED_NAME_RE = re.compile(r"^[ \t]*\.(?:subckt|model)[ \t]+(\S+)", re.IGNORECASE | re.MULTILINE)


# --------------------------------------------------------------------------- #
# Decoding / tokenising
# --------------------------------------------------------------------------- #
def decode_bytes(data: bytes) -> str:
    """Decode an uploaded netlist/model into normalised text."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16", errors="replace")
    elif b"\x00" in data[:400]:
        text = data.decode("utf-16-le", errors="replace")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    return text.replace("µ", "u").replace("μ", "u")


def basename_of(ref: str) -> str:
    """Last path component of a (possibly Windows) path, without quotes."""
    ref = ref.strip().strip('"').strip("'")
    return re.split(r"[\\/]", ref)[-1].strip()


def _strip_inline_comment(line: str) -> str:
    # ';' starts a comment anywhere in SPICE-like dialects.
    idx = line.find(";")
    return line[:idx] if idx >= 0 else line


def logical_lines(text: str, *, first_line_is_title: bool) -> list[str]:
    """Join continuation lines, drop comments / blanks / the title line."""
    raw = text.split("\n")
    first_idx = next((i for i, ln in enumerate(raw) if ln.strip()), -1)
    out: list[str] = []
    for i, line in enumerate(raw):
        if i == first_idx and first_line_is_title and not line.lstrip().startswith("."):
            continue  # SPICE treats the first line as a title/comment
        stripped = line.strip()
        if not stripped or stripped.startswith("*"):
            continue
        stripped = _strip_inline_comment(stripped).strip()
        if not stripped:
            continue
        if stripped.startswith("+"):
            if not out:
                raise NetlistError("continuation line ('+') without a preceding line")
            out[-1] = out[-1] + " " + stripped[1:].strip()
        else:
            out.append(stripped)
    return [" ".join(s.split()) for s in out]


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
@dataclass
class ParsedNetlist:
    statements: list[str]
    warnings: list[str] = field(default_factory=list)


class NetlistLoader:
    """Resolve and sanitise a main netlist plus its uploaded model files."""

    def __init__(
        self,
        files: dict[str, bytes],
        *,
        builtin_libs: frozenset[str] = frozenset(),
        max_statements: int = MAX_STATEMENTS,
    ) -> None:
        # case-insensitive base-name lookup
        self._files: dict[str, tuple[str, str]] = {}
        for name, data in files.items():
            self._files[basename_of(name).lower()] = (basename_of(name), decode_bytes(data))
        self._builtin = frozenset(b.lower() for b in builtin_libs)
        self._max = max_statements
        self.warnings: list[str] = []

    # -- public ------------------------------------------------------------- #
    def load_main(self, name: str) -> ParsedNetlist:
        key = basename_of(name).lower()
        if key not in self._files:
            raise NetlistError(f"netlist file {name!r} was not uploaded")
        self.warnings = []
        self._used: set[str] = set()
        stmts = self._process_file(key, depth=0, stack=(), section=None, is_main=True)
        if not stmts:
            raise NetlistError("the netlist is empty after removing comments and analysis cards")
        stmts = self._auto_include(stmts, key)
        return ParsedNetlist(statements=stmts, warnings=list(dict.fromkeys(w for w in self.warnings if w)))

    def _auto_include(self, stmts: list[str], main_key: str) -> list[str]:
        """Add uploaded model files the netlist never ``.include``s but whose models it uses.

        LTspice finds models in its own library folders, so exported netlists often
        have no ``.include`` line for them. Only files that define a missing model or
        subcircuit are added, each once, and the user is told.
        """
        for _ in range(MAX_INCLUDE_DEPTH):  # a pulled-in subckt can need another file
            missing = missing_models(stmts)
            if not missing:
                break
            added = False
            for fkey, (display, _) in sorted(self._files.items()):
                if fkey == main_key or fkey in self._used:
                    continue
                used_before, warn_before = set(self._used), len(self.warnings)
                try:
                    body = self._process_file(fkey, depth=1, stack=(main_key,), section=None, is_main=False)
                    provides = sorted(missing[n] for n in defined_models(body) if n in missing)
                except NetlistError:
                    # Not usable (e.g. encrypted). If it declares a model the circuit needs,
                    # that error is the real cause, so report it instead of "not defined".
                    text = self._files[fkey][1]
                    declared = {m.group(1).lower() for m in _DECLARED_NAME_RE.finditer(text)}
                    if declared & set(missing):
                        raise
                    provides = []
                if not provides:
                    self._used, self.warnings[warn_before:] = used_before, []
                    continue
                stmts = stmts + body
                added = True
                self.warnings.append(
                    f"{display!r} is not .include'd by the netlist; it was added automatically because it "
                    f"defines {', '.join(provides[:6])}{' …' if len(provides) > 6 else ''}"
                )
                missing = missing_models(stmts)
                if not missing:
                    break
            if not added:
                break
        return stmts

    # -- internals ---------------------------------------------------------- #
    def _process_file(
        self,
        key: str,
        *,
        depth: int,
        stack: tuple[str, ...],
        section: str | None,
        is_main: bool,
    ) -> list[str]:
        if depth > MAX_INCLUDE_DEPTH:
            raise NetlistError(f"includes are nested deeper than {MAX_INCLUDE_DEPTH} levels")
        if key in stack:
            raise NetlistError(f"circular include of {self._files[key][0]!r}")
        display, text = self._files[key]
        if hasattr(self, "_used"):
            self._used.add(key)
        for marker in _ENCRYPTED_MARKERS:
            if marker in text:
                raise NetlistError(
                    f"{display!r} is an encrypted (PSpice) model and cannot be used by LTspice/ngspice; "
                    "ask the vendor for an LTspice/SPICE-compatible model"
                )
        lines = logical_lines(text, first_line_is_title=is_main)
        lines = self._select_section(lines, section, display)
        out: list[str] = []
        for line in lines:
            out.extend(self._process_line(line, depth, stack + (key,), display))
            if len(out) > self._max:
                raise NetlistError("netlist is too large")
        return out

    @staticmethod
    def _select_section(lines: list[str], section: str | None, display: str) -> list[str]:
        """Handle ``.lib NAME`` ... ``.endl`` sections of library files.

        With ``section`` given, return only that section's body. Otherwise return
        everything outside of sections.
        """
        starts: list[tuple[str, int]] = []
        spans: list[tuple[str, int, int]] = []  # (name_lower, first_body_idx, endl_idx)
        for i, line in enumerate(lines):
            toks = line.split()
            head = toks[0].lower()
            if head == ".lib" and len(toks) == 2:
                starts.append((toks[1].lower(), i))
            elif head == ".endl" and starts:
                name, s = starts.pop()
                spans.append((name, s + 1, i))
        # Unmatched ".lib X" lines remain in `starts`: they are references, not sections.
        unmatched = {i for _, i in starts}
        if section is not None:
            wanted = [sp for sp in spans if sp[0] == section.lower()]
            if not wanted:
                raise NetlistError(f"section {section!r} not found in {display!r}")
            _, a, b = wanted[0]
            return [ln for ln in lines[a:b]]
        inside: set[int] = set()
        for _, a, b in spans:
            inside.update(range(a - 1, b + 1))  # include the .lib and .endl marker lines
        return [ln for i, ln in enumerate(lines) if i not in inside or i in unmatched]

    def _process_line(self, line: str, depth: int, stack: tuple[str, ...], display: str) -> list[str]:
        if _FILE_REF_RE.search(line):
            raise UnsafeNetlistError(f"'file=' references are not allowed (in {display!r}): {line[:80]}")
        if not line.startswith("."):
            return [line]
        toks = line.split()
        head = toks[0].lower()

        if head in (".include", ".inc"):
            if len(toks) < 2:
                raise NetlistError(f".include without a file name in {display!r}")
            return self._inline(" ".join(toks[1:]), None, depth, stack)
        if head == ".lib":
            if len(toks) == 2:
                return self._lib_reference(toks[1], None, depth, stack)
            if len(toks) == 3:
                return self._lib_reference(toks[1], toks[2], depth, stack)
            raise NetlistError(f"unsupported .lib statement in {display!r}: {line[:80]}")
        if head in ALLOWED_DIRECTIVES:
            return [line]
        if head in DROPPED_DIRECTIVES:
            if head not in (".end", ".endl", ".title"):
                self.warnings.append(f"ignored {head} in {display!r} (the analysis is generated automatically)")
            return []
        raise UnsafeNetlistError(f"directive {toks[0]!r} is not allowed (in {display!r})")

    def _lib_reference(self, ref: str, section: str | None, depth: int, stack: tuple[str, ...]) -> list[str]:
        base = basename_of(ref).lower()
        if base in self._files:
            return self._inline(ref, section, depth, stack)
        if base in self._builtin:
            return [f".lib {basename_of(ref)}"]
        raise UnresolvedIncludeError(
            f".lib {ref!r} refers to a file that was not uploaded. Upload it together with the netlist."
        )

    def _inline(self, ref: str, section: str | None, depth: int, stack: tuple[str, ...]) -> list[str]:
        base = basename_of(ref).lower()
        if base not in self._files:
            if base in self._builtin:
                return [f".lib {basename_of(ref)}"]
            have = sorted(v[0] for v in self._files.values())
            raise UnresolvedIncludeError(
                f"{ref!r} is included by the netlist but was not uploaded. Uploaded files: {have}"
            )
        return self._process_file(base, depth=depth + 1, stack=stack, section=section, is_main=False)


# --------------------------------------------------------------------------- #
# Analysis of parsed statements
# --------------------------------------------------------------------------- #
@dataclass
class Subckt:
    name: str
    pins: list[str]
    start: int
    end: int  # index of the .ends statement (or len(stmts) if missing)


def _head(stmt: str) -> str:
    return stmt.split(None, 1)[0].lower()


def find_subckts(stmts: list[str]) -> dict[str, Subckt]:
    """Return subcircuit definitions keyed by lower-case name."""
    found: dict[str, Subckt] = {}
    open_stack: list[Subckt] = []
    for i, stmt in enumerate(stmts):
        h = _head(stmt)
        if h == ".subckt":
            toks = stmt.split()
            if len(toks) < 2:
                raise NetlistError(".subckt without a name")
            pins: list[str] = []
            for tok in toks[2:]:
                if "=" in tok or tok.lower().startswith("params:"):
                    break
                pins.append(tok)
            sc = Subckt(name=toks[1], pins=pins, start=i, end=len(stmts))
            open_stack.append(sc)
        elif h == ".ends":
            if not open_stack:
                raise NetlistError(".ends without matching .subckt")
            sc = open_stack.pop()
            sc.end = i
            found[sc.name.lower()] = sc
    if open_stack:
        raise NetlistError(f".subckt {open_stack[-1].name!r} is never closed with .ends")
    return found


def top_level_indices(stmts: list[str]) -> list[int]:
    """Indices of statements that are outside every .subckt block."""
    depth = 0
    out: list[int] = []
    for i, stmt in enumerate(stmts):
        h = _head(stmt)
        if h == ".subckt":
            depth += 1
            continue
        if h == ".ends":
            depth -= 1
            continue
        if depth == 0:
            out.append(i)
    return out


def top_level_elements(stmts: list[str]) -> list[tuple[int, list[str]]]:
    """(index, tokens) for each element line outside subcircuits."""
    return [(i, stmts[i].split()) for i in top_level_indices(stmts) if not stmts[i].startswith(".")]


def find_element(stmts: list[str], name: str) -> tuple[int, list[str]]:
    for i, toks in top_level_elements(stmts):
        if toks[0].lower() == name.lower():
            return i, toks
    raise NetlistError(f"element {name!r} was not found at the top level of the netlist")


_TWO_NODE = set("RCLVIDBFH")
_FOUR_NODE = set("EGSM")


def _element_nodes(toks: list[str]) -> list[str]:
    kind = toks[0][0].upper()
    if kind in _TWO_NODE:
        return toks[1:3]
    if kind in _FOUR_NODE:
        return toks[1:5]
    if kind in "JQ":
        return toks[1:4]
    if kind == "X":
        body = [t for t in toks[1:] if "=" not in t and not t.lower().startswith("params:")]
        return body[:-1]  # last token is the subcircuit name
    return []


_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?[a-zA-Z]*$")
_MODEL_DEF_RE = re.compile(r"^\.model\s+(\S+)", re.IGNORECASE)


def _is_param(tok: str) -> bool:
    t = tok.lower()
    return "=" in t or t in ("params:", "on", "off") or bool(_NUMBER_RE.match(t))


def element_model(toks: list[str], defined: set[str] | frozenset[str] = frozenset()) -> str | None:
    """Name of the model / subcircuit an element refers to (None for plain R/L/C/V/I...).

    Node counts vary (MOSFET with 3 or 4 nodes, BJT with or without substrate), so
    when ``defined`` (lower-case names) is given, a candidate that is defined wins.
    """
    kind = toks[0][0].upper()
    if kind == "X":
        body = []
        for t in toks[1:]:
            if t.lower() == "params:" or "=" in t:
                break
            body.append(t)
        return body[-1] if len(body) >= 1 else None
    positions = {"D": (3,), "M": (5, 4), "Q": (5, 4), "J": (4,), "S": (5,), "W": (4,), "Z": (4,)}.get(kind)
    if positions is None:
        return None
    cands = [toks[i] for i in positions if i < len(toks) and not _is_param(toks[i])]
    for c in cands:
        if c.lower() in defined:
            return c
    return cands[0] if cands else None


def defined_models(stmts: list[str]) -> set[str]:
    """Lower-case names of every .subckt and .model in the statements."""
    names = set(find_subckts(stmts))
    for s in stmts:
        m = _MODEL_DEF_RE.match(s)
        if m:
            names.add(m.group(1).lower())
    return names


def missing_models(stmts: list[str]) -> dict[str, str]:
    """Models/subcircuits referenced by elements but defined nowhere: lower name -> name."""
    defined = defined_models(stmts)
    out: dict[str, str] = {}
    for s in stmts:
        if s.startswith("."):
            continue
        toks = s.split()
        m = element_model(toks, defined)
        if m and m.lower() not in defined:
            out.setdefault(m.lower(), m)
    return out


def inspect(stmts: list[str]) -> dict:
    """Summary used by the UI to pre-fill the port mapping."""
    subckts = [{"name": s.name, "pins": s.pins} for s in find_subckts(stmts).values()]
    sources: list[dict] = []
    loads: list[dict] = []
    nodes: dict[str, str] = {}
    for _, toks in top_level_elements(stmts):
        kind = toks[0][0].upper()
        for n in _element_nodes(toks):
            nodes.setdefault(n.lower(), n)
        if kind == "V" and len(toks) >= 3:
            sources.append({"name": toks[0], "nplus": toks[1], "nminus": toks[2], "value": " ".join(toks[3:])[:60]})
        elif kind in "RIB" and len(toks) >= 3:
            loads.append({"name": toks[0], "nodes": toks[1:3], "value": " ".join(toks[3:])[:60]})
    return {
        "subckts": subckts,
        "voltage_sources": sources,
        "load_candidates": loads,
        "nodes": sorted(nodes.values(), key=str.lower),
    }

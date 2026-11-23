"""Optional AI explanation of an uploaded circuit (Claude API).

Disabled unless the server has Anthropic credentials (``ANTHROPIC_API_KEY``) and
``AI_ASSIST`` is not ``0``. Only runs when the user presses "Explain with AI":
the netlist text, BOM rows and our own topology guess are then sent to the
Anthropic API. The answer is advisory; it never changes a job by itself.
"""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
MAX_NETLIST_CHARS = 120_000

SYSTEM_PROMPT = """You are a power-electronics engineer reviewing a SPICE netlist of a DC-DC buck \
converter for a simulation tool. The tool replaces the input source and the load with its own \
stimulus and runs steady-state, transient and regulation tests.

Explain the circuit for an engineer who is about to map it: topology, the controller IC and what \
each of its pins does, the power stage, the output filter and the feedback network. Then suggest \
the mapping the tool needs and point out anything that would make the simulation fail or \
mislead (missing models, ideal parts that hide losses, values that disagree with the BOM, \
unusual nodes). Base every statement on the netlist and BOM you are given; when you infer \
something, say that it is an inference. Keep each text field short and concrete."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "topology", "controller", "pins", "suggested_mapping", "design_estimates", "issues"],
    "properties": {
        "summary": {"type": "string", "description": "2-4 sentences: what this circuit is"},
        "topology": {"type": "string", "description": "power stage, output filter, feedback, compensation"},
        "controller": {
            "type": "object",
            "additionalProperties": False,
            "required": ["element", "model", "description"],
            "properties": {
                "element": {"type": "string"},
                "model": {"type": "string"},
                "description": {"type": "string"},
            },
        },
        "pins": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["pin", "net", "function"],
                "properties": {
                    "pin": {"type": "string"},
                    "net": {"type": "string"},
                    "function": {"type": "string"},
                },
            },
        },
        "suggested_mapping": {
            "type": "object",
            "additionalProperties": False,
            "required": ["vin_source", "vout_node", "load_element", "gnd_node"],
            "properties": {
                "vin_source": {"type": "string"},
                "vout_node": {"type": "string"},
                "load_element": {"type": "string"},
                "gnd_node": {"type": "string"},
            },
        },
        "design_estimates": {
            "type": "object",
            "additionalProperties": False,
            "required": ["vin_nom_v", "vout_nom_v", "iout_max_a", "fsw_khz"],
            "properties": {
                "vin_nom_v": {"type": ["number", "null"]},
                "vout_nom_v": {"type": ["number", "null"]},
                "iout_max_a": {"type": ["number", "null"]},
                "fsw_khz": {"type": ["number", "null"]},
            },
        },
        "issues": {"type": "array", "items": {"type": "string"}},
    },
}


class AiUnavailable(RuntimeError):
    """AI help is not configured or the request failed (message is safe to show)."""


def ai_enabled() -> bool:
    if os.environ.get("AI_ASSIST", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def build_prompt(netlist_text: str, topology: dict, bom_items: list[dict] | None) -> str:
    if len(netlist_text) > MAX_NETLIST_CHARS:
        raise AiUnavailable(
            f"the netlist is {len(netlist_text) // 1000} kB; AI help accepts up to {MAX_NETLIST_CHARS // 1000} kB"
        )
    guess = {k: topology.get(k) for k in ("scope", "roles", "guess", "ics", "counts")}
    parts = [
        "<netlist>\n" + netlist_text + "\n</netlist>",
        "<tool_topology_guess>\n" + json.dumps(guess, indent=1) + "\n</tool_topology_guess>",
    ]
    if bom_items:
        parts.append("<bom>\n" + json.dumps(bom_items[:400], indent=0) + "\n</bom>")
    parts.append("Explain this converter and suggest the mapping, following the output schema.")
    return "\n\n".join(parts)


def explain(netlist_text: str, topology: dict, bom_items: list[dict] | None = None, client=None) -> dict:
    """Ask Claude for an explanation. ``client`` can be injected for tests."""
    if client is None:
        if not ai_enabled():
            raise AiUnavailable("AI help is not configured on this server (set ANTHROPIC_API_KEY).")
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover - optional dependency
            raise AiUnavailable("install the optional dependency: pip install 'buck-sim[ai]'") from e
        client = anthropic.Anthropic(timeout=180.0)
    model = os.environ.get("AI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    prompt = build_prompt(netlist_text, topology, bom_items)
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            # if the model declines, retry on a fallback model chosen by the API
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except Exception as e:  # network / auth / rate limit: report, never crash the API
        log.warning("AI request failed: %s", e)
        raise AiUnavailable(f"AI request failed: {type(e).__name__}: {getattr(e, 'message', e)}") from e
    if response.stop_reason == "refusal":
        raise AiUnavailable("the AI model declined to answer for this circuit")
    if response.stop_reason == "max_tokens":
        raise AiUnavailable("the AI answer was cut off; try a smaller netlist")
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
    try:
        out = json.loads(text)
    except json.JSONDecodeError as e:
        raise AiUnavailable("the AI answer could not be read") from e
    out["model"] = getattr(response, "model", model)
    return out

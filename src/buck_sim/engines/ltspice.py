"""LTspice engine (batch mode).

LTspice runs natively on Windows and macOS. On Linux it needs Wine (and a virtual
display, e.g. ``xvfb-run``). Configure the command with ``LTSPICE_CMD``, e.g.::

    macOS  : /Applications/LTspice.app/Contents/MacOS/LTspice
    Windows: "C:\\Program Files\\ADI\\LTspice\\LTspice.exe"
    Linux  : xvfb-run -a wine /opt/ltspice/LTspice.exe

Command line used: ``<cmd> <args> tb.cir`` where ``args`` defaults to ``-b`` (macOS)
or ``-Run -b`` (Windows / Wine). Override with ``LTSPICE_ARGS`` if your version differs.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from ..testbench import CTL_NODE, Bundle, SignalNames
from .base import Engine, EngineError, Waveforms, run_process, split_command, tail
from .rawio import read_raw_traces, waveforms_from_traces


class LTspiceEngine(Engine):
    name = "ltspice"
    supports_builtin_libs = True

    def __init__(self, cmd: str, args: str | None = None) -> None:
        self.cmd = split_command(cmd)
        joined = " ".join(self.cmd).lower()
        self.use_wine = "wine" in joined
        is_windows_exe = joined.endswith(".exe") or os.name == "nt"
        if args:
            self.args = shlex.split(args)
        elif self.use_wine or is_windows_exe:
            self.args = ["-Run", "-b"]
        else:
            self.args = ["-b"]

    def extra_lines(self, signals: SignalNames) -> list[str]:
        return [
            ".options plotwinsize=0",
            f".save V({signals.vin_node}) V({signals.vout_node}) V({CTL_NODE}) I({signals.vin_source})",
        ]

    def version(self) -> str:
        return "LTspice (" + " ".join(self.cmd) + ")"

    def simulate(self, bundle: Bundle, workdir: Path, timeout_s: float) -> Waveforms:
        workdir.mkdir(parents=True, exist_ok=True)
        netlist = workdir / "tb.cir"
        raw = workdir / "tb.raw"
        log = workdir / "tb.log"
        for stale in (raw, log):
            stale.unlink(missing_ok=True)
        netlist.write_text(bundle.netlist, encoding="ascii", errors="replace")

        path_arg = str(netlist)
        if self.use_wine:
            path_arg = "Z:" + path_arg.replace("/", "\\")
        cmd = [*self.cmd, *self.args, path_arg]
        env = {"WINEDEBUG": "-all"} if self.use_wine else None
        code, out = run_process(cmd, cwd=workdir, timeout_s=timeout_s, env=env)

        log_text = log.read_text(errors="replace") if log.exists() else ""
        if not raw.exists() or raw.stat().st_size == 0:
            raise EngineError(
                f"LTspice produced no output (exit code {code})",
                tail(log_text or out),
            )
        time, traces = read_raw_traces(raw)
        wf = waveforms_from_traces(time, traces, bundle.signals, self.name)
        wf.log_tail = tail(log_text)
        return wf

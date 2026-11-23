"""ngspice engine (open source; recommended default for hosted deployments)."""

from __future__ import annotations

from pathlib import Path

from ..testbench import Bundle
from .base import Engine, EngineError, Waveforms, run_process, split_command, tail
from .rawio import read_raw_traces, waveforms_from_traces


class NgspiceEngine(Engine):
    name = "ngspice"
    supports_builtin_libs = False

    def __init__(self, cmd: str = "ngspice") -> None:
        self.cmd = split_command(cmd)
        self._version: str | None = None

    def version(self) -> str:
        if self._version is None:
            try:
                _, out = run_process([*self.cmd, "--version"], cwd=Path.cwd(), timeout_s=15)
                first = next((ln.strip() for ln in out.splitlines() if "ngspice" in ln.lower()), "")
                self._version = first or "ngspice"
            except EngineError:
                self._version = "ngspice (version unknown)"
        return self._version

    def simulate(self, bundle: Bundle, workdir: Path, timeout_s: float) -> Waveforms:
        workdir.mkdir(parents=True, exist_ok=True)
        netlist = workdir / "tb.cir"
        raw = workdir / "tb.raw"
        log = workdir / "tb.log"
        for stale in (raw, log):
            stale.unlink(missing_ok=True)
        netlist.write_text(bundle.netlist, encoding="ascii", errors="replace")

        cmd = [*self.cmd, "-b", "-r", str(raw), "-o", str(log), str(netlist)]
        code, out = run_process(cmd, cwd=workdir, timeout_s=timeout_s)
        log_text = log.read_text(errors="replace") if log.exists() else ""
        if not raw.exists() or raw.stat().st_size == 0:
            raise EngineError(f"ngspice produced no output (exit code {code})", tail(log_text or out))
        time, traces = read_raw_traces(raw)
        wf = waveforms_from_traces(time, traces, bundle.signals, self.name)
        wf.log_tail = tail(log_text or out)
        return wf

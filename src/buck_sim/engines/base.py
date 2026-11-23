"""Engine interface and shared subprocess helper."""

from __future__ import annotations

import contextlib
import os
import shlex
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..testbench import Bundle, SignalNames


class EngineError(RuntimeError):
    """Simulation failed. ``log_tail`` carries the end of the simulator log."""

    def __init__(self, message: str, log_tail: str = "") -> None:
        super().__init__(message)
        self.log_tail = log_tail


@dataclass
class Waveforms:
    """Normalised simulator output.

    ``signals`` always contains: ``vin`` (V), ``vout`` (V), ``ctl`` (load set-point, A)
    and ``iin`` (A, SPICE sign: negative when the source delivers power).
    """

    time: np.ndarray
    signals: dict[str, np.ndarray]
    engine: str
    synthetic: bool = False
    notes: list[str] = field(default_factory=list)
    log_tail: str = ""


class Engine(ABC):
    name: str = "base"
    synthetic: bool = False
    supports_builtin_libs: bool = False

    def extra_lines(self, signals: SignalNames) -> list[str]:  # noqa: ARG002
        """Engine-specific netlist lines inserted before ``.end``."""
        return []

    def version(self) -> str:
        return "unknown"

    @abstractmethod
    def simulate(self, bundle: Bundle, workdir: Path, timeout_s: float) -> Waveforms:
        """Run one testbench and return normalised waveforms."""


def split_command(cmd: str) -> list[str]:
    """Split a command string into argv.

    On Windows, ``shlex`` must run in non-POSIX mode so backslashes in paths survive,
    but that mode keeps the surrounding quotes (``"C:\\Program Files\\x.exe"``).
    Strip them, otherwise ``subprocess`` would quote the token a second time.
    """
    if os.name == "nt":
        parts = [_unquote(p) for p in shlex.split(cmd, posix=False)]
    else:
        parts = shlex.split(cmd)
    if not parts:
        raise EngineError("empty simulator command")
    return parts


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process and everything it started."""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
            return
        # Windows: proc.kill() only ends the direct child; taskkill /T ends the tree.
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    with contextlib.suppress(OSError):
        proc.kill()


def run_process(cmd: list[str], *, cwd: Path, timeout_s: float, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a process in its own session; kill the whole group on timeout."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    popen_kwargs: dict = {}
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    else:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
    except FileNotFoundError as e:
        raise EngineError(f"simulator executable not found: {cmd[0]!r}") from e
    except OSError as e:  # e.g. WinError 193: not a valid executable
        raise EngineError(f"cannot start simulator {cmd[0]!r}: {e}") from e
    try:
        out, _ = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        _kill_tree(proc)
        proc.communicate()
        raise EngineError(f"simulation timed out after {timeout_s:.0f} s") from e
    return proc.returncode, out.decode("utf-8", errors="replace")


def tail(text: str, n: int = 40) -> str:
    return "\n".join(text.strip().splitlines()[-n:])

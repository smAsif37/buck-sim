"""Simulation engines."""

from __future__ import annotations

import shutil
import threading

from ..config import Settings
from .base import Engine, EngineError, Waveforms, split_command
from .ltspice import LTspiceEngine
from .mock import MockEngine
from .ngspice import NgspiceEngine

__all__ = ["Engine", "EngineError", "EngineRegistry", "Waveforms", "create_engine"]

ENGINE_LABELS = {
    "ngspice": "ngspice (open source)",
    "ltspice": "LTspice (Analog Devices)",
    "mock": "Mock (synthetic demo data)",
}


def create_engine(settings: Settings) -> Engine:
    return _build(settings.engine, settings)


def _build(name: str, settings: Settings) -> Engine:
    if name == "ltspice":
        return LTspiceEngine(settings.ltspice_cmd, settings.ltspice_args or None)
    if name == "ngspice":
        return NgspiceEngine(settings.ngspice_cmd)
    if name == "mock":
        return MockEngine()
    raise ValueError(f"unknown engine {name!r}")


def _command_exists(cmd: str) -> bool:
    try:
        exe = split_command(cmd)[0]
    except EngineError:
        return False
    return shutil.which(exe) is not None


class EngineRegistry:
    """The engines a user may pick per job, created lazily and cached.

    ``default`` is the engine named by ``ENGINE`` (ngspice unless configured
    otherwise). When a concrete engine instance is injected (tests), only that
    engine is offered.
    """

    def __init__(self, settings: Settings, engine: Engine | None = None) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._cache: dict[str, Engine] = {}
        if engine is not None:
            self.default = engine.name
            self._cache[engine.name] = engine
            self._fixed = True
        else:
            self.default = settings.engine
            self._fixed = False

    def names(self) -> list[str]:
        """Engines that can be selected, default first."""
        if self._fixed:
            return [self.default]
        out = []
        for name in ("ngspice", "ltspice", "mock"):
            if self.available(name):
                out.append(name)
        if self.default in out:
            out.remove(self.default)
            out.insert(0, self.default)
        return out

    def available(self, name: str) -> bool:
        if self._fixed:
            return name == self.default
        s = self.settings
        if name == "ngspice":
            return name == self.default or _command_exists(s.ngspice_cmd)
        if name == "ltspice":
            return bool(s.ltspice_cmd)
        if name == "mock":
            return s.allow_mock or self.default == "mock"
        return False

    def get(self, name: str | None = None) -> Engine:
        if self._fixed:  # a single injected engine runs everything
            return self._cache[self.default]
        name = name or self.default
        if not self.available(name):
            raise ValueError(f"engine {name!r} is not available on this server")
        with self._lock:
            if name not in self._cache:
                self._cache[name] = _build(name, self.settings)
            return self._cache[name]

    def describe(self) -> list[dict]:
        out = []
        for name in self.names():
            eng = self.get(name)
            out.append(
                {
                    "name": name,
                    "label": ENGINE_LABELS.get(name, name),
                    "synthetic": eng.synthetic,
                    "default": name == self.default,
                }
            )
        return out

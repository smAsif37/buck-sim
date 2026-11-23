"""Simulation engines."""

from __future__ import annotations

from ..config import Settings
from .base import Engine, EngineError, Waveforms
from .ltspice import LTspiceEngine
from .mock import MockEngine
from .ngspice import NgspiceEngine

__all__ = ["Engine", "EngineError", "Waveforms", "create_engine"]


def create_engine(settings: Settings) -> Engine:
    if settings.engine == "ltspice":
        return LTspiceEngine(settings.ltspice_cmd, settings.ltspice_args or None)
    if settings.engine == "ngspice":
        return NgspiceEngine(settings.ngspice_cmd)
    if settings.engine == "mock":
        return MockEngine()
    raise ValueError(f"unknown engine {settings.engine!r}")

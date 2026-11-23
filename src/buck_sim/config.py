"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

DEFAULT_BUILTIN_LIBS = (
    "standard.dio,standard.bjt,standard.mos,standard.jft,standard.cap,standard.ind,"
    "standard.res,standard.bead,ltc.lib,ltc2.lib,ltc3.lib,ltc4.lib,ltc5.lib,"
    "universalopamp.lib,universalopamp2.lib"
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"environment variable {name} must be an integer, got {raw!r}") from e


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"environment variable {name} must be a number, got {raw!r}") from e


def detect_ltspice_cmd() -> str:
    """Best-effort default for LTSPICE_CMD (empty string when nothing is found)."""
    candidates = [
        "/Applications/LTspice.app/Contents/MacOS/LTspice",
        r"C:\Program Files\ADI\LTspice\LTspice.exe",
        r"C:\Program Files\LTC\LTspiceXVII\XVIIx64.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return f'"{c}"' if sys.platform == "win32" else c
    return ""


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("./data")
    engine: str = "ltspice"  # ltspice | ngspice | mock
    ltspice_cmd: str = ""
    ltspice_args: str = ""
    ngspice_cmd: str = "ngspice"
    allow_mock: bool = False

    api_key: str = ""
    host: str = "0.0.0.0"
    port: int = 8000

    max_netlist_bytes: int = 1 * 1024 * 1024
    max_model_bytes: int = 20 * 1024 * 1024
    max_files: int = 12
    max_total_upload_bytes: int = 60 * 1024 * 1024
    queue_max: int = 200
    rate_limit_per_min: int = 60

    sim_timeout_s: float = 300.0
    job_timeout_s: float = 3600.0
    sim_parallelism: int = 1
    max_time_points: int = 3_000_000
    max_attempts: int = 2
    stale_after_s: float = 120.0
    retention_days: int = 30
    poll_interval_s: float = 1.0

    builtin_libs: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_BUILTIN_LIBS.split(",")))
    log_level: str = "INFO"
    log_format: str = "text"  # text | json

    def with_engine(self, engine: str) -> Settings:
        """Copy with another engine (choosing ``mock`` explicitly counts as consent)."""
        return replace(self, engine=engine, allow_mock=self.allow_mock or engine == "mock")

    @classmethod
    def from_env(cls, validate: bool = True) -> Settings:
        e = os.environ
        builtin = e.get("ALLOWED_BUILTIN_LIBS")
        libs = frozenset(x.strip().lower() for x in builtin.split(",") if x.strip()) if builtin is not None else None
        engine = e.get("ENGINE", "ltspice").strip().lower()
        ltspice_cmd = e.get("LTSPICE_CMD", "").strip() or detect_ltspice_cmd()
        s = cls(
            data_dir=Path(e.get("DATA_DIR", "./data")),
            engine=engine,
            ltspice_cmd=ltspice_cmd,
            ltspice_args=e.get("LTSPICE_ARGS", "").strip(),
            ngspice_cmd=e.get("NGSPICE_CMD", "ngspice").strip() or "ngspice",
            allow_mock=e.get("ALLOW_MOCK_ENGINE", "").strip().lower() in ("1", "true", "yes"),
            api_key=e.get("API_KEY", "").strip(),
            host=e.get("HOST", "0.0.0.0"),
            port=_env_int("PORT", 8000),
            max_netlist_bytes=_env_int("MAX_NETLIST_KB", 1024) * 1024,
            max_model_bytes=_env_int("MAX_MODEL_MB", 20) * 1024 * 1024,
            max_files=_env_int("MAX_FILES", 12),
            max_total_upload_bytes=_env_int("MAX_TOTAL_UPLOAD_MB", 60) * 1024 * 1024,
            queue_max=_env_int("QUEUE_MAX", 200),
            rate_limit_per_min=_env_int("RATE_LIMIT_PER_MIN", 60),
            sim_timeout_s=_env_float("SIM_TIMEOUT_S", 300.0),
            job_timeout_s=_env_float("JOB_TIMEOUT_S", 3600.0),
            sim_parallelism=max(1, _env_int("SIM_PARALLELISM", 1)),
            max_time_points=_env_int("MAX_TIME_POINTS", 3_000_000),
            max_attempts=max(1, _env_int("MAX_ATTEMPTS", 2)),
            stale_after_s=_env_float("STALE_AFTER_S", 120.0),
            retention_days=_env_int("RETENTION_DAYS", 30),
            poll_interval_s=_env_float("POLL_INTERVAL_S", 1.0),
            builtin_libs=libs if libs is not None else frozenset(DEFAULT_BUILTIN_LIBS.split(",")),
            log_level=e.get("LOG_LEVEL", "INFO").upper(),
            log_format=e.get("LOG_FORMAT", "text").lower(),
        )
        if validate:
            s.validate()
        return s

    def validate(self) -> None:
        if self.engine not in ("ltspice", "ngspice", "mock"):
            raise ValueError("ENGINE must be one of: ltspice, ngspice, mock")
        if self.engine == "mock" and not self.allow_mock:
            raise ValueError(
                "ENGINE=mock produces SYNTHETIC data. Set ALLOW_MOCK_ENGINE=1 to confirm you want that "
                "(development / demos only)."
            )
        if self.engine == "ltspice" and not self.ltspice_cmd:
            raise ValueError(
                "ENGINE=ltspice needs LTSPICE_CMD (path/command that starts LTspice). "
                "See docs/SETUP_GUIDE.md. Use ENGINE=ngspice if you have no LTspice."
            )

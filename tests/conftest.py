from __future__ import annotations

import json
from pathlib import Path

import pytest

from buck_sim.config import Settings
from buck_sim.engines.mock import MockEngine
from buck_sim.spec import JobSpec

EXAMPLES = Path(__file__).resolve().parents[1] / "src" / "buck_sim" / "examples"


@pytest.fixture()
def example_files() -> dict[str, bytes]:
    return {n: (EXAMPLES / n).read_bytes() for n in ("buck_demo.cir", "toyctrl.lib")}


@pytest.fixture()
def subckt_files() -> dict[str, bytes]:
    return {n: (EXAMPLES / n).read_bytes() for n in ("buck_demo_subckt.cir", "toyctrl.lib")}


@pytest.fixture()
def spec_dict() -> dict:
    return json.loads((EXAMPLES / "buck_demo.spec.json").read_text())


@pytest.fixture()
def spec(spec_dict) -> JobSpec:
    return JobSpec.model_validate(spec_dict)


@pytest.fixture()
def subckt_spec() -> JobSpec:
    return JobSpec.model_validate_json((EXAMPLES / "buck_demo_subckt.spec.json").read_text())


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", engine="mock", allow_mock=True, rate_limit_per_min=1000)


@pytest.fixture()
def mock_engine() -> MockEngine:
    return MockEngine()

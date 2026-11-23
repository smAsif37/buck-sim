.PHONY: install test test-engines lint serve worker selftest-mock selftest-ngspice docker-up docker-down

PY ?= python3

install:
	$(PY) -m pip install -e ".[dev]"

test:
	$(PY) -m pytest -m "not ngspice and not ltspice"

test-engines:
	$(PY) -m pytest -m "ngspice or ltspice" -v

lint:
	ruff check src tests

serve:
	buck-sim serve

worker:
	buck-sim worker

selftest-mock:
	buck-sim selftest --engine mock

selftest-ngspice:
	buck-sim selftest --engine ngspice

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

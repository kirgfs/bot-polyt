# Команды проекта. Разработка: Python 3.12 + venv; прод (VPS): Docker Compose.
PY ?= python3.12
VENV ?= .venv
BIN := $(VENV)/bin
DATE ?= $(shell date -u -d yesterday +%F 2>/dev/null)

.PHONY: install lint format typecheck test check record discover geocheck netcheck latency \
        oddspapi-eval report compact docker-build docker-up docker-logs

install:  ## venv + зависимости (закреплённые версии) + dev-инструменты
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

lint:
	$(BIN)/ruff check src tests
	$(BIN)/ruff format --check src tests

format:
	$(BIN)/ruff format src tests
	$(BIN)/ruff check --fix src tests

typecheck:
	$(BIN)/mypy src/polybot

test:
	$(BIN)/pytest

check: lint typecheck test

# --- Запуск локально (на VPS удобнее через Docker, см. docs/runbook_m1.md) -----------
record:
	$(BIN)/polybot record

discover:
	$(BIN)/polybot discover --out data/reports/discover.md

geocheck:
	$(BIN)/polybot geocheck

netcheck:
	$(BIN)/polybot netcheck --out data/reports/netcheck.md

latency:
	$(BIN)/polybot latency --out data/reports/latency.md

oddspapi-eval:  ## make oddspapi-eval STEP=meta|fixtures|coverage|sample|burst|summary
	$(BIN)/polybot oddspapi-eval $(STEP) --out data/reports/oddspapi_$(STEP).md

report:
	$(BIN)/polybot report --days 7 --out data/reports/m1_data.md

compact:  ## make compact DATE=2026-09-24 (по умолчанию — вчера, UTC)
	$(BIN)/polybot compact --date $(DATE)

# --- Docker (VPS) -------------------------------------------------------------------
docker-build:
	docker compose build

docker-up:
	docker compose up -d recorder

docker-logs:
	docker compose logs -f --tail=200 recorder

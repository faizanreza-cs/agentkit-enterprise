PY ?= .venv/bin/python

.PHONY: setup test lint eval eval-llm api docker

setup:
	uv sync --locked --extra dev

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy

eval:             ## governance eval with the offline mock planner
	$(PY) evals/run_eval.py --min-pass-rate 1.0

eval-llm:         ## needs ANTHROPIC_API_KEY and AGENTKIT_MODEL
	$(PY) evals/run_eval.py --provider anthropic

api:              ## API + operator console on http://localhost:8000
	$(PY) -m uvicorn agentkit.api:app --reload --port 8000

docker:
	docker build -t agentkit-enterprise .

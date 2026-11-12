# One-word commands for Linux, macOS and CI. On Windows without make, use the commands in the
# comments (with the venv active).
PYTHON ?= python

.PHONY: run test lint check

run:  # python -m riskplatform.pipeline   (or: riskplatform-run)
	$(PYTHON) -m riskplatform.pipeline

test:  # pytest
	$(PYTHON) -m pytest

lint:  # ruff check . ; ruff format --check .
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

check: lint test

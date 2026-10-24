.PHONY: help install dev test lint fmt demo clean

PYTHON ?= python3
SEED ?= demo

help:                ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-10s %s\n", $$1, $$2}'

install:             ## Install the package
	$(PYTHON) -m pip install .

dev:                 ## Install with dev extras (pytest, ruff)
	$(PYTHON) -m pip install -e ".[dev]"

test:                ## Run the test suite
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py'

lint:                ## Lint with ruff (requires dev extras)
	ruff check nox tests

fmt:                 ## Auto-fix lint issues
	ruff check --fix nox tests

demo:                ## Run the bundled example artifacts (SEED=demo)
	$(PYTHON) -m nox run examples/fib.nxa --seed $(SEED)
	@echo "hello" | $(PYTHON) -m nox run examples/cat.nxa --seed $(SEED)

clean:               ## Remove build artifacts and caches
	rm -rf build dist *.egg-info .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

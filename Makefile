PY ?= python

.PHONY: install check test lint type bench run
install:
	$(PY) -m pip install -r requirements-dev.txt -e .
lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .
type:
	$(PY) -m mypy
test:
	$(PY) -m pytest --cov
check: lint type test
bench:
	$(PY) benchmarks/bench.py --docs 5000 --tokens 200
run:
	$(PY) -m uvicorn docsearch.api:create_app --factory --reload

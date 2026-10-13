PY ?= .venv/bin/python

.PHONY: install install-spa run spa-dev spa-build test test-fast test-spa lint check

install:            ## create .venv and install service, CLI, engines and dev tools
	python3 -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e '.[engines,dev]'

install-spa:
	cd spa && npm install

run:                ## start opal-talk-svc using config/service.yaml
	.venv/bin/opal-talk-svc

spa-dev:            ## Vite dev server on http://127.0.0.1:5173 (API at http://127.0.0.1:8001)
	cd spa && npm run dev

spa-build:          ## build the SPA into spa/dist (served at /ui by the service)
	cd spa && npm run build

test:               ## all Python tests, including real Piper/Kokoro tests when available
	$(PY) -m pytest

test-fast:          ## Python tests without real engines
	$(PY) -m pytest -m "not piper and not kokoro"

test-spa:
	cd spa && npm test && npm run typecheck

lint:
	.venv/bin/ruff check src tests

check: lint test test-spa

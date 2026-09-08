# Common tasks. `make setup` once, then `make test` / `make app` / `make lint`.
.PHONY: setup test lint smoke app operator console seed patch-check metrics docker-up docker-test clean

PY ?= python

setup:
	$(PY) -m pip install -r requirements.txt ruff
	$(PY) -m playwright install --with-deps chromium

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

# Live replay smoke test (incl. the data_unavailable path) — needs the app running.
smoke:
	$(PY) scripts/smoke_test_replay.py

seed:
	cd app && $(PY) -c "import models; models.init_db(); models.seed()"

app: seed
	cd app && $(PY) app.py

operator:
	$(PY) escalation/operator_page.py

# Link-gated live console (needs the mock app on :5050). Prints a share URL with the access key.
# Set ANTHROPIC_API_KEY to enable Discover mode; Replay works without it.
console:
	$(PY) -m webconsole.server

# CI guard: fail if the committed capability artifacts have drifted from app_knowledge/*.yaml.
patch-check:
	$(PY) scripts/patch_capabilities.py --check

# Per-capability outcome mix / latency / drift from the replay trace files.
metrics:
	$(PY) scripts/replay_metrics.py

docker-up:
	docker compose up --build bank operator

docker-test:
	docker compose run --rm tests

clean:
	rm -f app/bank.db app/bank.db-journal
	find . -name __pycache__ -type d -exec rm -rf {} +

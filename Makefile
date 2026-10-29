# Shortcuts for the things people run most. Each target is one line you could
# type yourself; the Makefile only saves remembering the directories.

PY ?= python3

.PHONY: help up down test train figures results verify clean-models

help:
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

up:            ## Run the whole stack with the device simulator (no hardware)
	docker compose up --build

down:          ## Stop it
	docker compose down

test:          ## Backend tests
	cd backend && $(PY) -m pytest

train:         ## Generate the synthetic set, train, compare, write ai/reports/
	cd ai && $(PY) training/generate_dataset.py
	cd ai && $(PY) training/train.py
	cd ai && $(PY) training/compare_models.py
	$(PY) ai/reports/results_table.py
	mkdir -p backend/models && cp ai/models/behaviour.pkl backend/models/behaviour.pkl

figures:       ## Redraw the README figures from ai/reports/metrics.json
	$(PY) ai/reports/make_figures.py

results:       ## Rewrite ai/reports/RESULTS.md from metrics.json
	$(PY) ai/reports/results_table.py

verify:        ## Recompute every published number in eight languages
	bash verify/verify.sh

clean-models:  ## Remove the trained artefact so the backend falls back to rules
	rm -f ai/models/behaviour.pkl backend/models/behaviour.pkl

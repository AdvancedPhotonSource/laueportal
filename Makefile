# Laue Portal developer entry points. The recipes that run the env's tools source scripts/env.sh.
# Run `make help` for the list.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help
MAKEFLAGS += --no-print-directory

ENV := . scripts/env.sh
PY := $$LAUE_DEV_WORKSPACE/env/bin/python
SUPERVISOR_DEV := deploy/supervisor/dev.conf
SUPERVISOR_E2E := deploy/supervisor/e2e.conf
# Python locks resolve only releases at least 7 days old. A timestamp, not a date: uv reads a bare date as the end of
# that day in local time, which lets in releases younger than 7 days.
# Evaluated once (an override on the command line wins), so every use in `lock` sees the same cutoff.
ifeq ($(origin LOCK_DATE),undefined)
LOCK_DATE := $(shell date -u -d '7 days ago' +%Y-%m-%dT%H:%M:%SZ)
endif
# Exact pins younger than LOCK_DATE that are approved as already deployed, as `package=time` entries: each
# package alone is exempt from the cutoff, up to the given time. Drop an entry once LOCK_DATE has passed it.
LOCK_EXEMPT :=
# conda-forge's Python packages (environment.yml), with pycparser, which conda's cffi brings: the locks are
# constrained to the env's versions and leave them out, so pip never replaces them (scripts/setup.sh checks).
CONDA_PY := numpy h5py cffi pycparser
# Targeted runs: `make e2e SPECS="e2e/scans.spec.ts e2e/runs.spec.ts"` runs only those specs (paths from
# frontend/; all of them by default); `make test-slow TESTS="tests/jobs/test_faults.py"` (paths from backend/, or
# TESTS='-k parity') runs only that area's slow tests (all of them by default), and `make test TESTS=...` its fast
# tests.
SPECS ?=
TESTS ?=
# The backend's fast tests in two halves of about equal time, CI's two backend test jobs: test-jobs-remote runs these
# folders, test-others every other (paths from backend/).
JOBS_REMOTE := tests/jobs tests/remote
FAST_TESTS := pytest -q -m "not slow"
# check-hygiene's `git diff --check` compares the working tree with this commit: CI gives the pull request's base or
# the commit before a push; empty, it is the commit where HEAD left main (scripts/check-tree.sh).
DIFF_BASE ?=

.PHONY: help setup setup-backend setup-frontend lock lock-env dev-up dev-down dev-status dev-logs gen check \
        check-hygiene check-paths check-docs check-backend check-frontend check-contract test test-jobs-remote \
        test-others test-slow test-frontend-slow e2e e2e-build dev-spa build db-upgrade db-check

help: ## list targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

setup: ## build/refresh the environment (idempotent): setup-backend and setup-frontend
	@scripts/setup.sh

setup-backend: ## the backend part of setup: the conda env, the Python lock, lauelab, the backend, the dev config
	@scripts/setup.sh backend

setup-frontend: ## the frontend part of setup: pnpm and node_modules
	@scripts/setup.sh frontend

lock: ## re-lock Python packages (dev, base, launcher, compute; releases older than LOCK_DATE)
	@$(ENV)
	pins=requirements/conda-pins.txt
	$(PY) -c 'import importlib.metadata as m, sys; print("\n".join(f"{n}=={m.version(n)}" for n in sys.argv[1:]))' \
	  $(CONDA_PY) | sed '1i # conda-forge versions in the env (environment.yml); constraints only, never installed by pip' > $$pins
	# The conda packages are not installed from PyPI, so the PyPI age cutoff doesn't apply to them.
	tomorrow="$$(date -u -d tomorrow +%Y-%m-%d)"
	# dev first (it includes launcher.in): base and launcher are constrained to the same versions. So is compute
	# (the compute nodes' set, never installed here), except for the packages compute.in pins itself: those keep
	# its pins.
	for name in dev base launcher compute; do
	  constraints=""
	  case $$name in
	    base|launcher) constraints="-c requirements/dev.constraints" ;;
	    compute)
	      own="$$(sed -nE 's/^([A-Za-z0-9_.-]+)==.*/\1/p' requirements/compute.in | tr 'A-Z_.' 'a-z--' | paste -sd'|')"
	      grep -vE "^($$own)==" requirements/dev.constraints > requirements/compute.constraints
	      constraints="-c requirements/compute.constraints" ;;
	  esac
	  args=(--generate-hashes --python "$(PY)" --exclude-newer $(LOCK_DATE) \
	    $(foreach p,$(CONDA_PY),--exclude-newer-package $(p)=$$tomorrow) \
	    $(foreach e,$(LOCK_EXEMPT),--exclude-newer-package $(e)) \
	    -c $$pins $(foreach p,$(CONDA_PY),--no-emit-package $(p)) $$constraints \
	    requirements/$$name.in -o requirements/$$name.lock.tmp)
	  # The lock's header records this command with the workspace as the variable, never a local path.
	  shown="uv pip compile $${args[*]}"; variable='$$LAUE_DEV_WORKSPACE'
	  uv pip compile -q "$${args[@]}" --custom-compile-command "$${shown//"$$LAUE_DEV_WORKSPACE"/$$variable}"
	  { echo "# exclude-newer: $(LOCK_DATE)"; cat requirements/$$name.lock.tmp; } > requirements/$$name.lock
	  rm requirements/$$name.lock.tmp
	  if [ $$name = dev ]; then
	    grep -E '^[A-Za-z0-9_.-]+(\[[^]]*\])?==' requirements/dev.lock | sed 's/ .*//' > requirements/dev.constraints
	  fi
	done
	rm -f requirements/dev.constraints requirements/compute.constraints
	echo "locked with exclude-newer $(LOCK_DATE)"

lock-env: ## re-solve environment.yml into environment.lock (explicit conda lock), then run `make setup`
	@$(ENV)
	mm=$$LAUE_DEV_WORKSPACE/tools/micromamba; tmp=$$TMPDIR/lock-env
	# micromamba registers every env it creates in ~/.conda/environments.txt; `env remove` unregisters it too
	# (rm -rf alone would leave the line behind). The temporary env goes whatever happens.
	drop_tmp() { if [ -d $$tmp/conda-meta ]; then $$mm env remove -y -q -p $$tmp; fi; rm -rf $$tmp; }
	drop_tmp
	trap drop_tmp EXIT
	$$mm create -y -q -p $$tmp -f environment.yml
	{ echo "# Generated by 'make lock-env' from environment.yml on $$(date -u +%Y-%m-%d). Do not edit."; \
	  $$mm env export -p $$tmp --explicit --md5 | grep -v '^#'; } > environment.lock
	echo "wrote environment.lock"

dev-up: dev-spa ## start the dev stack (API 2100, worker, Vite 2101); rebuilds the SPA when older than frontend/src, public/, index.html, package.json, the pnpm lock, the Vite or TS config; seeds the demo DB if missing
	@$(ENV); scripts/stack.sh up --seed=missing $(SUPERVISOR_DEV)

# dev-up's SPA: rebuilt when frontend/dist/index.html is missing or older than any of the SPA's sources: the files
# and folders under frontend/src and frontend/public (a folder's mtime changes when an entry is added, moved in or
# deleted, so a deletion or a file moved in with an old mtime counts), index.html, package.json, the pnpm lock, the
# Vite config and tsconfig.json. Make splits prerequisites on spaces, so a path with a space is left out (the
# nearest folder without one still counts its arrival and its removal, not its edits). Its recipe holds no $(MAKE) (such a recipe runs
# even under `make -n` or `-q`, one shell per recipe, .ONESHELL), so `make -n dev-up` starts and builds nothing
# (tests/test_dev_stack.py runs it on a stand-in tree).
SPA_SOURCES := $(shell find frontend/src frontend/public -path '* *' -prune -o -print 2>/dev/null) frontend/index.html \
               frontend/package.json frontend/pnpm-lock.yaml frontend/vite.config.ts frontend/tsconfig.json
dev-spa: frontend/dist/index.html

frontend/dist/index.html: $(SPA_SOURCES)
	@$(ENV)
	echo "frontend/dist is missing or older than its sources: building the SPA"
	pnpm -C frontend build

dev-down: ## stop the dev stack
	@$(ENV); scripts/stack.sh down $(SUPERVISOR_DEV)

dev-status: ## show the dev stack's programs and health (fails when it is not running)
	@$(ENV); scripts/stack.sh status $(SUPERVISOR_DEV)

dev-logs: ## tail the dev stack logs
	@$(ENV); tail -n 50 -F $$LAUE_DEV_WORKSPACE/logs/dev-*.log

gen: ## OpenAPI -> frontend/openapi.json -> Orval client
	@$(ENV)
	laueportal openapi --out frontend/openapi.json
	pnpm -C frontend exec orval

# CI runs each of check's parts as a job of its own (.github/workflows/ci.yml).
check: check-hygiene check-backend test-jobs-remote test-others check-frontend check-contract ## all fast gates (run before every merge)

check-hygiene: check-paths check-docs ## the path and docs link checks, whitespace errors since DIFF_BASE, no tracked generated files
	@scripts/check-tree.sh $(DIFF_BASE)

check-paths:
	@scripts/check-paths.sh

# Relative links in the Markdown files resolve.
check-docs:
	@python3 scripts/check-doc-links.py

check-backend: ## ruff, ruff format, import-linter, pyright
	@$(ENV)
	cd backend
	echo "--- ruff"; ruff check . && ruff format --check .
	echo "--- import-linter"
	if grep -q '^\[\[tool.importlinter.contracts\]\]' pyproject.toml; then lint-imports; \
	else echo "SKIPPED: no import contracts yet"; fi
	echo "--- pyright"; pyright

# tsc keeps its incremental build info, and ESLint its results, in the workspace's cache/frontend/ (CI restores and
# saves that folder). tsc checks again what changed and what depends on it. ESLint's type-aware rules read other files'
# types, so its results are kept for one state of frontend/ (scripts/frontend-digest.sh) and dropped at any change.
check-frontend: ## tsc, ESLint, Prettier, Vitest without the slow set (test-frontend-slow)
	@$(ENV)
	if [ ! -f frontend/package.json ]; then echo "SKIPPED: no frontend yet"; exit 0; fi
	cache="$$LAUE_DEV_WORKSPACE/cache/frontend"
	digest="$$(scripts/frontend-digest.sh)"
	mkdir -p "$$cache/eslint"
	find "$$cache/eslint" -mindepth 1 -maxdepth 1 ! -name "$$digest" -exec rm -rf {} +
	cd frontend
	echo "--- tsc"; pnpm exec tsc --noEmit -p . --incremental --tsBuildInfoFile "$$cache/tsconfig.tsbuildinfo"
	echo "--- eslint"
	pnpm exec eslint --cache --cache-strategy content --cache-location "$$cache/eslint/$$digest" .
	echo "--- prettier"; pnpm exec prettier --check .
	echo "--- vitest"; pnpm exec vitest run --project node --project jsdom

test-frontend-slow: ## Vitest's slow set: the heaviest page tests (SLOW_TESTS in frontend/vite.config.ts)
	@$(ENV); cd frontend; pnpm exec vitest run --project slow

check-contract: ## make gen, then the client in the index matches it
	@$(ENV)
	if [ ! -f frontend/orval.config.ts ] || [ ! -f backend/src/laueportal/cli.py ]; then
	  echo "SKIPPED: needs the frontend client"; exit 0; fi
	$(MAKE) gen
	# The client in the index (staged, or committed when nothing is staged) is what `make gen` writes, and every
	# generated file is in the index.
	git diff --exit-code -- frontend/openapi.json frontend/src/api/generated
	untracked="$$(git ls-files --others -- frontend/openapi.json frontend/src/api/generated)"
	test -z "$$untracked" || { echo "generated files not in the index:"; echo "$$untracked"; exit 1; }

test: ## backend tests except slow ones; TESTS= picks an area
	@$(ENV); cd backend; $(FAST_TESTS) $(TESTS)

test-jobs-remote: ## backend tests except slow ones, in tests/jobs and tests/remote (half of `test`)
	@$(ENV); cd backend; $(FAST_TESTS) $(JOBS_REMOTE)

test-others: ## backend tests except slow ones, outside tests/jobs and tests/remote (the other half of `test`)
	@$(ENV); cd backend; $(FAST_TESTS) $(addprefix --ignore=,$(JOBS_REMOTE))

test-slow: ## native lauelab compute, the live-against-batch parity, the demo seed, the fault injection, the 200,000-file browse timing; TESTS= picks an area
	@$(ENV); cd backend; pytest -q -m slow $(TESTS)

# e2e's first step, apart: a recipe that holds $(MAKE) runs even under `make -n` (one shell per recipe, .ONESHELL),
# so the sub-make sits in a step of its own, with nothing that starts a stack (`make -n e2e` starts nothing).
# It refuses while the dev stack runs, then builds.
e2e-build:
	@$(ENV)
	if scripts/stack.sh running $(SUPERVISOR_DEV); then echo "the dev stack is running: make dev-down first"; exit 1; fi
	$(MAKE) build

build: ## build the SPA into frontend/dist
	@$(ENV); pnpm -C frontend build

e2e: e2e-build ## build, start a disposable freshly seeded stack, run Playwright (default, and no-gpu for @webgl tests; SPECS= picks specs), stop it
	@$(ENV)
	# From here the e2e stack is always stopped, whatever fails (the exit status is kept).
	trap 'status=$$?; scripts/stack.sh down $(SUPERVISOR_E2E) || status=1; exit $$status' EXIT
	scripts/stack.sh up --seed=always $(SUPERVISOR_E2E)
	pnpm -C frontend exec playwright test $(SPECS)

db-upgrade: ## upgrade the configured database to head
	@$(ENV); laueportal db upgrade

db-check: ## check the configured database against the models
	@$(ENV); laueportal db check

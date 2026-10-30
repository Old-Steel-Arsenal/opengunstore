.PHONY: help dev-up dev-down dev-logs dev-shell dev-names dev-bench-init dev-bench-clone dev-site-create dev-site-drop dev-install-our-apps dev-reset dev-upgrade test test-js test-serial ci-upgrade-test

DEV_COMPOSE := deploy/dev/docker-compose.yml
DEV_NOPORTS_COMPOSE := deploy/dev/docker-compose.noports.yml
SITE_NAME ?= dev.localhost
SITE_ADMIN_PASSWORD ?= admin
DB_ROOT_PASSWORD ?= changeme123
FRAPPE_BRANCH ?= version-16

# ---- one compose project per checkout ---------------------------------------
# The main checkout (`.git` is a directory) keeps the historical project name `dev` and the
# container names `gunstore-*`, so the existing DB volume, docs and `docker exec gunstore-frappe …`
# keep working — the resolved compose config is byte-identical to before. A linked git worktree
# (`.git` is a file) gets its own project, containers, network and DB volume named after the
# worktree directory (lower-cased, non [a-z0-9_-] → '-'), so `make test` can run there while the
# main checkout is busy. A tree without `.git` at all behaves like the main checkout.
WORKTREE := $(if $(wildcard .git/HEAD),,$(if $(wildcard .git),$(shell printf '%s' '$(notdir $(CURDIR))' | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_-' '-'),))
PROJECT ?= dev$(if $(WORKTREE),-$(WORKTREE))
CONTAINER_PREFIX ?= gunstore$(if $(WORKTREE),-$(WORKTREE))
ifneq ($(WORKTREE),)
ifneq ($(filter dev,$(PROJECT))$(filter gunstore,$(CONTAINER_PREFIX)),)
$(error a worktree must not reuse the main checkout's project 'dev' / prefix 'gunstore' (it would rebuild the main containers onto this tree))
endif
endif
# Host ports: the main checkout publishes 8000-8005 / 9000-9005 as before (defaults inside
# docker-compose.yml, so even a raw `docker compose -f …` there resolves unchanged). A worktree
# publishes nothing — docker-compose.noports.yml resets the ports (tests need no host port) —
# unless PORT_OFFSET is given at `make dev-up` time: PORT_OFFSET=10 → 8010-8015 / 9010-9015.
PORT_OFFSET ?= $(if $(WORKTREE),,0)
ifneq ($(PORT_OFFSET),)
ifneq ($(shell printf '%s' '$(PORT_OFFSET)' | grep -Ec '^[0-9]+$$'),1)
$(error PORT_OFFSET must be a non-negative integer, got '$(PORT_OFFSET)')
endif
endif
WEB_PORTS := $(if $(PORT_OFFSET),$(shell echo $$((8000+$(PORT_OFFSET)))-$$((8005+$(PORT_OFFSET)))),)
SOCKETIO_PORTS := $(if $(PORT_OFFSET),$(shell echo $$((9000+$(PORT_OFFSET)))-$$((9005+$(PORT_OFFSET)))),)
COMPOSE := CONTAINER_PREFIX=$(CONTAINER_PREFIX) WEB_PORTS=$(WEB_PORTS) SOCKETIO_PORTS=$(SOCKETIO_PORTS) \
	docker compose -p $(PROJECT) -f $(DEV_COMPOSE)$(if $(PORT_OFFSET),, -f $(DEV_NOPORTS_COMPOSE))

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-25s\033[0m %s\n", $$1, $$2}'

dev-names: ## Show this checkout's compose project / container names / published ports
	@echo "project=$(PROJECT) containers=$(CONTAINER_PREFIX)-{mariadb,redis-cache,redis-queue,frappe} ports=$(if $(PORT_OFFSET),$(WEB_PORTS) $(SOCKETIO_PORTS),none (make dev-up PORT_OFFSET=10 to publish 8010/9010))"

dev-up: ## Start dev containers (mariadb, redis, frappe bench)
	@$(MAKE) --no-print-directory dev-names
	$(COMPOSE) up -d
	@echo "Waiting for mariadb..."
	@until $(COMPOSE) exec -T mariadb healthcheck.sh --connect >/dev/null 2>&1; do sleep 2; done
	@echo "Containers ready. Next: make dev-bench-init (only once)"

dev-down: ## Stop dev containers
	$(COMPOSE) down

dev-down-volumes: ## Stop dev containers AND wipe volumes (DB reset)
	$(COMPOSE) down -v

dev-logs: ## Tail frappe container logs
	$(COMPOSE) logs -f frappe

dev-shell: ## Open bash shell inside frappe container
	$(COMPOSE) exec frappe bash

dev-bench-init: ## ONE-TIME: bootstrap a frappe-bench inside the container with ERPNext (uses built-in POS)
	$(COMPOSE) exec -T frappe bash -lc '\
		cd /workspace && \
		if [ -d frappe-bench ]; then echo "frappe-bench already exists, skipping init"; exit 0; fi && \
		bench init \
			--frappe-branch $(FRAPPE_BRANCH) \
			--ignore-exist \
			--skip-redis-config-generation \
			frappe-bench && \
		cd frappe-bench && \
		bench set-config -g db_host mariadb && \
		bench set-config -g redis_cache redis://redis-cache:6379 && \
		bench set-config -g redis_queue redis://redis-queue:6379 && \
		bench set-config -g redis_socketio redis://redis-queue:6379 && \
		bench get-app --branch $(FRAPPE_BRANCH) erpnext https://github.com/frappe/erpnext.git \
	'
	@$(MAKE) --no-print-directory dev-site-create

dev-bench-clone: ## Worktree only: clone the main checkout's frappe-bench (APFS copy-on-write, seconds) instead of bench init, then create the site
	@test -n "$(WORKTREE)" || { echo "dev-bench-clone is for linked worktrees; the main checkout uses make dev-bench-init"; exit 1; }
	@if [ -e frappe-bench ]; then echo "frappe-bench already exists, skipping clone"; else \
		src=$$(git rev-parse --path-format=absolute --git-common-dir)/../frappe-bench; \
		test -d "$$src" || { echo "no frappe-bench in the main checkout ($$src): run make dev-bench-init there first"; exit 1; }; \
		tmp=frappe-bench.partial.$$$$; rm -rf "$$tmp"; \
		echo "Cloning $$src -> ./frappe-bench"; \
		{ cp -Rc "$$src" "$$tmp" 2>/dev/null || { rm -rf "$$tmp"; echo "cp -c (APFS clone) unavailable, plain copy"; cp -R "$$src" "$$tmp"; }; } \
			&& mv "$$tmp" frappe-bench || { rm -rf "$$tmp"; echo "clone failed, nothing left behind"; exit 1; }; \
	fi
	@$(MAKE) --no-print-directory dev-site-create
	@$(MAKE) --no-print-directory dev-install-our-apps

dev-site-create: ## Create the dev site and install ERPNext; always (re-)applies allow_tests
	$(COMPOSE) exec -T frappe bash -lc '\
		cd /workspace/frappe-bench && \
		if bench --site $(SITE_NAME) doctor >/dev/null 2>&1; then \
			echo "*** Site $(SITE_NAME) already exists — NOT recreated; drop it with: make dev-site-drop CONFIRM=yes"; \
		else \
			bench new-site $(SITE_NAME) \
				--mariadb-root-password $(DB_ROOT_PASSWORD) \
				--admin-password $(SITE_ADMIN_PASSWORD) \
				--install-app erpnext \
				--no-mariadb-socket \
				--force; \
		fi && \
		bench --site $(SITE_NAME) set-config allow_tests true && \
		echo "allow_tests set on $(SITE_NAME) (make test refuses to run without it)" \
	'

dev-site-drop: ## DESTRUCTIVE: drop the dev site and its database (needs CONFIRM=yes; check `make dev-names` first)
	@if [ "$(CONFIRM)" != "yes" ]; then \
		echo "Refusing: this drops site $(SITE_NAME) on compose project $(PROJECT) — every row on it is lost."; \
		echo "  Check 'make dev-names' is the stack you mean, then: make dev-site-drop CONFIRM=yes"; \
		exit 1; \
	fi
	$(COMPOSE) exec -T frappe bash -lc '\
		cd /workspace/frappe-bench && \
		bench drop-site $(SITE_NAME) \
			--db-root-password $(DB_ROOT_PASSWORD) \
			--force --no-backup \
	'

dev-serve: ## Start bench serve (foreground; access at http://localhost:8000)
	$(COMPOSE) exec frappe bash -lc 'cd /workspace/frappe-bench && bench start'

dev-install-our-apps: ## Install ffl_core / ffl_integrations / ffl_woo_sync into the dev site
	$(COMPOSE) exec -T frappe bash -lc '\
		cd /workspace/frappe-bench && \
		for app in ffl_core ffl_integrations ffl_woo_sync; do \
			if [ ! -d apps/$$app ]; then \
				ln -s /workspace/apps/$$app apps/$$app && \
				./env/bin/pip install -e apps/$$app && \
				{ [ -n "$$(tail -c1 sites/apps.txt 2>/dev/null)" ] && echo >> sites/apps.txt; true; }; \
				grep -qxF "$$app" sites/apps.txt || echo "$$app" >> sites/apps.txt; \
			fi; \
			bench --site $(SITE_NAME) install-app $$app || true; \
		done \
	'

dev-reset: dev-down-volumes ## Full reset: drop containers + volumes + frappe-bench dir
	rm -rf frappe-bench

# Refuses a site that would run zero tests, then bootstraps the masters once, outside the test transaction.
# One shell line (it is spliced into a single-quoted bash -lc string); both test targets start with it.
TEST_PREFLIGHT = \
	if ! python3 -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get(\"allow_tests\") else 1)" \
			"sites/$(SITE_NAME)/site_config.json" 2>/dev/null; then \
		echo "ERROR: sites/$(SITE_NAME)/site_config.json has no truthy allow_tests."; \
		echo "  frappe/commands/testing.py returns EARLY and exits 0 when it is missing"; \
		echo "  (it just prints \"Testing is disabled for the site!\"), so the run would"; \
		echo "  report success having executed ZERO tests. Refusing to fake a green run."; \
		echo "  Fix it with either of:"; \
		echo "    make dev-site-create"; \
		echo "    make dev-shell   \# then: bench --site $(SITE_NAME) set-config allow_tests true"; \
		exit 1; \
	fi && \
	bench --site $(SITE_NAME) execute ffl_core.setup.test_bootstrap.run

WORKERS ?= 6

test: test-js ## All tests: JS unit tests, then the three apps split across WORKERS sites at once (default 6; make test WORKERS=n)
	$(COMPOSE) exec -T frappe bash -lc '\
		set -o pipefail; \
		cd /workspace/frappe-bench && \
		$(TEST_PREFLIGHT) && \
		mkdir -p logs && \
		{ pkill -f "^[^ ]*python[0-9.]* [^ ]*/scripts/ci/parallel_(tests|worker)\.py" && echo "stopped a parallel run left over from an interrupted make test (Ctrl-C does not reach it)" && sleep 2 || true; } && \
		python3 /workspace/scripts/ci/parallel_tests.py --bench . --apps "ffl_core ffl_integrations ffl_woo_sync" \
			--workers $(WORKERS) --base-site $(SITE_NAME) --db-root-password $(DB_ROOT_PASSWORD) \
			--timings logs/test-timings.json 2>&1 | tee logs/make-test.log \
	'

test-js: ## JS unit tests (no site needed; the same command CI runs)
	$(COMPOSE) exec -T frappe bash -lc 'cd /workspace && node --test "apps/ffl_core/ffl_core/tests/js/*.test.js"'

test-serial: ## Same as test, one app after another on one site (slower; for tracing one failure in order)
	$(COMPOSE) exec -T frappe bash -lc '\
		set -o pipefail; \
		cd /workspace/frappe-bench && \
		$(TEST_PREFLIGHT) && \
		{ status=0; \
		mkdir -p logs; : > logs/make-test.log; \
		for app in ffl_core ffl_integrations ffl_woo_sync; do \
			if [ -d apps/$$app/$$app/tests ]; then \
				bench --site $(SITE_NAME) run-tests --app $$app 2>&1 | tee -a logs/make-test.log || status=1; \
			fi; \
		done; \
		ran=0; failures=0; errors=0; \
		for n in $$(sed -nE "s/^Ran ([0-9]+) tests? in .*/\1/p" logs/make-test.log); do ran=$$((ran+n)); done; \
		for n in $$(sed -nE "s/^FAILED \((.*, )?failures=([0-9]+).*/\2/p" logs/make-test.log); do failures=$$((failures+n)); done; \
		for n in $$(sed -nE "s/^FAILED \((.*, )?errors=([0-9]+).*/\2/p" logs/make-test.log); do errors=$$((errors+n)); done; \
		echo "TOTAL ran=$$ran failures=$$failures errors=$$errors"; \
		exit $$status; } \
	'

.PHONY: ext-smoke

ext-smoke: ## Extension-layer smoke test against an image: make ext-smoke IMAGE=<registry>/<image>:<tag>
	@test -n "$(IMAGE)" || { echo "usage: make ext-smoke IMAGE=<registry>/<image>:<tag>"; exit 1; }
	deploy/prod/tests/test_ext_replay.sh "$(IMAGE)"
	deploy/prod/tests/test_ext_agent.sh
	deploy/prod/tests/test_mcp_deploy.sh
	deploy/prod/tests/test_prune_images.sh

.PHONY: ext-lint ext-new ext-test

ext-lint: ## Lint an extension (dir or zip) against the extension lint rules: make ext-lint DIR=apps-ext/<ext_id>
	@test -n "$(DIR)" || { echo "usage: make ext-lint DIR=<app dir | zip>"; exit 1; }
	$(COMPOSE) exec -T frappe bash -lc 'cd /workspace/frappe-bench && env/bin/python -m ffl_core.extensions.lint "$(DIR)"'

ext-new: ## New extension from the skeleton: make ext-new ID=acme_hello TITLE="Acme Hello" PUBLISHER="Acme Software LLC" [OUT=../acme_hello]
	@test -n "$(ID)" -a -n "$(TITLE)" -a -n "$(PUBLISHER)" || { echo 'usage: make ext-new ID=<vendor>_<name> TITLE="…" PUBLISHER="…" [OUT=../<id>]'; exit 2; }
	python3 scripts/ext_new.py --id "$(ID)" --title "$(TITLE)" --publisher "$(PUBLISHER)" --out "$(or $(OUT),../$(ID))"

ext-test: ## Install an extension on this checkout's dev site, then its tests, lint and API-docs check (the core CI's steps): make ext-test ID=<ext_id> DIR=../<ext_repo>
	@test -n "$(ID)" -a -n "$(DIR)" || { echo "usage: make ext-test ID=<id> DIR=<extension folder: pyproject.toml next to <id>/>"; exit 2; }
	@# A copy, not a mount: the folder may sit outside this checkout. It is unpacked beside apps-ext and checked there with
	@# every refusal of the run (ext_test.sh --check: id, core app, hooks.py, wheels/, apps/<id>) before it replaces
	@# apps-ext/<id>, where replay keeps extensions: a refused folder leaves apps-ext as it was, and no copy behind.
	COPYFILE_DISABLE=1 tar -C "$(DIR)" --exclude .git --exclude node_modules --exclude __pycache__ -cf - . | \
		$(COMPOSE) exec -T frappe bash -c 'set -e; b=/workspace/frappe-bench; t=$$(mktemp -d "$$b/.ext-test.XXXXXX"); trap "rm -rf \"$$t\"" EXIT; \
			tar -C "$$t" -xf -; echo "==> check the folder (unpacked in $$t) before it replaces apps-ext/$(ID)"; \
			/workspace/scripts/ci/ext_test.sh --check "$$b" "$(ID)" "$$t" "$$b/apps-ext/$(ID)"; \
			mkdir -p "$$b/apps-ext"; rm -rf "$$b/apps-ext/$(ID)"; mv "$$t" "$$b/apps-ext/$(ID)"'
	$(COMPOSE) exec -T frappe bash -lc 'cd /workspace/frappe-bench && \
		bench --site $(SITE_NAME) execute ffl_core.setup.test_bootstrap.run && \
		/workspace/scripts/ci/ext_test.sh . $(SITE_NAME) $(ID) apps-ext/$(ID)'

.PHONY: bump-version api-docs api-docs-check api-docs-serve

bump-version: ## Set the product version in all three apps (needs the release note first): make bump-version VERSION=1.2.0
	@test -n "$(VERSION)" || { echo "usage: make bump-version VERSION=x.y.z"; exit 2; }
	python3 scripts/bump_version.py $(VERSION)

api-docs: ## Regenerate the API reference (docs/api/) and each app's api_catalog.json from source — no bench needed
	python3 scripts/generate_api_docs.py

api-docs-check: ## Verify (parity+conformance), regenerate, fail if docs/api is stale (CI gate)
	python3 scripts/generate_api_docs.py --check
	python3 scripts/generate_api_docs.py
	@git diff --exit-code HEAD -- docs/api 'apps/*/*/api_catalog.json' || { \
		echo "ERROR: docs/api out of sync with HEAD — if generated files"; \
		echo "changed, run 'make api-docs' and commit; if a hand-written"; \
		echo "page (README.md/frappe-rest.md/index.html) changed, commit it."; \
		exit 1; }
	@untracked=$$(git ls-files --others --exclude-standard docs/api 'apps/*/*/api_catalog.json'); \
	if [ -n "$$untracked" ]; then \
		echo "ERROR: untracked generated files — commit them:"; echo "$$untracked"; exit 1; fi

api-docs-serve: ## Serve the Redoc API browser at http://localhost:8033
	@echo "API docs at http://localhost:8033 (Ctrl-C to stop)"
	@cd docs/api && python3 -m http.server 8033

dev-upgrade: ## Move this checkout's bench to the latest Frappe + ERPNext $(FRAPPE_BRANCH) and migrate (no tests)
	$(COMPOSE) exec -T frappe bash -lc '\
		set -e; cd /workspace/frappe-bench && \
		for app in frappe erpnext; do \
			(cd apps/$$app && git fetch -q && git checkout -q $(FRAPPE_BRANCH) && git pull -q && \
				echo "$$app: $$(git describe --tags --always)"); \
		done && \
		./env/bin/pip install -q -e apps/frappe -e apps/erpnext && \
		bench --site $(SITE_NAME) migrate && \
		bench version | grep -E "^(frappe|erpnext) " \
	'

ci-upgrade-test: ## Simulate a Frappe + ERPNext stable upgrade and re-run tests (run in CI)
	@$(MAKE) dev-upgrade
	@$(MAKE) test

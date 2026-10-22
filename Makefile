.PHONY: dev test demo reset preflight
dev:
	uvicorn app.main:app --reload --port 8000
test:
	python -m pytest -q
demo:
	@echo "Open http://localhost:8000 after 'make dev'. Follow the 7-minute script in docs/BUILD_PLAN.md."
reset:  # deletes every company, person and request in the local data directory (default .deskhand)
	rm -rf .deskhand deskhand.db

check-viasocket:  # skill's check script: needs network and node; not part of `make test` (tests make no network calls)
	curl -fsSL https://flow.viasocket.com/viasocket-check.mjs -o viasocket-check.mjs && node viasocket-check.mjs app

preflight:  # per company: is delivery set up? Prints no ids, secrets or addresses. Uses DESKHAND_DATA_DIR (default .deskhand).
	@python -m app.preflight

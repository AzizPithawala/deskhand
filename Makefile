.PHONY: dev test demo reset
dev:
	uvicorn app.main:app --reload --port 8000
test:
	python -m pytest -q
demo:
	@echo "Open http://localhost:8000 after 'make dev'. Follow the 7-minute script in docs/BUILD_PLAN.md."
reset:
	rm -f deskhand.db

check-viasocket:  # skill's check script: needs network and node; not part of `make test` (tests make no network calls)
	curl -fsSL https://flow.viasocket.com/viasocket-check.mjs -o viasocket-check.mjs && node viasocket-check.mjs app

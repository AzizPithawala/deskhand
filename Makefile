.PHONY: dev test demo reset
dev:
	uvicorn app.main:app --reload --port 8000
test:
	python -m pytest -q
demo:
	@echo "Open http://localhost:8000 after 'make dev'. Follow the 7-minute script in docs/BUILD_PLAN.md."
reset:
	rm -f deskhand.db

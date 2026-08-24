.PHONY: install dev build serve test bench clean ship

install:
	pip install -r requirements.txt
	cd frontend && npm install

build:
	cd frontend && npm run build

dev:
	@echo "Run these in two terminals:"
	@echo "  uvicorn backend.main:app --reload --port 8000"
	@echo "  cd frontend && npm run dev"

serve: build
	uvicorn backend.main:app --host 0.0.0.0 --port 8000

test:
	python -m pytest tests/ -v

bench:
	python scripts/benchmark.py

clean:
	rm -rf frontend/dist data/app.db* .pytest_cache
	find . -name __pycache__ -type d -exec rm -rf {} +

# Run before every push: rebuild the committed bundle, then prove it works.
ship: build
	python -m pytest tests/ -q
	@echo "Bundle rebuilt and tests green. Safe to commit and push."

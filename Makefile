.PHONY: test lint test-backend test-frontend build-backend build-frontend

# Run tests separately (prevents namespace conflict between backend/app.py and frontend/app.py)
test-backend:
	DATABASE_URL=sqlite:///:memory: pytest backend/tests/ -v

test-frontend:
	pytest frontend/tests/ -v

test: test-backend test-frontend

# Lint
lint-backend:
	flake8 backend/ --config=setup.cfg

lint-frontend:
	flake8 frontend/ --config=setup.cfg

lint: lint-backend lint-frontend

# Docker builds (local check)
build-backend:
	docker build -t interviewsync-backend ./backend

build-frontend:
	docker build -t interviewsync-frontend ./frontend

build: build-backend build-frontend

# Local dev
up:
	docker compose up --build

down:
	docker compose down

down-v:
	docker compose down -v

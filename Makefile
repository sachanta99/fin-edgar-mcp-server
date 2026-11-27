.PHONY: install lint format typecheck test live check run

install:
	uv sync --extra dev

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check --fix .

typecheck:
	uv run mypy

test:
	uv run pytest --cov --cov-report=term

live:
	uv run pytest -m live

check: lint typecheck test

run:
	uv run fin-edgar-mcp

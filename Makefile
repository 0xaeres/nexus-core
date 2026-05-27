.PHONY: help install services-up services-down observability-up docker-up docker-down embedder reranker light-llm api dev test test-live-e2e lint format clean

PIDDIR := .pids

help:
	@echo "Nexus dev orchestration"
	@echo ""
	@echo "  make install        — uv sync (Python deps)"
	@echo "  make services-up    — bring up Qdrant + host embedder/reranker"
	@echo "  make observability-up — optional: bring up Langfuse"
	@echo "  make services-down  — stop everything"
	@echo "  make api            — run FastAPI dev server"
	@echo "  make dev            — services-up + api (one shot)"
	@echo "  make light-llm      — optional: start local Ollama for light model"
	@echo "  make test           — pytest"
	@echo "  make test-live-e2e  — real live backend E2E (requires NEXUS_LIVE_E2E=1)"
	@echo "  make lint           — ruff check"
	@echo "  make format         — ruff format"

install:
	uv sync

# ---------------------------------------------------------------- Infra
docker-up:
	docker compose up -d qdrant

docker-down:
	docker compose down

observability-up:
	docker compose --profile observability up -d langfuse

# ------------------------------------------------------ Host LLM services
$(PIDDIR):
	@mkdir -p $(PIDDIR)

embedder: $(PIDDIR) logs
	@if [ -f $(PIDDIR)/embedder.pid ] && kill -0 $$(cat $(PIDDIR)/embedder.pid) 2>/dev/null; then \
		echo "embedder already running (pid=$$(cat $(PIDDIR)/embedder.pid))"; \
	else \
		nohup ./scripts/serve-embedder.sh >logs/embedder.log 2>&1 & echo $$! >$(PIDDIR)/embedder.pid; \
		echo "embedder started (pid=$$(cat $(PIDDIR)/embedder.pid))"; \
	fi

reranker: $(PIDDIR) logs
	@if [ -f $(PIDDIR)/reranker.pid ] && kill -0 $$(cat $(PIDDIR)/reranker.pid) 2>/dev/null; then \
		echo "reranker already running (pid=$$(cat $(PIDDIR)/reranker.pid))"; \
	else \
		nohup ./scripts/serve-reranker.sh >logs/reranker.log 2>&1 & echo $$! >$(PIDDIR)/reranker.pid; \
		echo "reranker started (pid=$$(cat $(PIDDIR)/reranker.pid))"; \
	fi

light-llm:
	@./scripts/serve-light-llm.sh

logs:
	@mkdir -p logs

services-up: logs docker-up embedder reranker
	@echo ""
	@echo "✓ Services up:"
	@echo "  Qdrant   http://localhost:6333"
	@echo "  Embedder http://localhost:8080"
	@echo "  Reranker http://localhost:8081"

services-down: docker-down
	@for svc in embedder reranker; do \
		if [ -f $(PIDDIR)/$$svc.pid ]; then \
			kill $$(cat $(PIDDIR)/$$svc.pid) 2>/dev/null || true; \
			rm $(PIDDIR)/$$svc.pid; \
			echo "stopped $$svc"; \
		fi \
	done

# ---------------------------------------------------------------- App
api:
	uv run uvicorn nexus.api.app:app --reload --port 8000

dev: services-up api

# ---------------------------------------------------------------- Quality
test:
	uv run pytest

test-live-e2e:
	NEXUS_LIVE_E2E=1 uv run pytest -q -m live_e2e

lint:
	uv run ruff check nexus tests

format:
	uv run ruff format nexus tests

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info

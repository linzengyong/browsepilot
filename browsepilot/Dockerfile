FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --all-extras --frozen --no-dev --no-install-project
COPY src ./src
COPY eval ./eval
COPY frontend-vue ./frontend-vue
RUN uv sync --all-extras --frozen --no-dev

FROM python:3.13-slim-bookworm
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY eval ./eval
COPY frontend-vue ./frontend-vue
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8001
CMD ["uvicorn", "browsepilot.app.main:app", "--host", "0.0.0.0", "--port", "8001"]

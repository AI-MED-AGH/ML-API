FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/

RUN useradd --system --uid 10001 router
USER router

EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "src.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

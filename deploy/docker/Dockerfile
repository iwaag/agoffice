FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/services/api/src:/app/packages/domain/src:/app/packages/infra/src

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock* /app/

RUN uv sync --frozen --no-install-project

COPY ./services /app/services
COPY ./packages /app/packages
COPY ./deploy/k8s/remote-config.yaml /app/deploy/k8s/remote-config.yaml

EXPOSE 8000

# Define the command to run the application when the container starts
CMD ["uv", "run", "uvicorn", "agoffice_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM python:3.12-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/services/worker/src \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @openai/codex @anthropic-ai/claude-code

RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock /app/

RUN uv sync --frozen --no-dev

COPY ./services/worker/src /app/services/worker/src

EXPOSE 8000

CMD ["uvicorn", "agcode_worker.main:combined_app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

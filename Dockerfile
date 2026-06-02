# ── Stage 1: slim Python base ───────────────────────────────
FROM python:3.12-slim AS base

LABEL maintainer="Md Asif"
LABEL description="Notion → Google Docs sync pipeline"

WORKDIR /app

# Prevent Python from writing .pyc files & enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ── Stage 2: install dependencies ───────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 3: copy application code ─────────────────────────
COPY . .

# ── Entrypoint ──────────────────────────────────────────────
# Mount your .env (or pass env vars via docker run --env-file)
# Mount credentials.json and token.json for Google auth.
#
#   docker run --env-file .env \
#       -v $(pwd)/credentials.json:/app/credentials.json \
#       -v $(pwd)/token.json:/app/token.json \
#       -v $(pwd)/state.json:/app/state.json \
#       notion-to-gdocs
#
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD find /tmp/healthy -mmin -200 || exit 1

CMD ["python", "main.py"]

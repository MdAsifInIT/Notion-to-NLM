# ── Stage 1: slim Python base ───────────────────────────────
FROM python:3.12-slim AS base

LABEL maintainer="Md Asif"
LABEL description="Notion → Google Docs sync pipeline"

WORKDIR /app

# Prevent Python from writing .pyc files & enable unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEALTH_FILE=/tmp/healthy

# ── Stage 2: install dependencies ───────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Stage 3: copy application code ─────────────────────────
RUN useradd --create-home --shell /usr/sbin/nologin appuser
COPY --chown=appuser:appuser . .
RUN chown appuser:appuser /app
USER appuser

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
  CMD test -n "$(find "$HEALTH_FILE" -mmin -200 -type f 2>/dev/null)" || exit 1

CMD ["python", "main.py"]

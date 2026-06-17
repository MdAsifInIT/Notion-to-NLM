#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
#  deploy.sh — Bootstrap & run the Notion → Google Docs pipeline
# ──────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

# ── Colours ──────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Colour

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# ── 1. Check Python ─────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    error "python3 is not installed or not in PATH."
    exit 1
fi

# ── 2. Create / reuse virtual environment ───────────────────
if [ ! -d "$VENV_DIR" ]; then
    info "Creating virtual environment in ${VENV_DIR} …"
    python3 -m venv "$VENV_DIR"
fi

# ── 3. Install / upgrade dependencies ──────────────────────
info "Installing dependencies …"
"$PIP" install --quiet --upgrade pip
"$PIP" install --quiet -r requirements.txt

# ── 4. Ensure runtime configuration exists ─────────────────
if [ ! -f ".env" ]; then
    warn ".env file not found.  Copying .env.example → .env"
    cp .env.example .env
    warn "Please edit .env with your actual credentials before running again."
    exit 0
fi

if [ ! -r ".env" ]; then
    error ".env exists but is not readable."
    exit 1
fi

if [ -f "credentials.json" ] && [ ! -r "credentials.json" ]; then
    error "credentials.json exists but is not readable."
    exit 1
fi

mkdir -p "$(dirname "${STATE_FILE:-state.json}")" "$(dirname "${METRICS_FILE:-metrics.json}")"

# ── 5. Run the pipeline ────────────────────────────────────
info "Launching pipeline …"
"$PYTHON" main.py
info "Done."

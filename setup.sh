#!/usr/bin/env bash
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SKILL_DIR/venv"

echo "=== contract-drafting setup ==="

# Require Python >= 3.12
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 not found on PATH. Python >= 3.12 is required." >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    echo "ERROR: Python >= 3.12 is required (found $(python3 --version 2>&1))." >&2
    echo "Install Python 3.12+ and re-run setup.sh." >&2
    exit 1
fi
echo "Python: $(python3 --version 2>&1)"

# Create venv if missing
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"
fi

# Install dependencies
echo "Installing dependencies..."
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$SKILL_DIR/requirements.txt"

# Install Node.js build dependencies for Concerto schema generation
if command -v node &> /dev/null; then
    echo "Node.js: $(node --version)"
    if [ -f "$SKILL_DIR/package.json" ]; then
        echo "Installing Concerto build dependencies..."
        (cd "$SKILL_DIR" && npm install --quiet 2>/dev/null)
    fi
else
    echo "NOTE: Node.js not found. Schema generation requires Node.js. Pre-generated schema.json is committed to the repo."
fi

echo "=== Setup complete ==="

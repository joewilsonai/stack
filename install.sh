#!/bin/bash
# Stack install — venv, deps, and deny-config bootstrap
set -e
cd "$(dirname "$0")"

# 1. Python venv
if [ ! -d .venv ]; then
  echo "[install] creating .venv"
  python3 -m venv .venv
fi

echo "[install] upgrading pip + installing requirements"
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

# 2. Deny config — fail-closed; create from template if missing
DENY_DIR="$HOME/.config/stack"
DENY_FILE="$DENY_DIR/deny.json"
if [ ! -f "$DENY_FILE" ]; then
  mkdir -p "$DENY_DIR"
  cp deny.json.example "$DENY_FILE"
  echo "[install] wrote $DENY_FILE from template"
  echo "          edit this file to extend or restrict file access"
else
  echo "[install] $DENY_FILE exists, leaving as-is"
fi

# 3. Sanity check
.venv/bin/python tools.py

echo
echo "[install] done."
echo "  set OPENAI_API_KEY (export or .env), then run: ./run.sh"

#!/bin/bash
# Stack launcher
set -e

# Preserve the cwd we were invoked from — this becomes Stack's project root
# (what read_file / git tools / persona context all key off). If we cd'd to
# the script dir for everything, Stack would only see its own repo, not the
# user's working directory.
ORIG_CWD="$(pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -d "$SCRIPT_DIR/.venv" ]; then
  echo "venv missing — run $SCRIPT_DIR/install.sh first"
  exit 1
fi

# Source .env from the SCRIPT dir (where it lives), but stay in ORIG_CWD
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a; . "$SCRIPT_DIR/.env"; set +a
fi

if [ -z "$OPENAI_API_KEY" ]; then
  echo "OPENAI_API_KEY not set. Export it or add to $SCRIPT_DIR/.env"
  exit 1
fi

export STACK_VOICE="${VOICE:-cedar}"
export STACK_MODEL="${MODEL:-gpt-realtime-2}"

echo "─── stack ───────────────────────────────────"
echo " model:    $STACK_MODEL"
echo " voice:    $STACK_VOICE   (override: VOICE=ash ./run.sh)"
echo " project:  $ORIG_CWD"
echo " ctrl-c to exit"
echo "─────────────────────────────────────────────"

# Run from ORIG_CWD so Path.cwd() inside Stack = the user's project
cd "$ORIG_CWD"
exec "$SCRIPT_DIR/.venv/bin/python" "$SCRIPT_DIR/client.py"

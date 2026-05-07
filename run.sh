#!/bin/bash
# Stack launcher
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "venv missing — run ./install.sh first"
  exit 1
fi

# Always source .env if present (loads OPENAI_API_KEY + any STACK_* overrides
# like STACK_INPUT_DEVICE, STACK_VOICE, etc.). Existing env vars take precedence.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ -z "$OPENAI_API_KEY" ]; then
  echo "OPENAI_API_KEY not set. Export it or add to .env"
  exit 1
fi

export STACK_VOICE="${VOICE:-cedar}"
export STACK_MODEL="${MODEL:-gpt-realtime-2}"

echo "─── stack ───────────────────────────────────"
echo " model: $STACK_MODEL"
echo " voice: $STACK_VOICE   (override: VOICE=ash ./run.sh)"
echo " ctrl-c to exit"
echo "─────────────────────────────────────────────"

exec .venv/bin/python client.py

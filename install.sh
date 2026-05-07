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

# 4. Symlink the `stack` command to a directory on PATH so you can run it
#    from any cwd. Tries common locations; user can override with STACK_BIN_DIR.
SCRIPT_DIR="$(pwd)"
TARGET="$SCRIPT_DIR/stack"
BIN_CANDIDATES=("${STACK_BIN_DIR:-}" "$HOME/.local/bin" "/usr/local/bin")
INSTALLED=""
for d in "${BIN_CANDIDATES[@]}"; do
  [ -z "$d" ] && continue
  if [ ! -d "$d" ]; then
    # try to create user-local; skip system dirs we can't make
    if [[ "$d" == "$HOME/"* ]]; then
      mkdir -p "$d" 2>/dev/null || continue
    else
      continue
    fi
  fi
  if [ -w "$d" ]; then
    ln -sf "$TARGET" "$d/stack"
    INSTALLED="$d/stack"
    break
  fi
done
if [ -n "$INSTALLED" ]; then
  echo "[install] symlinked: $INSTALLED -> $TARGET"
  case ":$PATH:" in
    *":$(dirname "$INSTALLED"):"*) ;;
    *) echo "[install]   note: $(dirname "$INSTALLED") is not in your PATH; add it to your shell rc to use 'stack' globally" ;;
  esac
else
  echo "[install] couldn't auto-install 'stack' to PATH. Run from the repo dir as ./stack, or symlink manually:"
  echo "          ln -sf $TARGET /usr/local/bin/stack"
fi

echo
echo "[install] done."
echo "  set OPENAI_API_KEY (export or .env in $SCRIPT_DIR), then run: stack"

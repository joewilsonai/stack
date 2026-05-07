"""Shared runtime state for Stack: mode (quiet/pair/roast).

Lives in its own module so client.py, tools.py, and watcher.py can all
read/mutate the same value without circular imports. Also persists the
mode into the pet's state file so the pet ring color reflects the mode.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

VALID_MODES = ("quiet", "pair", "roast")

_current_mode = os.environ.get("STACK_MODE", "pair").strip().lower()
if _current_mode not in VALID_MODES:
    _current_mode = "pair"

PET_STATE_FILE = (
    Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    / "stack" / "pet_state.json"
)


def get_mode() -> str:
    return _current_mode


def set_mode(mode: str) -> bool:
    """Returns True if the mode changed, False if invalid or no-op."""
    global _current_mode
    mode = (mode or "").strip().lower()
    if mode not in VALID_MODES:
        return False
    if mode == _current_mode:
        return True
    _current_mode = mode
    # Patch the pet state file's "mode" field so the pet's ring color updates
    try:
        if PET_STATE_FILE.exists():
            data = json.loads(PET_STATE_FILE.read_text())
        else:
            data = {"state": "idle", "tool": None}
        data["mode"] = mode
        tmp = PET_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(PET_STATE_FILE)
    except Exception:
        pass
    return True

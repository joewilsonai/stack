"""Stack watcher — polls neighboring tmux/cmux panes and injects observations.

Phase 1: makes Stack actually pair-programmer-ish. Without this, Stack is
just a voice agent in a pane talking in isolation. With this, Stack sees
test failures, build output, git commands, etc. happening in your shell
pane and can speak up about them.

Architecture:
- Polls non-self panes every POLL_SECONDS via cmux/tmux capture-pane
- Diffs against previous capture per pane
- If new lines exceed MIN_NEW_LINES, queues an observation
- Caller (client.py) drains the queue and injects each observation as
  a system-role message into the realtime conversation, then fires a
  response.create with 'speak only if useful' instructions
- Throttled to at most one proactive observation per THROTTLE_SECONDS
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

POLL_SECONDS = int(os.environ.get("STACK_WATCH_POLL_SEC", "30"))
MIN_NEW_LINES = int(os.environ.get("STACK_WATCH_MIN_LINES", "3"))
THROTTLE_SECONDS = int(os.environ.get("STACK_WATCH_THROTTLE_SEC", "60"))
CAPTURE_LINES = int(os.environ.get("STACK_WATCH_LINES", "200"))
WATCH_DISABLED = os.environ.get("STACK_WATCH", "1") == "0"

# Ignore lines that look like a shell prompt redraw — these spam the diff
PROMPT_LINE_RE = re.compile(r"^\s*[❯➜>$#%]\s*$|^\s*\[.*\]\s*[❯>$#%]\s*$")

CMUX_BIN = os.environ.get("CMUX_BUNDLED_CLI_PATH") or shutil.which("cmux")
TMUX_BIN = shutil.which("tmux")


def _is_cmux() -> bool:
    return bool(CMUX_BIN and os.environ.get("CMUX_WORKSPACE_ID"))


def _is_tmux() -> bool:
    return bool(TMUX_BIN and os.environ.get("TMUX"))


class PaneInfo:
    __slots__ = ("ref", "title", "is_self")

    def __init__(self, ref: str, title: str = "", is_self: bool = False):
        self.ref = ref
        self.title = title
        self.is_self = is_self

    def __repr__(self):
        return f"Pane({self.ref}, self={self.is_self})"


def _list_panes_cmux() -> list[PaneInfo]:
    """List cmux surfaces in the current workspace. Marks our own pane as is_self.

    Self-detection priority:
      1. STACK_SELF_SURFACE env var (set by the launcher to the new pane's ref) — most reliable
      2. CMUX_SURFACE_ID UUID matched in --id-format both output
      3. CMUX_PANEL_ID UUID matched in --id-format both output
    """
    self_ref = os.environ.get("STACK_SELF_SURFACE", "").strip()
    self_surface = os.environ.get("CMUX_SURFACE_ID", "")
    self_panel = os.environ.get("CMUX_PANEL_ID", "")
    try:
        out = subprocess.run(
            [CMUX_BIN, "list-panels", "--id-format", "both"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    if out.returncode != 0:
        # Fall back to default format
        out = subprocess.run(
            [CMUX_BIN, "list-panels"],
            capture_output=True, text=True, timeout=5,
        )
    if out.returncode != 0:
        return []

    panes: list[PaneInfo] = []
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r"(surface:\d+)", line)
        if not m:
            continue
        ref = m.group(1)
        tm = re.search(r'"([^"]*)"', line)
        title = tm.group(1) if tm else ""
        is_self = (
            (self_ref and ref == self_ref)
            or bool(self_surface and self_surface in line)
            or bool(self_panel and self_panel in line)
        )
        panes.append(PaneInfo(ref=ref, title=title, is_self=is_self))
    return panes


def _list_panes_tmux() -> list[PaneInfo]:
    """List tmux panes in the current session."""
    self_pane = os.environ.get("TMUX_PANE", "")
    try:
        out = subprocess.run(
            [TMUX_BIN, "list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    panes: list[PaneInfo] = []
    for line in out.stdout.splitlines():
        if "\t" in line:
            ref, title = line.split("\t", 1)
        else:
            ref, title = line, ""
        is_self = (ref == self_pane)
        panes.append(PaneInfo(ref=ref, title=title, is_self=is_self))
    return panes


def list_panes() -> list[PaneInfo]:
    if _is_cmux():
        return _list_panes_cmux()
    if _is_tmux():
        return _list_panes_tmux()
    return []


def _capture_cmux(ref: str) -> Optional[str]:
    try:
        out = subprocess.run(
            [CMUX_BIN, "capture-pane", "--surface", ref, "--lines", str(CAPTURE_LINES)],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _capture_tmux(ref: str) -> Optional[str]:
    try:
        out = subprocess.run(
            [TMUX_BIN, "capture-pane", "-p", "-t", ref, "-S", f"-{CAPTURE_LINES}"],
            capture_output=True, text=True, timeout=8,
        )
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def capture_pane(ref: str) -> Optional[str]:
    if _is_cmux():
        return _capture_cmux(ref)
    if _is_tmux():
        return _capture_tmux(ref)
    return None


def _meaningful_diff(prev: str, current: str) -> list[str]:
    """Return new lines in `current` not in `prev`, filtering noise."""
    prev_lines = set(prev.splitlines())
    new_lines = []
    for line in current.splitlines():
        if line in prev_lines:
            continue
        # Skip empty / pure-whitespace
        stripped = line.strip()
        if not stripped:
            continue
        # Skip prompt-only lines
        if PROMPT_LINE_RE.match(line):
            continue
        # Skip box-drawing dividers
        if all(c in "─━━─═│┃┄ " for c in stripped):
            continue
        new_lines.append(line)
    return new_lines


class Watcher:
    """Polls non-self panes and yields observation strings via an asyncio queue.

    Caller awaits queue.get() to receive observations, then injects them into
    the realtime conversation as system messages.
    """

    def __init__(self, target_ref: Optional[str] = None):
        # target_ref is now a "scope to one pane" override. If unset, the
        # watcher follows ALL non-self panes and auto-discovers new ones
        # as they appear (e.g. when the developer splits a new test pane).
        self.target_ref = target_ref or os.environ.get("STACK_WATCH_PANE") or None
        # If a single STACK_WATCH_FOCUS env var is set explicitly, lock to that.
        # Otherwise treat STACK_WATCH_PANE as a hint and follow all panes.
        self.lock_to_target = os.environ.get("STACK_WATCH_LOCK", "0") == "1"
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.last_capture: dict[str, str] = {}
        # Per-pane throttle so multi-pane workspaces don't suppress events
        # from one pane while another is busy.
        self.last_emit_ms_per_pane: dict[str, float] = {}
        self.disabled = WATCH_DISABLED or (not _is_cmux() and not _is_tmux())
        self._panes_cached: list[PaneInfo] = []
        self._known_refs: set[str] = set()

    def status(self) -> str:
        if self.disabled:
            return "[watcher] disabled (STACK_WATCH=0 or no tmux/cmux detected)"
        backend = "cmux" if _is_cmux() else "tmux" if _is_tmux() else "none"
        return f"[watcher] backend={backend} poll={POLL_SECONDS}s min_lines={MIN_NEW_LINES} throttle={THROTTLE_SECONDS}s"

    def _select_panes(self) -> list[PaneInfo]:
        all_other = [p for p in list_panes() if not p.is_self]
        if self.lock_to_target and self.target_ref:
            # Strict single-pane focus
            matches = [p for p in all_other if p.ref == self.target_ref]
            return matches or all_other  # fall back if the locked pane vanished
        return all_other

    async def loop(self):
        if self.disabled:
            return
        # Prime initial captures so we don't fire on existing content
        for p in self._select_panes():
            cap = capture_pane(p.ref)
            if cap is not None:
                self.last_capture[p.ref] = cap
                self._known_refs.add(p.ref)
        if self._known_refs:
            print(f"[watcher] watching {len(self._known_refs)} pane(s): {', '.join(sorted(self._known_refs))}", flush=True)
        # Poll loop
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await self._tick()
            except Exception as e:
                print(f"[watcher] tick error: {e}", flush=True)

    async def _tick(self):
        panes = self._select_panes()
        current_refs = {p.ref for p in panes}

        # Auto-discover newly-appeared panes — prime them so we don't fire
        # on their pre-existing content the first time we see them
        new_refs = current_refs - self._known_refs
        for p in panes:
            if p.ref in new_refs:
                cap = capture_pane(p.ref)
                if cap is not None:
                    self.last_capture[p.ref] = cap
                self._known_refs.add(p.ref)
                print(f"[watcher] now watching new pane {p.ref}{f' \"{p.title}\"' if p.title else ''}", flush=True)

        # Drop tracking for panes that closed
        for stale_ref in self._known_refs - current_refs:
            self.last_capture.pop(stale_ref, None)
            self.last_emit_ms_per_pane.pop(stale_ref, None)
            self._known_refs.discard(stale_ref)
            print(f"[watcher] pane {stale_ref} closed", flush=True)

        # Diff loop — per-pane throttle so multi-pane work doesn't get suppressed
        for p in panes:
            if p.ref in new_refs:
                continue  # just primed; skip first diff
            cap = capture_pane(p.ref)
            if cap is None:
                continue
            prev = self.last_capture.get(p.ref, "")
            self.last_capture[p.ref] = cap
            new_lines = _meaningful_diff(prev, cap)
            if len(new_lines) < MIN_NEW_LINES:
                continue
            now_ms = time.monotonic() * 1000
            last_emit = self.last_emit_ms_per_pane.get(p.ref, 0.0)
            if (now_ms - last_emit) < THROTTLE_SECONDS * 1000:
                continue
            self.last_emit_ms_per_pane[p.ref] = now_ms
            preview = "\n".join(new_lines[-40:])
            obs = (
                f"PANE UPDATE ({p.ref}{f' \"{p.title}\"' if p.title else ''}):\n"
                f"{preview}"
            )
            await self.queue.put(obs)

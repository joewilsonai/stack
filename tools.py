"""Stack tool layer — file/git/web/tmux access for the gpt-realtime-2 model.

Privacy-critical. All file access goes through a resolved-path allowlist with a
fail-closed deny config. See PLAN.md (private) for the threat model.

v0 status:
- read_file: implemented with full path policy
- web_search: implemented (OpenAI Responses API)
- tmux_pane / git_status / git_diff / git_log / run_readonly: stub schemas only,
  Phase 4 fills in implementations.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ============================================================
# Path policy — fail-closed deny config + resolved-path allowlist
# ============================================================

DENY_CONFIG_PATH = Path.home() / ".config" / "stack" / "deny.json"


def _expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def _load_deny_config() -> dict:
    """Load deny config or refuse to start. Fail-closed by design."""
    if not DENY_CONFIG_PATH.exists():
        sys.stderr.write(
            f"FATAL: {DENY_CONFIG_PATH} missing.\n"
            f"Stack refuses to start without an explicit deny list.\n"
            f"Copy deny.json.example from the repo to {DENY_CONFIG_PATH} and edit it.\n"
        )
        sys.exit(2)
    try:
        cfg = json.loads(DENY_CONFIG_PATH.read_text())
    except json.JSONDecodeError as e:
        sys.stderr.write(f"FATAL: {DENY_CONFIG_PATH} is invalid JSON: {e}\n")
        sys.exit(2)
    if not isinstance(cfg.get("denied_roots"), list):
        sys.stderr.write(f"FATAL: {DENY_CONFIG_PATH} missing 'denied_roots' array.\n")
        sys.exit(2)
    return cfg


_CONFIG = _load_deny_config()
DENIED_ROOTS = [_expand(p).resolve() for p in _CONFIG.get("denied_roots", [])]
DENIED_EXTS = set(_CONFIG.get("denied_extensions", []))

CWD_ROOT = Path.cwd().resolve()  # frozen at startup
ALLOWED_FILES: set[Path] = set()  # exact-match allowed files (populated below)


def _path_allowed(path: Path) -> tuple[bool, str]:
    """Returns (allowed, reason). Default-deny."""
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as e:
        return False, f"path resolution failed: {e}"

    # 1. Denied extensions
    if resolved.suffix.lower() in DENIED_EXTS:
        return False, f"extension {resolved.suffix} is denied"

    # 2. Denied roots — runs first; catches symlink escapes
    for root in DENIED_ROOTS:
        try:
            if resolved == root or resolved.is_relative_to(root):
                return False, f"path resolves under denied root {root}"
        except AttributeError:
            # Python < 3.9 fallback
            try:
                resolved.relative_to(root)
                return False, f"path resolves under denied root {root}"
            except ValueError:
                pass

    # 3. Allowlist: exact-match files, then cwd descendants
    if resolved in ALLOWED_FILES:
        return True, "exact-allow"
    try:
        if resolved == CWD_ROOT or resolved.is_relative_to(CWD_ROOT):
            return True, "cwd-descendant"
    except AttributeError:
        try:
            resolved.relative_to(CWD_ROOT)
            return True, "cwd-descendant"
        except ValueError:
            pass

    return False, "outside allowed roots (default deny)"


def read_file(path: str) -> str:
    """Read a file under the resolved-path allowlist."""
    p = _expand(path)
    ok, reason = _path_allowed(p)
    if not ok:
        return f"[refused: {reason}]"
    try:
        text = p.read_text(errors="replace")
    except FileNotFoundError:
        return f"[not found: {p}]"
    except IsADirectoryError:
        return f"[is a directory: {p}]"
    except PermissionError:
        return f"[permission denied: {p}]"
    if len(text) > 30000:
        text = text[:30000] + f"\n\n[truncated at 30000 chars; full file is {len(text)} chars]"
    return text


# ============================================================
# Web search — OpenAI Responses API with hosted web_search
# ============================================================

def web_search(query: str) -> str:
    """Hosted web search. Returns synthesized answer + source URLs."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "[web_search: OPENAI_API_KEY not set]"
    body = json.dumps({
        "model": "gpt-5-nano",
        "tools": [{"type": "web_search"}],
        "input": query,
    }).encode()
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return f"[web_search HTTP {e.code}: {e.read()[:300].decode(errors='replace')}]"
    except Exception as e:
        return f"[web_search error: {e}]"

    text_parts = []
    citations = []
    for item in payload.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    text_parts.append(c.get("text", ""))
                    for ann in c.get("annotations", []) or []:
                        if ann.get("type") == "url_citation":
                            citations.append(ann.get("url", ""))
    text = "\n".join(t for t in text_parts if t).strip()
    if citations:
        text += "\n\nSources:\n" + "\n".join(f"- {u}" for u in citations[:8])
    return text or json.dumps(payload)[:2000]


# ============================================================
# Stubs for Phase 4 (tmux/git/run_readonly) — return clear "not yet" messages
# so the model knows the tool exists but can't actually call it in v0.
# ============================================================

def _not_implemented(name: str) -> str:
    return f"[{name}: not yet implemented in v0 — coming in Phase 4]"


def tmux_pane(name: str = "") -> str:
    return _not_implemented("tmux_pane")


def git_status() -> str:
    return _not_implemented("git_status")


def git_diff(staged: bool = False) -> str:
    return _not_implemented("git_diff")


def git_log(limit: int = 10) -> str:
    return _not_implemented("git_log")


def run_readonly(cmd: str) -> str:
    return _not_implemented("run_readonly")


# ============================================================
# Tool schemas exposed to gpt-realtime-2
# ============================================================

TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "read_file",
        "description": (
            "Read a file from the developer's project (the directory Stack was launched from). "
            "Subject to a strict allowlist + deny list. Refuses anything outside the project root "
            "or under denied paths (~/.ssh, ~/.aws, etc.)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "File path, relative to cwd or absolute"}},
            "required": ["path"],
        },
    },
    {
        "type": "function",
        "name": "web_search",
        "description": (
            "Live web search with citations. Use for current events, library docs, error messages, "
            "anything you don't have in your training. Takes ~5-10 seconds — say 'one sec' before calling."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "tmux_pane",
        "description": "Read recent output from a named tmux pane. (v0 stub — Phase 4)",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Pane name or id"}},
        },
    },
    {
        "type": "function",
        "name": "git_status",
        "description": "Show current git status of the project repo. (v0 stub — Phase 4)",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "git_diff",
        "description": "Show current git diff. (v0 stub — Phase 4)",
        "parameters": {
            "type": "object",
            "properties": {"staged": {"type": "boolean", "default": False}},
        },
    },
    {
        "type": "function",
        "name": "git_log",
        "description": "Show recent git log. (v0 stub — Phase 4)",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "type": "function",
        "name": "run_readonly",
        "description": "Run a whitelisted read-only command (ls/grep/find/head/tail/cat/etc.). (v0 stub — Phase 4)",
        "parameters": {
            "type": "object",
            "properties": {"cmd": {"type": "string"}},
            "required": ["cmd"],
        },
    },
]


DISPATCH = {
    "read_file": lambda args: read_file(args.get("path", "")),
    "web_search": lambda args: web_search(args.get("query", "")),
    "tmux_pane": lambda args: tmux_pane(args.get("name", "")),
    "git_status": lambda args: git_status(),
    "git_diff": lambda args: git_diff(args.get("staged", False)),
    "git_log": lambda args: git_log(args.get("limit", 10)),
    "run_readonly": lambda args: run_readonly(args.get("cmd", "")),
}


def dispatch(name: str, args_json: str) -> str:
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError:
        return f"[bad json args: {args_json}]"
    fn = DISPATCH.get(name)
    if not fn:
        return f"[unknown tool: {name}]"
    try:
        return fn(args)
    except Exception as e:
        return f"[{name} error: {e}]"


if __name__ == "__main__":
    print("Stack tools loaded.")
    print(f"  cwd root: {CWD_ROOT}")
    print(f"  denied roots: {len(DENIED_ROOTS)}")
    print(f"  denied extensions: {sorted(DENIED_EXTS)}")
    print(f"  tools: {', '.join(DISPATCH.keys())}")

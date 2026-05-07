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
import shutil
import subprocess
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


def _detect_project_root() -> Path:
    """Find the project root by walking up from cwd looking for common markers
    (.git, pyproject.toml, package.json, Cargo.toml, go.mod). Falls back to cwd.
    STACK_PROJECT_ROOT env var overrides detection if set."""
    override = os.environ.get("STACK_PROJECT_ROOT", "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        if p.exists():
            return p
    cwd = Path.cwd().resolve()
    current = cwd
    home = Path.home().resolve()
    markers = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "Gemfile")
    while True:
        for m in markers:
            if (current / m).exists():
                return current
        if current == home or current == current.parent:
            break
        current = current.parent
    return cwd


CWD_ROOT = _detect_project_root()
ALLOWED_FILES: set[Path] = set()


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

def _web_search(query: str, model: str, timeout: int) -> str:
    """Shared backend for both quick and deep search. Calls OpenAI Responses API
    with hosted web_search and synthesizes an answer + citations."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return "[web_search: OPENAI_API_KEY not set]"
    body = json.dumps({
        "model": model,
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
        with urllib.request.urlopen(req, timeout=timeout) as r:
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


# Quick search — fast, snappy, best for one-liner lookups.
QUICK_MODEL = os.environ.get("STACK_QUICK_MODEL", "gpt-5-nano")
QUICK_TIMEOUT = int(os.environ.get("STACK_QUICK_TIMEOUT", "30"))

# Deep search — slower, better synthesis, for research-heavy questions.
DEEP_MODEL = os.environ.get("STACK_DEEP_MODEL", "gpt-5-mini")
DEEP_TIMEOUT = int(os.environ.get("STACK_DEEP_TIMEOUT", "90"))


def web_search_quick(query: str) -> str:
    return _web_search(query, QUICK_MODEL, QUICK_TIMEOUT)


def web_search_deep(query: str) -> str:
    return _web_search(query, DEEP_MODEL, DEEP_TIMEOUT)


# ============================================================
# Stubs for Phase 4 (tmux/git/run_readonly) — return clear "not yet" messages
# so the model knows the tool exists but can't actually call it in v0.
# ============================================================

def _not_implemented(name: str) -> str:
    return f"[{name}: not yet implemented in v0 — coming in Phase 4]"


def tmux_pane(name: str = "") -> str:
    """Capture recent output from a tmux/cmux pane. Defaults to the pane Stack
    is watching (STACK_WATCH_PANE) — usually the developer's working pane."""
    target = name or os.environ.get("STACK_WATCH_PANE", "")
    if not target:
        return "[tmux_pane: no pane specified and STACK_WATCH_PANE not set — Stack was likely launched without a watch target]"
    try:
        # Local import to avoid circulars at module load
        from watcher import capture_pane
    except ImportError:
        return "[tmux_pane: watcher module not available]"
    out = capture_pane(target)
    if out is None:
        return f"[tmux_pane: failed to capture {target}]"
    if not out.strip():
        return f"[tmux_pane: {target} is empty]"
    # Cap output at 8000 chars
    if len(out) > 8000:
        out = "[truncated to last 8000 chars]\n" + out[-8000:]
    return f"=== {target} ===\n{out}"


def git_status() -> str:
    ok, out = _git(["status", "-sb"])
    if not ok:
        return out
    return out or "[clean working tree]"


def git_diff(staged: bool = False) -> str:
    args = ["diff", "--no-textconv", "--no-ext-diff", "--stat"]
    if staged:
        args.append("--cached")
    ok, out = _git(args, timeout=15)
    if not ok:
        return out
    if not out:
        return "[no diff]"
    if len(out) > 6000:
        return out[:6000] + "\n[truncated]"
    return out


def git_log(limit: int = 10) -> str:
    limit = max(1, min(limit, 50))
    ok, out = _git([
        "log", f"-{limit}", "--no-textconv",
        "--pretty=format:%h  %ad  %s  (%an)",
        "--date=short",
    ])
    if not ok:
        return out
    return out or "[no commits]"


def run_readonly(cmd: str) -> str:
    return _not_implemented("run_readonly")


def list_dir(path: str = ".") -> str:
    """List entries in a directory. Scoped to the same allowlist as read_file —
    refuses denied paths. Useful for Stack to explore the project tree."""
    p = _expand(path)
    if not p.is_absolute():
        p = (CWD_ROOT / p).resolve()
    ok, reason = _path_allowed(p)
    if not ok:
        return f"[refused: {reason}]"
    if not p.exists():
        return f"[not found: {p}]"
    if not p.is_dir():
        return f"[not a directory: {p}]"
    try:
        items = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except PermissionError:
        return f"[permission denied: {p}]"
    if not items:
        return f"=== {p} ===\n(empty)"
    lines = [f"=== {p} ==="]
    for item in items[:300]:
        name = item.name
        if name.startswith(".") and name not in (".env.example", ".gitignore"):
            continue  # hide dotfiles by default
        if item.is_symlink():
            lines.append(f"  {name}@ -> {os.readlink(item)}")
        elif item.is_dir():
            lines.append(f"  {name}/")
        else:
            try:
                sz = item.stat().st_size
                lines.append(f"  {name}  ({_humanize_size(sz)})")
            except OSError:
                lines.append(f"  {name}")
    if len(items) > 300:
        lines.append(f"  … {len(items) - 300} more")
    return "\n".join(lines)


_FIND_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".next", ".turbo", "dist", "build", ".cache", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "target", ".gradle",
}


def find_in_repo(name: str, max_results: int = 30) -> str:
    """Search the project tree for files/directories matching a name.
    Case-insensitive substring match. Honors the same deny list as read_file
    by pruning denied subtrees during traversal."""
    if not name:
        return "[find_in_repo: empty name]"
    name_low = name.lower()
    matches: list[str] = []

    for root, dirs, files in os.walk(CWD_ROOT):
        # Prune noisy dirs + dotdirs
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _FIND_SKIP_DIRS]
        # Prune denied subtrees
        root_path = Path(root)
        ok, _ = _path_allowed(root_path)
        if not ok:
            dirs[:] = []
            continue
        # Match dirs (also yields walking into them; we add the dir entry itself)
        for d in dirs:
            if name_low in d.lower():
                rel = (root_path / d).resolve()
                try:
                    matches.append(str(rel.relative_to(CWD_ROOT)) + "/")
                except ValueError:
                    matches.append(str(rel) + "/")
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break
        # Match files
        for f in files:
            if f.startswith("."):
                continue
            if name_low in f.lower():
                rel = (root_path / f).resolve()
                try:
                    matches.append(str(rel.relative_to(CWD_ROOT)))
                except ValueError:
                    matches.append(str(rel))
                if len(matches) >= max_results:
                    break
        if len(matches) >= max_results:
            break

    if not matches:
        return f"[find_in_repo: no matches for '{name}']"
    return f"=== matches for '{name}' ({len(matches)}) ===\n" + "\n".join(f"  {m}" for m in matches)


def _humanize_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _git(args: list[str], timeout: int = 10) -> tuple[bool, str]:
    """Run a hardened git command in CWD_ROOT. Returns (ok, output)."""
    env = os.environ.copy()
    env.update({
        "GIT_PAGER": "cat",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    })
    env.pop("GIT_EXTERNAL_DIFF", None)
    cmd = [
        "git", "--no-pager",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "diff.external=",
        "-c", "protocol.version=2",
    ] + args
    try:
        out = subprocess.run(
            cmd, cwd=str(CWD_ROOT), env=env,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"[git timeout after {timeout}s]"
    except FileNotFoundError:
        return False, "[git not installed]"
    except Exception as e:
        return False, f"[git error: {e}]"
    if out.returncode != 0:
        return False, f"[git exit {out.returncode}]\n{out.stderr.strip()[:1000]}"
    return True, out.stdout.strip()


def send_to_pane(text: str, name: str = "") -> str:
    """Type text into the developer's working pane WITHOUT pressing Enter.
    Use only when explicitly asked. Newlines are stripped to prevent
    accidental Enter (cmux interprets \\n as Enter)."""
    if not text:
        return "[send_to_pane: empty text]"
    target = name or os.environ.get("STACK_WATCH_PANE", "")
    if not target:
        return "[send_to_pane: no target pane]"

    # Strip newlines so we never press Enter automatically
    safe = text.replace("\r", "").replace("\n", " ")

    cmux_bin = os.environ.get("CMUX_BUNDLED_CLI_PATH") or shutil.which("cmux")
    if cmux_bin and os.environ.get("CMUX_WORKSPACE_ID"):
        try:
            subprocess.run(
                [cmux_bin, "send", "--surface", target, "--", safe],
                check=True, capture_output=True, timeout=5,
            )
            print(f"[send -> {target}] {safe[:120]}{'…' if len(safe) > 120 else ''}", flush=True)
            return f"typed (no enter): {safe[:80]}{'…' if len(safe) > 80 else ''}"
        except subprocess.CalledProcessError as e:
            return f"[send_to_pane: cmux send failed: {e.stderr.decode(errors='replace')[:200]}]"
        except Exception as e:
            return f"[send_to_pane: error: {e}]"

    tmux_bin = shutil.which("tmux")
    if tmux_bin and os.environ.get("TMUX"):
        try:
            subprocess.run(
                [tmux_bin, "send-keys", "-t", target, "-l", safe],
                check=True, capture_output=True, timeout=5,
            )
            print(f"[send -> {target}] {safe[:120]}", flush=True)
            return f"typed (no enter): {safe[:80]}"
        except Exception as e:
            return f"[send_to_pane: tmux send-keys failed: {e}]"

    return "[send_to_pane: no cmux/tmux backend detected]"


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
        "name": "web_search_quick",
        "description": (
            "FAST web search (~5-15 seconds, gpt-5-nano backend). Use for: simple lookups, "
            "definitions, one-line answers, current events ('did X happen yet'), package versions, "
            "error messages, library docs lookups. Say 'one sec' before calling. Pick this by default — "
            "only escalate to web_search_deep when the question genuinely needs multi-source synthesis."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "type": "function",
        "name": "web_search_deep",
        "description": (
            "DEEP web search (~30-60 seconds, gpt-5-mini backend). Use for: research-heavy questions, "
            "comparing multiple options ('what's the best X for Y'), 'how does X work under the hood', "
            "explainers that need to weigh sources, anything Joe explicitly says 'do a deep dive on' or "
            "'research thoroughly'. Say 'this'll take a minute' before calling. NEVER use this for a "
            "simple one-liner question — use web_search_quick instead."
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
        "description": (
            "Read recent terminal output from the developer's working pane. "
            "Call this when the user asks 'what do you see' / 'check my terminal' / "
            "'didn't you see X' or whenever you need to know what they're looking at. "
            "By default reads the pane Stack is watching — pass no arguments. "
            "Only pass `name` if you specifically need to read a different pane."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Optional pane ref/UUID. Leave empty to read the watched pane.",
                },
            },
        },
    },
    {
        "type": "function",
        "name": "find_in_repo",
        "description": (
            "Search the project tree for files or directories whose name contains "
            "a substring (case-insensitive). Use this when the developer asks "
            "'do you see X' / 'is X in the repo' / 'where is the X file' / "
            "'find me X'. Always try this BEFORE saying you can't find something."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name or substring to search for"},
                "max_results": {"type": "integer", "default": 30},
            },
            "required": ["name"],
        },
    },
    {
        "type": "function",
        "name": "list_dir",
        "description": (
            "List entries in a directory of the project tree. Use this to explore "
            "what's in the repo. Defaults to the project root. Hidden dotfiles are "
            "filtered. Path is relative to project root unless absolute."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory path. Defaults to '.'", "default": "."},
            },
        },
    },
    {
        "type": "function",
        "name": "git_status",
        "description": "Run `git status -sb` in the project repo and return the output. Use this when the developer asks 'what's changed' / 'what's the state of git' / 'am I behind'.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "type": "function",
        "name": "git_diff",
        "description": "Run `git diff --stat` (or `--cached` when staged=true) and return the diff stat. Use to summarize changes.",
        "parameters": {
            "type": "object",
            "properties": {"staged": {"type": "boolean", "default": False}},
        },
    },
    {
        "type": "function",
        "name": "git_log",
        "description": "Recent commits in the project repo. Default 10, max 50.",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "type": "function",
        "name": "send_to_pane",
        "description": (
            "Type text into the developer's working pane (e.g. their Claude Code prompt) "
            "WITHOUT pressing Enter. The developer presses Enter themselves. "
            "Use ONLY when the developer explicitly asks: 'send to Claude' / 'type that for me' / "
            "'inject this' / 'tell Claude to X' / 'put X in the prompt' / 'dictate this'. "
            "Do NOT use proactively. Do NOT type your own thoughts as if they were the developer's. "
            "After typing, briefly tell the developer what was typed and that they can press Enter to send. "
            "Newlines in the text are stripped to prevent accidental submission."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Exact text to type. Will not press Enter.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional pane ref. Defaults to the watched pane.",
                },
            },
            "required": ["text"],
        },
    },
]


DISPATCH = {
    "read_file": lambda args: read_file(args.get("path", "")),
    "list_dir": lambda args: list_dir(args.get("path", ".")),
    "find_in_repo": lambda args: find_in_repo(args.get("name", ""), args.get("max_results", 30)),
    "web_search_quick": lambda args: web_search_quick(args.get("query", "")),
    "web_search_deep": lambda args: web_search_deep(args.get("query", "")),
    "tmux_pane": lambda args: tmux_pane(args.get("name", "")),
    "git_status": lambda args: git_status(),
    "git_diff": lambda args: git_diff(args.get("staged", False)),
    "git_log": lambda args: git_log(args.get("limit", 10)),
    "send_to_pane": lambda args: send_to_pane(args.get("text", ""), args.get("name", "")),
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

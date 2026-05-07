"""Pane-diff classifier — labels terminal output noise into noteworthy events.

Used by client.py's watcher consumer: instead of injecting every diff as
a system message, classify first. Drop routine output, surface real
events with a one-line summary and severity. Cheap model pass (~1-2s)
keeps it fast enough for ambient use.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

CLASSIFIER_MODEL = os.environ.get("STACK_CLASSIFIER_MODEL", "gpt-5-nano")
CLASSIFIER_TIMEOUT = int(os.environ.get("STACK_CLASSIFIER_TIMEOUT", "20"))

CLASSIFIER_SYSTEM = """You classify terminal output diffs (the new lines that appeared in a developer's terminal pane) into structured events. Output ONE JSON object only — no prose, no markdown fences.

Schema (all fields required):
{
  "noteworthy": boolean,
  "category": "test_failure" | "test_pass" | "build_break" | "build_success" | "error" | "git_commit" | "git_change" | "package_install" | "long_run" | "routine" | "other",
  "summary": string,           // one line, max 90 chars, plain English, present-tense
  "severity": "low" | "medium" | "high"
}

Rules:
- noteworthy=true for: test failures, build breaks, exceptions/tracebacks, errors, successful tests after a failure, completed migrations, package install failures, commits.
- noteworthy=false for: ls/cd/pwd/cat, prompt redraws, plain command echoes, clean `git status` with no changes, simple successful command output that's part of routine flow.
- severity=high for: build breaks, exceptions, repeated errors, fatal failures.
- severity=medium for: test failures, warnings, single errors, package install failures.
- severity=low for: passes, minor info, completion confirmations.
- summary is the ONE line you'd tell a developer to know what happened. No quoting full lines back; paraphrase. Max 90 chars.
- If you can't tell what happened, return {"noteworthy": false, "category": "routine", "summary": "unclear output", "severity": "low"}.

Examples:

Diff:
FAILED test_login.py::test_invalid_password
1 failed, 14 passed in 0.42s

Output:
{"noteworthy":true,"category":"test_failure","summary":"test_login.py::test_invalid_password failed (1/15)","severity":"medium"}

Diff:
TypeError: Cannot read property 'x' of undefined
  at handler (server.js:42:18)
  at processRequest (server.js:88:5)

Output:
{"noteworthy":true,"category":"error","summary":"TypeError on undefined .x in server.js:42","severity":"high"}

Diff:
$ ls
README.md  src  tests  package.json

Output:
{"noteworthy":false,"category":"routine","summary":"directory listing","severity":"low"}

Diff:
[main 4f9a2cb] add user auth flow
 3 files changed, 142 insertions(+), 8 deletions(-)

Output:
{"noteworthy":true,"category":"git_commit","summary":"committed 'add user auth flow' (3 files, +142 -8)","severity":"low"}

Diff:
PASS test_auth.py (3.8s)
PASS test_login.py (1.2s)
4 passed in 5.1s

Output:
{"noteworthy":true,"category":"test_pass","summary":"all 4 tests passing","severity":"low"}
"""


def classify(diff_text: str) -> dict:
    """Returns dict with keys noteworthy/category/summary/severity. Falls back
    to a 'routine' label on any error so the pipeline never blocks on classifier
    issues."""
    if not diff_text or not diff_text.strip():
        return {"noteworthy": False, "category": "routine", "summary": "empty diff", "severity": "low"}

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"noteworthy": False, "category": "other", "summary": "classifier offline (no key)", "severity": "low"}

    body = json.dumps({
        "model": CLASSIFIER_MODEL,
        "input": [
            {"role": "system", "content": CLASSIFIER_SYSTEM},
            {"role": "user", "content": diff_text[:4000]},
        ],
        # Force JSON-only output
        "text": {"format": {"type": "json_object"}},
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
        with urllib.request.urlopen(req, timeout=CLASSIFIER_TIMEOUT) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {
            "noteworthy": False, "category": "other",
            "summary": f"classifier HTTP {e.code}", "severity": "low",
        }
    except Exception as e:
        return {
            "noteworthy": False, "category": "other",
            "summary": f"classifier error: {str(e)[:60]}", "severity": "low",
        }

    text = ""
    for item in payload.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") in ("output_text", "text"):
                    text += c.get("text", "")
    text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {
            "noteworthy": False, "category": "other",
            "summary": "classifier returned non-JSON", "severity": "low",
        }

    # Normalize / sanitize
    return {
        "noteworthy": bool(data.get("noteworthy", False)),
        "category": str(data.get("category", "other"))[:32],
        "summary": str(data.get("summary", ""))[:200],
        "severity": str(data.get("severity", "low")).lower(),
    }


# Map classifier categories → pet reaction states
PET_REACTION = {
    "test_failure": "alarmed",
    "build_break": "alarmed",
    "error": "alarmed",
    "test_pass": "happy",
    "build_success": "happy",
    "git_commit": "happy",
}

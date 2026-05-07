# Stack

> *The voice in your stack.*

Voice pair programmer for the terminal. Built on OpenAI's [`gpt-realtime-2`](https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/) (released May 7, 2026). Lives in a tmux/cmux pane, watches your other panes, talks back.

**Status:** v0 scaffold. Not yet feature-complete.

## What it is

Cursor and Copilot are silent text completion. Stack is *ambient voice presence*. It lives next to your editor in a separate terminal pane, watches commands run in your shell pane, and speaks up when there's something worth saying.

| | Cursor / Copilot | Stack |
|---|---|---|
| modality | silent text completion | ambient voice |
| invocation | reactive (you type) | proactive (watches panes) |
| personality | none | opinionated character |
| location | inside the IDE | terminal-native, IDE-agnostic |

## How it works

```
tmux session
├── pane 1: editor (neovim / vscode / whatever)
├── pane 2: shell (where commands run)
└── pane 3: stack ← voice agent + watcher
```

The Stack pane runs a Python client that:

1. Connects to OpenAI's GA Realtime API (`wss://api.openai.com/v1/realtime?model=gpt-realtime-2`)
2. Captures your microphone (24kHz PCM16) and streams to the server
3. Plays the model's voice replies back through your speakers
4. Polls neighboring tmux panes for new content and feeds observations into the conversation
5. Exposes a tool layer (`read_file`, `git_status`, `git_diff`, `web_search`, `tmux_pane`, `run_readonly`) the model can call

## Modes

- **quiet** — speak only when addressed
- **pair** *(default)* — interject on meaningful events (test failures, build breaks, unusual git status)
- **roast** — active push-back on rabbit holes

Switch by saying *"go quiet"* / *"switch to pair"* / *"roast me"*.

## Install

```bash
git clone https://github.com/PoliTwit1984/stack.git ~/stack
cd ~/stack
./install.sh         # creates venv, installs deps, writes ~/.config/stack/deny.json from template
```

Set your OpenAI key:

```bash
export OPENAI_API_KEY=sk-...
```

Run it:

```bash
./run.sh
```

## Privacy & file access

Stack reads files. By design, this is heavily constrained:

- **Allowed by default:** the directory you launch it from (your project repo).
- **Configurable extra reads:** edit `~/.config/stack/deny.json` to extend the deny list.
- **Hard-coded denies:** any path under your `~/.ssh`, `~/.aws`, `~/.config/gh`, etc. (default deny list shipped in `deny.json.example`).
- **Sessions stored outside the repo:** all transcripts go to `~/.local/state/stack/sessions/` so they're never accidentally committed.

If `~/.config/stack/deny.json` is missing or invalid, Stack refuses to start (fail-closed).

## Status

- [x] gpt-realtime-2 GA voice client
- [ ] tmux pane watcher
- [ ] Proactive interjection
- [ ] Mode toggles
- [ ] Tool layer (`tmux_pane`, `run_readonly`, `git_*`)
- [ ] Custom persona
- [ ] Install script

See open issues for current work.

## License

MIT

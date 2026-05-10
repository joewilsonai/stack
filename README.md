<div align="center">

# 🎙️ Stack

**The voice in your stack.**

Voice pair programmer for the terminal. Built on OpenAI's [`gpt-realtime-2`](https://openai.com/index/advancing-voice-intelligence-with-new-models-in-the-api/). Lives in a tmux/cmux pane, watches your other panes, talks back.

[![Status](https://img.shields.io/badge/Status-v0_scaffold-yellow)]()
[![Model](https://img.shields.io/badge/Model-gpt--realtime--2-000000?logo=openai)](https://openai.com)
[![License](https://img.shields.io/badge/License-MIT-blue)](#license)

</div>

---

## What it is

Cursor and Copilot are silent text completion. **Stack is ambient voice presence.** It lives next to your editor in a separate terminal pane, watches commands run in your shell pane, and speaks up when there's something worth saying.

|  | Cursor / Copilot | Stack |
|---|---|---|
| **Modality** | silent text completion | ambient voice |
| **Invocation** | reactive (you type) | proactive (watches panes) |
| **Personality** | none | opinionated character |
| **Location** | inside the IDE | terminal-native, IDE-agnostic |

## How it works

```
tmux session
├── pane 1: editor (neovim / vscode / whatever)
├── pane 2: shell (where commands run)
└── pane 3: stack ← voice agent + watcher
```

The Stack pane runs a Python client that:

1. **Connects** to OpenAI's GA Realtime API (`wss://api.openai.com/v1/realtime?model=gpt-realtime-2`)
2. **Captures your mic** (24kHz PCM16) and streams to the server
3. **Plays voice replies** back through your speakers
4. **Polls neighboring tmux panes** for new content and feeds observations into the conversation
5. **Exposes a tool layer** (`read_file`, `git_status`, `git_diff`, `web_search`, `tmux_pane`, `run_readonly`) the model can call

## Modes

- 🔇 **quiet** — speak only when addressed
- 🤝 **pair** *(default)* — interject on meaningful events (test failures, build breaks, unusual git status)
- 🔥 **roast** — active push-back on rabbit holes

Switch by saying *"go quiet"* / *"switch to pair"* / *"roast me"*.

## Install

```bash
git clone https://github.com/joewilsonai/stack ~/stack
cd ~/stack
./install.sh         # creates venv, installs deps, writes ~/.config/stack/deny.json
```

Set your OpenAI key:

```bash
echo "OPENAI_API_KEY=sk-..." >> ~/.config/stack/env
```

Open a tmux session and run:

```bash
stack pair
```

## Configuration

Stack reads `~/.config/stack/config.toml`:

```toml
[mic]
device = "default"
sample_rate = 24000

[panes]
watch = ["shell", "editor"]
poll_interval_ms = 500

[deny]
# commands stack won't run even via run_readonly
patterns = ["rm -rf", "git push --force", "sudo"]
```

## Status

⚠️ **v0 scaffold — not feature-complete.** The voice loop and pane-watching are working; tool dispatching, deny-list enforcement, and IDE integrations are in progress. Built in public — follow along on [Twitter](https://twitter.com/joewilsonai).

## License

MIT

"""Stack — voice pair programmer for the terminal.

Connects to OpenAI's GA Realtime API (gpt-realtime-2), captures mic audio, plays
audio replies, and exposes a tool layer the model can call to read files, query
git, etc.

Run: ./run.sh   (or .venv/bin/python client.py)
Ctrl-C to exit.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import websockets

API_KEY = os.environ.get("OPENAI_API_KEY")
if not API_KEY:
    print("FATAL: OPENAI_API_KEY not set", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))
from tools import TOOL_SCHEMAS, dispatch  # noqa: E402
from watcher import Watcher  # noqa: E402

MODEL = os.environ.get("STACK_MODEL", "gpt-realtime-2")
VOICE = os.environ.get("STACK_VOICE", "cedar")
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"

SAMPLE_RATE = 24000
CHANNELS = 1
INPUT_CHUNK_MS = 30
INPUT_CHUNK_SAMPLES = SAMPLE_RATE * INPUT_CHUNK_MS // 1000
OUTPUT_BLOCKSIZE = 1200

# Override audio devices via env vars. Accepts either an integer index or a
# substring of the device name (case-insensitive). If unset, sounddevice's
# default is used. Useful when macOS picks a webcam mic that's not capturing.
INPUT_DEVICE_OVERRIDE = os.environ.get("STACK_INPUT_DEVICE", "").strip() or None
OUTPUT_DEVICE_OVERRIDE = os.environ.get("STACK_OUTPUT_DEVICE", "").strip() or None


def _resolve_device(spec, kind):
    """Resolve env-var override to a sounddevice device id. Returns None for default."""
    if not spec:
        return None
    if spec.isdigit():
        return int(spec)
    spec_low = spec.lower()
    for i, d in enumerate(sd.query_devices()):
        ch = d["max_input_channels"] if kind == "input" else d["max_output_channels"]
        if ch > 0 and spec_low in d["name"].lower():
            return i
    print(f"[audio] {kind} device matching '{spec}' not found; using system default", file=sys.stderr)
    return None

# Half-duplex echo gate — suppress mic upload while speakers are playing the
# model's voice. Eliminates the VAD-triggered self-interruption loop on speakers.
SPEAKER_TAIL_MS = 280

# Volume-threshold barge-in: when the gate is active, a mic chunk with peak
# int16 amplitude above this threshold is allowed through anyway, treating it
# as a deliberate interrupt. Tune via STACK_INTERRUPT_THRESHOLD env var.
# Lower = easier to interrupt but more false-trips from speaker bleed.
# Higher = need to talk louder. Default 3000 covers most laptop-speaker setups.
INTERRUPT_THRESHOLD = int(os.environ.get("STACK_INTERRUPT_THRESHOLD", "3000"))

# Persona loaded from persona.md so it can be edited without touching code.
PERSONA_PATH = Path(__file__).parent / "persona.md"
SYSTEM_PROMPT = PERSONA_PATH.read_text() if PERSONA_PATH.exists() else "You are Stack."


PET_ENABLED = os.environ.get("STACK_PET", "1") != "0"
PET_STATE_FILE = (
    Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
    / "stack" / "pet_state.json"
)
PET_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)


def _write_pet_state(state: str, mode: str = "pair", tool: str | None = None):
    """Atomically write the pet's current state. Pet polls this file."""
    if not PET_ENABLED:
        return
    payload = json.dumps({"state": state, "mode": mode, "tool": tool, "ts": int(time.time() * 1000)})
    try:
        # Atomic write: write to temp + rename so the pet never reads a partial file
        tmp = PET_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(payload)
        tmp.replace(PET_STATE_FILE)
    except Exception:
        pass


def _session_dir() -> Path:
    """Collision-safe per-repo session directory under XDG state."""
    xdg_state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    repo_root = Path.cwd().resolve()
    repo_id = repo_root.name + "-" + hashlib.sha256(str(repo_root).encode()).hexdigest()[:12]
    d = Path(xdg_state) / "stack" / "sessions" / repo_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# Auto-resume: if the most recent session in this repo's session dir ended
# within RESUME_WINDOW_MIN minutes, replay the last RESUME_TURNS exchanges
# as a system-prompt context block. Pass --no-resume on the command line
# to force a fresh start.
RESUME_WINDOW_MIN = int(os.environ.get("STACK_RESUME_WINDOW_MIN", "30"))
RESUME_TURNS = int(os.environ.get("STACK_RESUME_TURNS", "10"))
RESUME_CHAR_BUDGET = int(os.environ.get("STACK_RESUME_CHARS", "2400"))


def _load_resume_context() -> tuple[str | None, str | None]:
    """Returns (resume_block, banner) or (None, None) if nothing to resume.

    resume_block: text to append to the system prompt (or None for fresh).
    banner:       short status line to print on startup (or None).
    """
    if "--no-resume" in sys.argv or os.environ.get("STACK_RESUME", "1") == "0":
        return None, None

    sessions = _session_dir()
    candidates = sorted(sessions.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        return None, None

    latest = candidates[0]
    age_sec = time.time() - latest.stat().st_mtime
    if age_sec > RESUME_WINDOW_MIN * 60:
        return None, None  # too old, fresh start

    # Parse the JSONL — extract user / stack turns in order
    turns: list[tuple[str, str]] = []  # (role, text)
    last_event_time: str | None = None
    try:
        with latest.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ev = rec.get("event")
                if ev == "user":
                    turns.append(("developer", rec.get("text", "")))
                    last_event_time = rec.get("ts", last_event_time)
                elif ev == "stack":
                    turns.append(("you (Stack)", rec.get("text", "")))
                    last_event_time = rec.get("ts", last_event_time)
    except OSError:
        return None, None

    if not turns:
        return None, None

    # Take only the last N turns, then trim total length to budget
    recent = turns[-RESUME_TURNS:]
    formatted_lines: list[str] = []
    total = 0
    for role, text in recent:
        line = f"[{role}] {text}"
        if total + len(line) > RESUME_CHAR_BUDGET:
            # If we'd blow the budget, drop earliest until we fit
            while formatted_lines and total + len(line) > RESUME_CHAR_BUDGET:
                dropped = formatted_lines.pop(0)
                total -= len(dropped)
        formatted_lines.append(line)
        total += len(line)

    age_min = max(1, int(age_sec / 60))
    last_seen = last_event_time or time.strftime("%H:%M:%S", time.localtime(latest.stat().st_mtime))

    block = (
        f"\n\n# Resumed conversation context\n\n"
        f"You were just talking with this developer {age_min} minute"
        f"{'s' if age_min != 1 else ''} ago (last activity {last_seen}). "
        f"Pick up naturally — do NOT re-introduce yourself, do NOT re-greet. "
        f"If they ask 'what were we doing,' use the recent turns below. "
        f"If a topic looks half-finished, you can ask whether to keep going.\n\n"
        f"Recent turns (oldest first):\n\n"
        + "\n\n".join(formatted_lines)
        + "\n"
    )

    banner = (
        f"[resume] picking up from {latest.name} "
        f"({age_min}m old, {len(recent)} turns, {len(formatted_lines)} included)"
    )
    return block, banner


class Stack:
    def __init__(self):
        self.ws = None
        self.input_stream = None
        self.output_stream = None
        # Continuous PCM16 buffer drained by the speaker callback (CoreAudio thread).
        self.audio_buf = bytearray()
        self.audio_lock = threading.Lock()
        self.last_speaker_ms = 0.0
        self.shutdown = asyncio.Event()
        self.gated_chunks = 0
        # Track whether a response is currently generating. We must NOT call
        # response.create while one is active — the API errors with
        # 'conversation_already_has_active_response'.
        # When a response completes WITH tool calls in it, the response is
        # "done" but the model never spoke — we need to fire a fresh
        # response.create to get the spoken continuation. Tracked via
        # had_tool_calls_in_response.
        self.response_active = False
        self.response_active_since: float | None = None
        self.had_tool_calls_in_response = False
        # If a response has been "active" longer than this, assume it's wedged
        # and clear the gate so future nudges aren't permanently suppressed.
        self.response_stuck_timeout_sec = 90

        # Transcript persistence (outside the repo, in XDG state)
        sessions_dir = _session_dir()
        stamp = time.strftime("%Y-%m-%d-%H%M")
        self.session_path = sessions_dir / f"{stamp}.jsonl"
        self.session_file = self.session_path.open("a", buffering=1)
        self.session_started = time.time()
        self.assistant_buf: list[str] = []
        self.first_user_turn: str | None = None
        self.user_turns = 0
        self.assistant_turns = 0
        self._write_session({"event": "start", "model": MODEL, "voice": VOICE, "cwd": str(Path.cwd())})

        # Auto-resume: load recent prior session if within window
        resume_block, banner = _load_resume_context()
        self.system_prompt = SYSTEM_PROMPT + (resume_block or "")
        self.is_resumed = resume_block is not None
        if banner:
            print(banner, flush=True)
            self._write_session({"event": "resume", "info": banner})

        # Pane watcher
        self.watcher = Watcher()
        print(self.watcher.status(), flush=True)

        # Pet subprocess
        self.pet_process: subprocess.Popen | None = None
        self.mode = os.environ.get("STACK_MODE", "pair")
        if PET_ENABLED:
            _write_pet_state("idle", self.mode)
            try:
                pet_path = Path(__file__).parent / "pet.py"
                self.pet_process = subprocess.Popen(
                    [sys.executable, str(pet_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"[pet] spawned pid={self.pet_process.pid}", flush=True)
            except Exception as e:
                print(f"[pet] failed to spawn: {e}", flush=True)

    async def connect(self):
        headers = [("Authorization", f"Bearer {API_KEY}")]
        try:
            self.ws = await websockets.connect(URL, additional_headers=headers, max_size=16 * 1024 * 1024)
        except TypeError:
            self.ws = await websockets.connect(URL, extra_headers=headers, max_size=16 * 1024 * 1024)
        print(f"[connected] {MODEL} voice={VOICE}", flush=True)

        await self.send({
            "type": "session.update",
            "session": {
                "type": "realtime",
                "output_modalities": ["audio"],
                "instructions": self.system_prompt,
                "tools": TOOL_SCHEMAS,
                "tool_choice": "auto",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "transcription": {"model": "whisper-1"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.55,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": 600,
                            "create_response": True,
                            "interrupt_response": True,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "voice": VOICE,
                        "speed": 1.0,
                    },
                },
            },
        })

    async def send(self, payload: dict):
        await self.ws.send(json.dumps(payload))

    def _write_session(self, record: dict):
        record["ts"] = time.strftime("%H:%M:%S")
        try:
            self.session_file.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def mic_loop(self):
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()

        def callback(indata, frames, time_info, status):
            if status:
                print(f"[mic] {status}", file=sys.stderr)
            loop.call_soon_threadsafe(q.put_nowait, bytes(indata))

        in_device = _resolve_device(INPUT_DEVICE_OVERRIDE, "input")
        if in_device is not None:
            try:
                name = sd.query_devices(in_device)["name"]
                print(f"[audio] input device: [{in_device}] {name}", flush=True)
            except Exception:
                pass
        self.input_stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=INPUT_CHUNK_SAMPLES, callback=callback, device=in_device,
        )
        self.input_stream.start()
        try:
            while not self.shutdown.is_set():
                chunk = await q.get()
                # Half-duplex gate: drop mic chunks while speakers are active,
                # UNLESS the chunk is loud enough to be a deliberate interrupt
                # (volume-threshold barge-in).
                now_ms = time.monotonic() * 1000
                with self.audio_lock:
                    speaker_active = len(self.audio_buf) > 0
                gated = speaker_active or (now_ms - self.last_speaker_ms) < SPEAKER_TAIL_MS

                if gated:
                    samples = np.frombuffer(chunk, dtype=np.int16)
                    peak = int(np.abs(samples).max()) if len(samples) else 0
                    if peak < INTERRUPT_THRESHOLD:
                        self.gated_chunks += 1
                        continue
                    # Loud enough — break through the gate. Cut local audio
                    # immediately so Stack stops talking on your end, and let
                    # the server's interrupt_response handle the API side.
                    print(f"\n[interrupt] peak={peak} (threshold {INTERRUPT_THRESHOLD})", flush=True)
                    with self.audio_lock:
                        self.audio_buf.clear()

                await self.send({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode(),
                })
        finally:
            self.input_stream.stop()
            self.input_stream.close()

    async def speaker_loop(self):
        def callback(outdata, frames, time_info, status):
            need = frames * 2
            with self.audio_lock:
                avail = len(self.audio_buf)
                if avail >= need:
                    outdata[:] = bytes(self.audio_buf[:need])
                    del self.audio_buf[:need]
                elif avail > 0:
                    outdata[:avail] = bytes(self.audio_buf[:avail])
                    outdata[avail:] = b"\x00" * (need - avail)
                    self.audio_buf.clear()
                else:
                    outdata[:] = b"\x00" * need

        out_device = _resolve_device(OUTPUT_DEVICE_OVERRIDE, "output")
        if out_device is not None:
            try:
                name = sd.query_devices(out_device)["name"]
                print(f"[audio] output device: [{out_device}] {name}", flush=True)
            except Exception:
                pass
        self.output_stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=OUTPUT_BLOCKSIZE, callback=callback, device=out_device,
        )
        self.output_stream.start()
        try:
            await self.shutdown.wait()
        finally:
            self.output_stream.stop()
            self.output_stream.close()

    async def event_loop(self):
        async for raw in self.ws:
            try:
                evt = json.loads(raw)
            except json.JSONDecodeError:
                continue
            t = evt.get("type", "")

            if t in ("response.audio.delta", "response.output_audio.delta"):
                audio = base64.b64decode(evt["delta"])
                with self.audio_lock:
                    self.audio_buf.extend(audio)
                self.last_speaker_ms = time.monotonic() * 1000
                _write_pet_state("speaking", self.mode)

            elif t in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
                d = evt.get("delta", "")
                print(d, end="", flush=True)
                self.assistant_buf.append(d)

            elif t in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
                print("", flush=True)
                full = "".join(self.assistant_buf).strip()
                self.assistant_buf = []
                if full:
                    self.assistant_turns += 1
                    self._write_session({"event": "stack", "text": full})

            elif t == "conversation.item.input_audio_transcription.completed":
                txt = evt.get("transcript", "").strip()
                if txt:
                    print(f"\n[user] {txt}", flush=True)
                    self.user_turns += 1
                    if self.first_user_turn is None:
                        self.first_user_turn = txt
                    self._write_session({"event": "user", "text": txt})

            elif t == "response.function_call_arguments.done":
                call_id = evt["call_id"]
                name = evt["name"]
                args = evt.get("arguments", "{}")
                print(f"\n[tool] {name}({args[:120]})", flush=True)
                _write_pet_state("thinking", self.mode, tool=name)
                self._write_session({"event": "tool_call", "name": name, "args": args})
                self.had_tool_calls_in_response = True
                result = await asyncio.get_running_loop().run_in_executor(None, dispatch, name, args)
                self._write_session({"event": "tool_result", "name": name, "preview": result[:400]})
                # Submit the tool output. Do NOT call response.create here —
                # the response is still active (waiting on more parallel tool
                # outputs, in some cases). The continuation is fired from
                # response.done when had_tool_calls_in_response is set.
                await self.send({
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": call_id, "output": result[:50000]},
                })

            elif t == "response.created":
                self.response_active = True
                self.response_active_since = time.monotonic()
                self.had_tool_calls_in_response = False  # reset per response
                _write_pet_state("thinking", self.mode)

            elif t == "response.done":
                self.response_active = False
                self.response_active_since = None
                # If this response was completed by emitting tool calls (with
                # outputs already submitted), fire a fresh response.create so
                # the model continues and actually speaks the answer.
                if self.had_tool_calls_in_response:
                    self.had_tool_calls_in_response = False
                    await self.send({"type": "response.create"})
                else:
                    _write_pet_state("idle", self.mode)

            elif t in ("response.cancelled", "response.canceled"):
                # Explicit cancel from server (barge-in interrupt or our own response.cancel)
                self.response_active = False
                self.response_active_since = None
                _write_pet_state("idle", self.mode)

            elif t == "input_audio_buffer.speech_started":
                with self.audio_lock:
                    self.audio_buf.clear()
                _write_pet_state("listening", self.mode)

            elif t == "input_audio_buffer.speech_stopped":
                _write_pet_state("idle", self.mode)

            elif t == "error":
                err = evt.get("error", {})
                print(f"\n[error] {json.dumps(err)}", file=sys.stderr, flush=True)
                _write_pet_state("alarmed", self.mode)
                # If the error implies the response is no longer running,
                # clear the gate so future nudges work. Conservative: clear on
                # ANY error after at least 1s of activity — if a response was
                # actually still running, the next response.created will reset
                # us back to active=True correctly.
                if self.response_active and self.response_active_since:
                    if time.monotonic() - self.response_active_since > 1.0:
                        self.response_active = False
                        self.response_active_since = None

            elif t == "session.created":
                print(f"[session] {evt['session']['id']}", flush=True)
                _write_pet_state("idle", self.mode)

    async def watch_consume_loop(self):
        """Drain observations from the Watcher queue and inject as system
        messages, then ask Stack to comment only if useful."""
        while not self.shutdown.is_set():
            obs = await self.watcher.queue.get()
            if not obs:
                continue
            print(f"\n[observe]\n{obs[:300]}", flush=True)
            self._write_session({"event": "observation", "text": obs[:2000]})
            try:
                await self.send({
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "system",
                        "content": [{
                            "type": "input_text",
                            "text": (
                                "[Watcher observation — pane content updated]\n"
                                f"{obs}\n\n"
                                "Speak only if there is something specific and useful to say. "
                                "Silence is fine. If everything looks routine, do NOT respond."
                            ),
                        }],
                    },
                })
                # Only nudge a new response if one isn't already running.
                # Stuck-state guard: if a response has been "active" longer
                # than response_stuck_timeout_sec, assume the .done event was
                # lost (handler crash, network blip, etc.) and clear the gate.
                if self.response_active and self.response_active_since:
                    age = time.monotonic() - self.response_active_since
                    if age > self.response_stuck_timeout_sec:
                        print(f"[watchdog] response_active stuck for {age:.0f}s — clearing gate", flush=True)
                        self.response_active = False
                        self.response_active_since = None
                if not self.response_active:
                    await self.send({"type": "response.create"})
            except Exception as e:
                print(f"[observe] inject error: {e}", flush=True)

    async def run(self):
        await self.connect()
        await asyncio.gather(
            self.mic_loop(),
            self.speaker_loop(),
            self.event_loop(),
            self.watcher.loop(),
            self.watch_consume_loop(),
            return_exceptions=False,
        )

    def stop(self):
        self.shutdown.set()


async def main():
    s = Stack()

    def handler(*_):
        print("\n[shutdown]", flush=True)
        s.stop()
        if s.ws:
            asyncio.create_task(s.ws.close())

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handler)

    try:
        await s.run()
    except websockets.ConnectionClosed as e:
        print(f"[ws closed] {e}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        s._write_session({"event": "end", "user_turns": s.user_turns, "assistant_turns": s.assistant_turns})
        try:
            s.session_file.close()
        except Exception:
            pass
        # Kill the pet subprocess
        if s.pet_process is not None:
            try:
                s.pet_process.terminate()
                try:
                    s.pet_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    s.pet_process.kill()
            except Exception:
                pass
        print(f"[transcript] {s.session_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

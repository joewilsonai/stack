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

MODEL = os.environ.get("STACK_MODEL", "gpt-realtime-2")
VOICE = os.environ.get("STACK_VOICE", "cedar")
URL = f"wss://api.openai.com/v1/realtime?model={MODEL}"

SAMPLE_RATE = 24000
CHANNELS = 1
INPUT_CHUNK_MS = 30
INPUT_CHUNK_SAMPLES = SAMPLE_RATE * INPUT_CHUNK_MS // 1000
OUTPUT_BLOCKSIZE = 1200

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


def _session_dir() -> Path:
    """Collision-safe per-repo session directory under XDG state."""
    xdg_state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    repo_root = Path.cwd().resolve()
    repo_id = repo_root.name + "-" + hashlib.sha256(str(repo_root).encode()).hexdigest()[:12]
    d = Path(xdg_state) / "stack" / "sessions" / repo_id
    d.mkdir(parents=True, exist_ok=True)
    return d


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
                "instructions": SYSTEM_PROMPT,
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

        self.input_stream = sd.RawInputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=INPUT_CHUNK_SAMPLES, callback=callback,
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

        self.output_stream = sd.RawOutputStream(
            samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
            blocksize=OUTPUT_BLOCKSIZE, callback=callback,
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
                self._write_session({"event": "tool_call", "name": name, "args": args})
                result = await asyncio.get_running_loop().run_in_executor(None, dispatch, name, args)
                self._write_session({"event": "tool_result", "name": name, "preview": result[:400]})
                await self.send({
                    "type": "conversation.item.create",
                    "item": {"type": "function_call_output", "call_id": call_id, "output": result[:50000]},
                })
                await self.send({"type": "response.create"})

            elif t == "input_audio_buffer.speech_started":
                with self.audio_lock:
                    self.audio_buf.clear()

            elif t == "error":
                print(f"\n[error] {json.dumps(evt.get('error', evt))}", file=sys.stderr, flush=True)

            elif t == "session.created":
                print(f"[session] {evt['session']['id']}", flush=True)

    async def run(self):
        await self.connect()
        await asyncio.gather(
            self.mic_loop(), self.speaker_loop(), self.event_loop(),
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
        print(f"[transcript] {s.session_path}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

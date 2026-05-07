"""Stack pet — floating pixel-art mascot for the Stack voice agent.

Architecture:
- Runs as a separate subprocess from client.py (PyObjC needs its own NSRunLoop).
- Polls ~/.local/state/stack/pet_state.json every 80ms for state changes.
- Renders a borderless, transparent, always-on-top, draggable NSWindow.
- v0 uses programmatic shapes (stacked rounded cubes) — real sprite frames
  swap in once we generate them.

State protocol (written by client.py):
  {
    "state": "idle" | "listening" | "thinking" | "speaking" | "happy" | "alarmed",
    "mode": "quiet" | "pair" | "roast",
    "tool": "<tool_name>" | null,
    "ts": "<unix_ms>"
  }

Run standalone for testing: python3 pet.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import AppKit
import objc
from Foundation import NSObject, NSRect, NSPoint, NSSize, NSTimer, NSDate
from AppKit import (
    NSApplication, NSWindow, NSView, NSColor, NSBezierPath,
    NSWindowStyleMaskBorderless, NSBackingStoreBuffered, NSScreen,
    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
    NSStatusWindowLevel, NSFloatingWindowLevel, NSEvent,
)

XDG_STATE = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state"))
STATE_FILE = XDG_STATE / "stack" / "pet_state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

PET_W = 140
PET_H = 160
POLL_MS = 80
ANIM_FPS = 24

# Color palette — glowing translucent cubes, brand-coherent
PALETTE = {
    "idle":      [(0.30, 0.34, 0.42, 0.85), (0.34, 0.40, 0.50, 0.85), (0.42, 0.50, 0.62, 0.90)],
    "listening": [(0.18, 0.55, 0.95, 0.92), (0.20, 0.65, 1.00, 0.95), (0.30, 0.78, 1.00, 0.98)],
    "thinking":  [(0.55, 0.40, 0.85, 0.92), (0.65, 0.45, 0.95, 0.95), (0.78, 0.55, 1.00, 0.98)],
    "speaking":  [(0.20, 0.85, 0.55, 0.95), (0.30, 0.95, 0.65, 0.97), (0.40, 1.00, 0.75, 1.00)],
    "happy":     [(0.95, 0.80, 0.20, 0.95), (1.00, 0.88, 0.30, 0.97), (1.00, 0.95, 0.50, 1.00)],
    "alarmed":   [(0.95, 0.30, 0.30, 0.95), (1.00, 0.40, 0.40, 0.97), (1.00, 0.55, 0.55, 1.00)],
}
MODE_RING = {
    "quiet": (0.55, 0.55, 0.55, 0.45),
    "pair":  (0.30, 0.78, 0.50, 0.55),
    "roast": (0.95, 0.40, 0.30, 0.65),
}


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"state": "idle", "mode": "pair", "tool": None}


class PetView(NSView):
    def initWithFrame_(self, frame):
        self = objc.super(PetView, self).initWithFrame_(frame)
        if self is None:
            return None
        self._anim_t = 0.0
        self._state = "idle"
        self._mode = "pair"
        self._drag_offset = None
        return self

    def setStateInfo_(self, info):
        self._state = info.get("state", "idle")
        self._mode = info.get("mode", "pair")
        self.setNeedsDisplay_(True)

    def tick_(self, _timer):
        self._anim_t += 1.0 / ANIM_FPS
        self.setNeedsDisplay_(True)

    def isOpaque(self):
        return False

    # Drag-to-move support — listen for mouse events on the view
    def mouseDown_(self, event):
        loc = NSEvent.mouseLocation()
        win_origin = self.window().frame().origin
        self._drag_offset = NSPoint(loc.x - win_origin.x, loc.y - win_origin.y)

    def mouseDragged_(self, event):
        if self._drag_offset is None:
            return
        loc = NSEvent.mouseLocation()
        new_origin = NSPoint(loc.x - self._drag_offset.x, loc.y - self._drag_offset.y)
        self.window().setFrameOrigin_(new_origin)

    def mouseUp_(self, event):
        self._drag_offset = None

    def drawRect_(self, rect):
        # Background — fully transparent so window shape follows our drawing
        NSColor.clearColor().set()
        NSBezierPath.fillRect_(self.bounds())

        w = self.bounds().size.width
        h = self.bounds().size.height
        cx = w / 2

        palette = PALETTE.get(self._state, PALETTE["idle"])
        ring = MODE_RING.get(self._mode, MODE_RING["pair"])

        # Bobbing animation when active
        bob_amp = 0.0
        if self._state in ("listening", "thinking", "speaking"):
            bob_amp = 4.0
        bob = math.sin(self._anim_t * 2.0 * math.pi * 1.4) * bob_amp

        # Pulsing glow when speaking/thinking
        pulse = 0.0
        if self._state == "thinking":
            pulse = (math.sin(self._anim_t * 2.0 * math.pi * 2.0) + 1) / 2
        elif self._state == "speaking":
            pulse = (math.sin(self._anim_t * 2.0 * math.pi * 5.0) + 1) / 2

        # ── 3 stacked cubes from bottom to top ────────────────────────────
        # Each cube is a rounded square with a slight perspective tilt.
        cube_widths = [78, 64, 50]
        cube_heights = [42, 36, 30]
        gaps = [-4, -4]  # negative = overlap for stacked feel
        y_cursor = 14  # bottom margin

        for i, (cw, ch, color) in enumerate(zip(cube_widths, cube_heights, palette)):
            x = cx - cw / 2
            y = y_cursor + bob * (i / 2.0)  # higher cubes bob more
            r = 10
            # Outer ring (mode color)
            rr_outer = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSRect((x - 2, y - 2), (cw + 4, ch + 4)), r + 2, r + 2,
            )
            NSColor.colorWithCalibratedRed_green_blue_alpha_(*ring).set()
            rr_outer.fill()
            # Cube body
            rr = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSRect((x, y), (cw, ch)), r, r,
            )
            r_, g_, b_, a_ = color
            # Pulse boost on top cube
            if i == 2:
                r_ = min(1.0, r_ + pulse * 0.15)
                g_ = min(1.0, g_ + pulse * 0.15)
                b_ = min(1.0, b_ + pulse * 0.15)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(r_, g_, b_, a_).set()
            rr.fill()
            # Subtle highlight band on top
            highlight = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSRect((x + 4, y + ch - 8), (cw - 8, 4)), 2, 2,
            )
            NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 1, 1, 0.18).set()
            highlight.fill()

            y_cursor += ch + gaps[i] if i < len(gaps) else ch

        # ── Face on the top cube ──────────────────────────────────────────
        face_y_base = y_cursor - cube_heights[2] - 4 + bob

        # Eyes
        eye_w = 6
        eye_h = 8
        eye_y = face_y_base + cube_heights[2] / 2 + 2
        eye_offset = 8

        # Closed eyes when idle
        if self._state == "idle":
            for sign in (-1, 1):
                ex = cx + sign * eye_offset - eye_w / 2
                line = NSBezierPath.bezierPath()
                line.moveToPoint_(NSPoint(ex, eye_y + eye_h / 2))
                line.lineToPoint_(NSPoint(ex + eye_w, eye_y + eye_h / 2))
                line.setLineWidth_(2.0)
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.15, 0.9).set()
                line.stroke()
        else:
            for sign in (-1, 1):
                ex = cx + sign * eye_offset - eye_w / 2
                eye = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    NSRect((ex, eye_y), (eye_w, eye_h)), 2, 2,
                )
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.15, 0.95).set()
                eye.fill()
                # White highlight dot
                hl = NSBezierPath.bezierPathWithOvalInRect_(
                    NSRect((ex + eye_w - 3, eye_y + eye_h - 3), (2, 2)),
                )
                NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 1, 1, 0.95).set()
                hl.fill()

        # Mouth — varies by state
        mouth_y = face_y_base + 8
        if self._state == "speaking":
            # Open oval, pulsing
            oh = 4 + pulse * 4
            mouth = NSBezierPath.bezierPathWithOvalInRect_(
                NSRect((cx - 5, mouth_y - oh / 2), (10, oh)),
            )
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.15, 0.9).set()
            mouth.fill()
        elif self._state == "happy":
            # Smile
            arc = NSBezierPath.bezierPath()
            arc.moveToPoint_(NSPoint(cx - 6, mouth_y + 1))
            arc.curveToPoint_controlPoint1_controlPoint2_(
                NSPoint(cx + 6, mouth_y + 1),
                NSPoint(cx - 2, mouth_y - 4),
                NSPoint(cx + 2, mouth_y - 4),
            )
            arc.setLineWidth_(2.0)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.15, 0.9).set()
            arc.stroke()
        elif self._state == "alarmed":
            # Open small "o"
            mouth = NSBezierPath.bezierPathWithOvalInRect_(
                NSRect((cx - 3, mouth_y - 2), (6, 6)),
            )
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.15, 0.9).set()
            mouth.fill()
        else:
            # Neutral line
            line = NSBezierPath.bezierPath()
            line.moveToPoint_(NSPoint(cx - 5, mouth_y))
            line.lineToPoint_(NSPoint(cx + 5, mouth_y))
            line.setLineWidth_(1.6)
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.1, 0.1, 0.15, 0.85).set()
            line.stroke()

        # ── State label below (small, faint) ──────────────────────────────
        label = self._state
        attrs = {
            AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(9),
            AppKit.NSForegroundColorAttributeName:
                NSColor.colorWithCalibratedRed_green_blue_alpha_(1, 1, 1, 0.55),
        }
        text = AppKit.NSString.stringWithString_(label)
        size = text.sizeWithAttributes_(attrs)
        text.drawAtPoint_withAttributes_(
            NSPoint(cx - size.width / 2, 2), attrs,
        )


class PetController(NSObject):
    def init(self):
        self = objc.super(PetController, self).init()
        if self is None:
            return None

        # Position window in top-right corner of main screen
        screen_frame = NSScreen.mainScreen().frame()
        x = screen_frame.size.width - PET_W - 24
        y = screen_frame.size.height - PET_H - 60

        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSRect((x, y), (PET_W, PET_H)),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setLevel_(NSFloatingWindowLevel)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.clearColor())
        self.window.setHasShadow_(True)
        self.window.setIgnoresMouseEvents_(False)
        self.window.setMovableByWindowBackground_(False)  # we handle drag in view
        self.window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorStationary
        )

        self.view = PetView.alloc().initWithFrame_(NSRect((0, 0), (PET_W, PET_H)))
        self.window.setContentView_(self.view)
        self.window.orderFrontRegardless()

        # Animation timer (24 fps)
        self._anim_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1.0 / ANIM_FPS, self.view, b"tick:", None, True,
        )
        # State poll timer
        self._poll_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            POLL_MS / 1000.0, self, b"poll:", None, True,
        )
        self._last_mtime = 0.0
        self._last_state_seen = time.time()
        return self

    def poll_(self, _timer):
        try:
            mtime = STATE_FILE.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime != self._last_mtime:
            self._last_mtime = mtime
            self._last_state_seen = time.time()
            info = _read_state()
            self.view.setStateInfo_(info)
        # Self-exit if state hasn't been written for 30s — implies parent died
        if time.time() - self._last_state_seen > 30:
            NSApplication.sharedApplication().terminate_(None)


def main():
    # Initialize state file with idle if not present
    if not STATE_FILE.exists():
        STATE_FILE.write_text(json.dumps({"state": "idle", "mode": "pair", "tool": None}))

    app = NSApplication.sharedApplication()
    # Hide dock icon — agent-only app
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

    controller = PetController.alloc().init()
    # Keep a reference so it isn't GC'd
    app._stack_pet_controller = controller

    app.run()


if __name__ == "__main__":
    main()

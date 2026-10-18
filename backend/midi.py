"""MIDI output: the waveform as a control change, and a note per detected mark.

Everything goes out on one channel. Settings live here and are shown to the
pages through `state()`; `update()` takes the same shape (partially) back.

The waveform reaches the server in bursts (5 samples per BLE packet, packets
sometimes 0.5 s apart). Sent as it arrives, the CC would be just as choppy, so
`run()` plays it out the way the UI's graph does: a short buffer absorbs the
bursts, samples are consumed at a steady 50 Hz, and the value is interpolated
between samples on every tick.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Callable

import mido

log = logging.getLogger(__name__)

NOTE_KINDS = ("systolic", "notch", "diastolic")
# the device's own beat flag triggers the systolic note
KIND_ALIASES = {"beat": "systolic"}
# note lengths as fractions of a whole note; a quarter note is one beat of the pulse rate
LENGTHS = {"1/4": 1.0, "1/8": 0.5, "1/16": 0.25, "1/32": 0.125}  # in beats
DEFAULT_BPM = 120  # used when the device reports no (or an implausible) pulse rate
BPM_RANGE = (20, 300)

# CC playout, as in web/app.js
RATE = 50.0  # waveform samples per second
TARGET_DELAY = 0.4  # s of samples kept buffered; BLE packets can arrive ~0.5 s apart
MAX_BACKLOG = 1.0  # s; beyond this the buffer is cut back to the target
TICK = 0.005  # s between CC updates (~6 ms in practice on Windows)


def _clamp(value, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


class MidiOutput:
    def __init__(self, on_change: Callable[[], None]):
        self._on_change = on_change
        self._port: mido.ports.BaseOutput | None = None
        self.port_name: str | None = None
        self.error: str | None = None
        self.ports: list[str] = []
        self.channel = 1  # 1..16 as shown to the user
        self.cc = {"enabled": False, "number": 1}
        self.notes = {kind: {"enabled": False, "note": note} for kind, note in zip(NOTE_KINDS, (36, 38, 42))}
        self.velocity = {"fixed": True, "value": 100}  # not fixed: velocity = pulse rate
        self.length = "1/16"
        self._bpm: float | None = None  # latest pulse rate from the device
        self._note_offs: dict[int, asyncio.TimerHandle] = {}
        # CC playout
        self._queue: deque[int] = deque()
        self._pos = 0.0  # fractional position between _queue[0] and _queue[1]
        self._speed = 1.0
        self._playing = False
        self._last_cc: int | None = None
        self._last_push = 0.0

    def state(self) -> dict:
        return {
            "ports": self.ports,
            "port": self.port_name,
            "error": self.error,
            "channel": self.channel,
            "cc": self.cc,
            "notes": self.notes,
            "velocity": self.velocity,
            "length": self.length,
        }

    # --- ports -------------------------------------------------------------

    def refresh(self):
        try:
            self.ports = mido.get_output_names()
        except Exception as e:  # the backend can fail if the system has no MIDI at all
            log.warning("can't list MIDI ports: %s", e)
            self.ports = []
        self._on_change()

    def connect(self, name: str):
        self._close_port()
        try:
            self._port = mido.open_output(name)
            self.port_name = name
            self.error = None
            self._last_cc = None
            log.info("MIDI output: %s", name)
        except Exception as e:
            self.error = f"Can't open {name}: {e}"
            log.warning("%s", self.error)
        self._on_change()

    def disconnect(self):
        self._close_port()
        self.error = None
        self._on_change()

    def _close_port(self):
        if self._port is None:
            return
        self.all_notes_off()
        try:
            self._port.close()
        except Exception:
            pass
        log.info("MIDI output closed")
        self._port = None
        self.port_name = None

    def close(self):
        self._close_port()

    # --- settings ----------------------------------------------------------

    def update(self, settings: dict):
        if "channel" in settings:
            self.all_notes_off()
            self.channel = _clamp(settings["channel"], 1, 16, self.channel)
            self._last_cc = None
        cc = settings.get("cc") or {}
        if "enabled" in cc:
            self.cc["enabled"] = bool(cc["enabled"])
        if "number" in cc:
            self.cc["number"] = _clamp(cc["number"], 0, 127, self.cc["number"])
            self._last_cc = None
        velocity = settings.get("velocity") or {}
        if "fixed" in velocity:
            self.velocity["fixed"] = bool(velocity["fixed"])
        if "value" in velocity:
            self.velocity["value"] = _clamp(velocity["value"], 1, 127, self.velocity["value"])
        if settings.get("length") in LENGTHS:
            self.length = settings["length"]
        for kind, note in (settings.get("notes") or {}).items():
            if kind not in self.notes:
                continue
            if "enabled" in note:
                self.notes[kind]["enabled"] = bool(note["enabled"])
            if "note" in note:
                self.notes[kind]["note"] = _clamp(note["note"], 0, 127, self.notes[kind]["note"])
        self._on_change()

    # --- output ------------------------------------------------------------

    def _send(self, msg: mido.Message):
        try:
            self._port.send(msg)
        except Exception as e:
            # e.g. a USB interface was unplugged
            self._port = None
            self.port_name = None
            self.error = f"MIDI output failed: {e}"
            log.warning("%s", self.error)
            self._on_change()

    def set_rate(self, bpm: float | None):
        """The device's current pulse rate (None when there is none)."""
        self._bpm = bpm

    def bpm(self) -> float:
        if self._bpm is None or not BPM_RANGE[0] <= self._bpm <= BPM_RANGE[1]:
            return DEFAULT_BPM
        return self._bpm

    def trigger(self, kind: str):
        setting = self.notes.get(KIND_ALIASES.get(kind, kind))
        if self._port is None or not setting or not setting["enabled"]:
            return
        note = setting["note"]
        ch = self.channel - 1
        pending = self._note_offs.pop(note, None)
        if pending:
            # retriggered before it ended: end it now so on/off stay paired
            pending.cancel()
            self._send(mido.Message("note_off", channel=ch, note=note))
        bpm = self.bpm()
        velocity = self.velocity["value"] if self.velocity["fixed"] else _clamp(round(bpm), 1, 127, 127)
        length = 60 / bpm * LENGTHS[self.length]
        self._send(mido.Message("note_on", channel=ch, note=note, velocity=velocity))
        if self._port is not None:
            self._note_offs[note] = asyncio.get_running_loop().call_later(length, self._note_off, ch, note)

    def _note_off(self, ch: int, note: int):
        self._note_offs.pop(note, None)
        if self._port is not None:
            self._send(mido.Message("note_off", channel=ch, note=note))

    def all_notes_off(self):
        """Ends every sounding note now (on pause, channel change, disconnect)."""
        ch = self.channel - 1
        for note, handle in list(self._note_offs.items()):
            handle.cancel()
            if self._port is not None:
                self._send(mido.Message("note_off", channel=ch, note=note))
        self._note_offs.clear()

    # --- CC playout --------------------------------------------------------

    def push_samples(self, samples: list[int]):
        now = time.perf_counter()
        if now - self._last_push > 1.0:
            # the stream paused (device off, reconnect): start over with a full buffer
            # instead of playing the new samples from a near-empty one
            self.clear_samples()
        self._last_push = now
        self._queue.extend(samples)
        if len(self._queue) > RATE * MAX_BACKLOG:
            for _ in range(len(self._queue) - round(RATE * TARGET_DELAY)):
                self._queue.popleft()

    def clear_samples(self):
        """Drops buffered samples (on pause), so resuming doesn't replay stale ones."""
        self._queue.clear()
        self._playing = False

    async def run(self):
        """Plays buffered samples out as CC until cancelled."""
        # perf_counter, not loop.time(): on Windows the latter only moves in 15.6 ms steps
        last = time.perf_counter()
        while True:
            await asyncio.sleep(TICK)
            now = time.perf_counter()
            value = self._advance(min(now - last, 0.1))
            last = now
            if value is not None and value != self._last_cc and self._port is not None and self.cc["enabled"]:
                self._last_cc = value
                self._send(mido.Message("control_change", channel=self.channel - 1, control=self.cc["number"], value=value))

    def _advance(self, dt: float) -> int | None:
        """Moves the playout position on by `dt` seconds; returns the CC value there."""
        target = RATE * TARGET_DELAY
        if not self._playing:
            # (re)start only with a full buffer, so playback doesn't stutter at once
            if len(self._queue) < target:
                return None
            self._playing, self._pos, self._speed = True, 0.0, 1.0
        if len(self._queue) < 2:
            self._playing = False
            return self._queue[0] if self._queue else None
        # nudge the speed so the buffer stays near its target; smoothed so it doesn't jerk
        wanted = min(1.25, max(0.8, 1 + 0.25 * (len(self._queue) - target) / target))
        self._speed += (wanted - self._speed) * min(1.0, dt * 1.5)
        self._pos += RATE * self._speed * dt
        while self._pos >= 1 and len(self._queue) > 2:
            self._queue.popleft()
            self._pos -= 1
        self._pos = min(self._pos, 1.0)
        a, b = self._queue[0], self._queue[1]
        return round(a + (b - a) * self._pos)

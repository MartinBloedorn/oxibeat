"""MIDI output: the waveform as a control change, and a note per detected mark.

Everything goes out on one channel. Settings live here and are shown to the
pages through `state()`; `update()` takes the same shape (partially) back.
The waveform arrives already smoothed (see playout.py) through `send_wave()`.
The MIDI clock (clock.py) sends from its own thread through `send_realtime()`;
a lock keeps the two from using the port at once.
"""

import asyncio
import logging
import threading
from typing import Callable

import mido

from .clock import MidiClock

log = logging.getLogger(__name__)

NOTE_KINDS = ("systolic", "notch", "diastolic")
# the device's own beat flag triggers the systolic note
KIND_ALIASES = {"beat": "systolic"}
# note lengths as fractions of a whole note; a quarter note is one beat of the pulse rate
LENGTHS = {"1/4": 1.0, "1/8": 0.5, "1/16": 0.25, "1/32": 0.125}  # in beats


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
        self._note_offs: dict[int, asyncio.TimerHandle] = {}
        self._last_cc: int | None = None
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.clock = MidiClock(self)

    def start(self):
        """Starts the clock thread; call from the running event loop."""
        self._loop = asyncio.get_running_loop()
        self.clock.start()

    @property
    def is_open(self) -> bool:
        return self._port is not None

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
            "clock": self.clock.state(),
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
        self.clock.halt()
        self.all_notes_off()
        with self._lock:
            try:
                self._port.close()
            except Exception:
                pass
            self._port = None
        log.info("MIDI output closed")
        self.port_name = None

    def close(self):
        self.clock.close()
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
        if isinstance(settings.get("clock"), dict):
            self.clock.update(settings["clock"])
        self._on_change()

    # --- output ------------------------------------------------------------

    def _send(self, msg: mido.Message):
        with self._lock:
            if self._port is None:
                return
            try:
                self._port.send(msg)
                return
            except Exception as e:
                # e.g. a USB interface was unplugged
                self._port = None
                error = e
        self._failed(error)

    def send_realtime(self, kind: str):
        """Sends a system realtime message ("clock", "start", "stop"); safe from any thread."""
        with self._lock:
            if self._port is None:
                return
            try:
                self._port.send(mido.Message(kind))
                return
            except Exception as e:
                self._port = None
                error = e
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._failed, error)

    def _failed(self, error: Exception):
        self.port_name = None
        self.error = f"MIDI output failed: {error}"
        log.warning("%s", self.error)
        self._on_change()

    def send_wave(self, value: float):
        """The smoothed waveform (0..127); sent as CC when it changes."""
        if self._port is None or not self.cc["enabled"]:
            return
        cc = round(value)
        if cc != self._last_cc:
            self._last_cc = cc
            self._send(mido.Message("control_change", channel=self.channel - 1, control=self.cc["number"], value=cc))

    def trigger(self, kind: str, bpm: float):
        """A detected mark: plays its note if enabled; `bpm` sets velocity (if not fixed) and length."""
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

"""OSC output over UDP: the waveform, a message per detected mark, and the device's on/off state.

Messages (with the default prefix "oxibeat"):
  /oxibeat/data       float 0..1   the smoothed waveform, whenever it changes (see playout.py)
  /oxibeat/systolic   float bpm    on every systolic peak (also the device's beat flag)
  /oxibeat/notch      float bpm    on every dicrotic notch
  /oxibeat/diastolic  float bpm    on every diastolic peak
  /oxibeat/device     int 1 / 0    the oximeter connected / disconnected

The bpm is the pulse rate at the moment of the mark (see rate.py). UDP has no
connection: "connecting" resolves the host and opens a socket; nothing checks
that anything is listening.
"""

import logging
from typing import Callable

from pythonosc.udp_client import SimpleUDPClient

log = logging.getLogger(__name__)

EVENT_KINDS = ("systolic", "notch", "diastolic")
KIND_ALIASES = {"beat": "systolic"}  # the device's own beat flag counts as a systolic peak
WAVE_MAX = 127.0
DEFAULT_PREFIX = "oxibeat"
# characters OSC doesn't allow in an address part (they are pattern syntax or separators)
_NOT_ALLOWED = set(" #*,?[]{}")


def clean_prefix(prefix) -> str:
    """'/my prefix/' -> 'myprefix'; nested parts ('a/b') are kept."""
    parts = ["".join(c for c in part if c not in _NOT_ALLOWED and c.isprintable()) for part in str(prefix).split("/")]
    return "/".join(p for p in parts if p)


class OscOutput:
    def __init__(self, on_change: Callable[[], None]):
        self._on_change = on_change
        self._client: SimpleUDPClient | None = None
        self.host = "127.0.0.1"
        self.port = 9000
        self.prefix = DEFAULT_PREFIX
        self.error: str | None = None
        self.wave = {"enabled": False}
        self.events = {kind: {"enabled": False} for kind in EVENT_KINDS}
        self.device = {"enabled": False}
        self._device_on = False
        self._last_wave: float | None = None

    def state(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "prefix": self.prefix,
            "connected": self._client is not None,
            "error": self.error,
            "wave": self.wave,
            "events": self.events,
            "device": self.device,
        }

    def _address(self, name: str) -> str:
        return f"/{self.prefix}/{name}" if self.prefix else f"/{name}"

    # --- connection ----------------------------------------------------------

    def connect(self):
        self._client = None
        try:
            # resolves the host name; raises if it can't
            self._client = SimpleUDPClient(self.host, self.port)
            self.error = None
            self._last_wave = None
            log.info("OSC output: %s:%d", self.host, self.port)
            self._send_device_on()
        except (OSError, UnicodeError) as e:
            self.error = f"Can't send to {self.host}:{self.port}: {e}"
            log.warning("%s", self.error)
        self._on_change()

    def disconnect(self):
        self.close()
        self.error = None
        self._on_change()

    def close(self):
        if self._client is not None:
            log.info("OSC output closed")
        self._client = None

    # --- settings ------------------------------------------------------------

    def update(self, settings: dict):
        # host and port only take effect on the next connect
        if "host" in settings:
            host = str(settings["host"]).strip()[:253]
            if host:
                self.host = host
        if "port" in settings:
            try:
                self.port = max(1, min(65535, int(settings["port"])))
            except (TypeError, ValueError):
                pass
        if "prefix" in settings:
            self.prefix = clean_prefix(settings["prefix"])
        wave = settings.get("wave") or {}
        if "enabled" in wave:
            self.wave["enabled"] = bool(wave["enabled"])
            self._last_wave = None
        device = settings.get("device") or {}
        if "enabled" in device:
            self.device["enabled"] = bool(device["enabled"])
            self._send_device_on()
        for kind, event in (settings.get("events") or {}).items():
            if kind in self.events and "enabled" in event:
                self.events[kind]["enabled"] = bool(event["enabled"])
        self._on_change()

    # --- output --------------------------------------------------------------

    def _send(self, address: str, value: float | int):
        try:
            self._client.send_message(address, value)
        except OSError as e:
            # e.g. the network went away
            self._client = None
            self.error = f"OSC output failed: {e}"
            log.warning("%s", self.error)
            self._on_change()

    def set_device_on(self, on: bool):
        """The oximeter connected or disconnected; sent as 1 / 0 when it changes."""
        if on != self._device_on:
            self._device_on = on
            self._send_device_on()

    def _send_device_on(self):
        """Sends the current device state, so a receiver that joins later (socket opened, message enabled) is in step."""
        if self._client is not None and self.device["enabled"]:
            self._send(self._address("device"), int(self._device_on))

    def send_wave(self, value: float):
        """The smoothed waveform (0..127); sent normalised to 0..1 when it changes."""
        if self._client is None or not self.wave["enabled"]:
            return
        normalised = round(value / WAVE_MAX, 4)
        if normalised != self._last_wave:
            self._last_wave = normalised
            self._send(self._address("data"), normalised)

    def trigger(self, kind: str, bpm: float):
        kind = KIND_ALIASES.get(kind, kind)
        if self._client is None or kind not in self.events or not self.events[kind]["enabled"]:
            return
        self._send(self._address(kind), float(bpm))

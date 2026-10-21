"""HTTP + WebSocket server: serves the web UI and relays device data to every open page.

WebSocket messages (JSON), server -> page:
  {"type": "status", "device": {"state", "name", "address"}, "running": bool,
   "detection": id, "detectors": [{"id", "label", "kinds": [kind, ...]}, ...],
   "midi": {"ports": [name, ...], "port": name or null, "error": str or null, "channel": 1..16,
            "cc": {"enabled", "number"}, "device": {"enabled", "number"}, "notes": {kind: {"enabled", "note"}, ...},
            "velocity": {"fixed", "value"}, "length": "1/4" | "1/8" | "1/16" | "1/32",
            "clock": {"enabled", "auto", "sensitivity": 0..100, "fraction": "1/4" | "1/2"}},
   "osc": {"host", "port", "prefix", "connected": bool, "error": str or null,
           "wave": {"enabled"}, "events": {kind: {"enabled"}, ...}, "device": {"enabled"}}}
  {"type": "vitals", "spo2": int, "pr": int, "pi": float}
  {"type": "wave", "n": first sample's running number, "samples": [int, ...]}
  {"type": "marks", "marks": [[sample number, kind], ...]}  (may refer to samples already sent)
page -> server:
  {"type": "set_running", "running": bool}
  {"type": "set_detection", "detection": id}
  {"type": "midi_refresh"}                       re-list the system's MIDI outputs
  {"type": "midi_connect", "port": name}
  {"type": "midi_disconnect"}
  {"type": "midi_set", ...}                      any part of status.midi's channel / cc / device / notes / velocity / length / clock
  {"type": "osc_connect"}                        start sending to status.osc's host and port
  {"type": "osc_disconnect"}
  {"type": "osc_set", ...}                       any part of status.osc's host / port / prefix / wave / events / device
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable

from aiohttp import WSMsgType, web

from .detection import DETECTORS, Mark
from .device import Device
from .midi import MidiOutput
from .osc import OscOutput
from .playout import Playout
from .protocol import Vitals, Wave
from .rate import effective_bpm
from .simulator import Simulator

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
CLIENT_QUEUE_MAX = 500  # messages; a page that falls this far behind gets dropped


class Hub:
    def __init__(
        self, name_prefix: str | None, address: str | None, simulate: bool = False, detection: str = "waveform"
    ):
        self.running = True
        self._clients: dict[web.WebSocketResponse, asyncio.Queue] = {}
        self._n = 0  # running number of the next waveform sample
        self.detector = DETECTORS[detection]()
        self._rate: float | None = None  # the device's latest pulse rate
        self.playout = Playout()  # the smoothed waveform for the outputs
        self.midi = MidiOutput(lambda: self._broadcast(self._status()))
        self.osc = OscOutput(lambda: self._broadcast(self._status()))
        if simulate:
            self.device = Simulator(self._on_state, self._on_vitals, self._on_wave)
        else:
            self.device = Device(self._on_state, self._on_vitals, self._on_wave, name_prefix, address)
        self.midi.refresh()

    # --- device callbacks -------------------------------------------------

    def _on_state(self, state: dict):
        self.detector.reset()
        if state["state"] != "connected":
            self._rate = None
        self.midi.set_device_on(state["state"] == "connected")
        self.osc.set_device_on(state["state"] == "connected")
        self._broadcast(self._status())

    def _on_vitals(self, v: Vitals):
        self._rate = v.pr or None
        if v.pr:
            self.midi.clock.pulse_rate(v.pr)
        if self.running:
            self._broadcast({"type": "vitals", "spo2": v.spo2, "pr": v.pr, "pi": v.pi})

    def _on_wave(self, w: Wave):
        n0 = self._n
        self._n += len(w.samples)
        # detection keeps running while paused, so it is in step when playback resumes
        try:
            marks = self.detector.feed(n0, w.samples, w.pulses)
        except Exception:
            # a detection bug must not stop the waveform reaching the pages and MIDI
            log.exception("detection failed; resetting the detector")
            self.detector.reset()
            marks = []
        # the clock stays locked to the pulse while paused, like the detector
        self.midi.clock.samples(self._n - 1)
        for m in marks:
            if m.kind in ("systolic", "beat"):
                self.midi.clock.beat(m.n)
        if self.running:
            self.playout.push(w.samples)
            self._broadcast({"type": "wave", "n": n0, "samples": w.samples})
            if marks:
                self._on_marks(marks)

    def _on_marks(self, marks: list[Mark]):
        bpm = effective_bpm(self._rate)
        for m in marks:
            self.midi.trigger(m.kind, bpm)
            self.osc.trigger(m.kind, bpm)
        self._broadcast({"type": "marks", "marks": [[m.n, m.kind] for m in marks]})

    def _on_wave_value(self, value: float):
        """The smoothed waveform, every playout tick."""
        self.midi.send_wave(value)
        self.osc.send_wave(value)

    # --- clients ----------------------------------------------------------

    def _status(self) -> dict:
        return {
            "type": "status",
            "device": self.device.state,
            "running": self.running,
            "detection": self.detector.id,
            "detectors": [{"id": d.id, "label": d.label, "kinds": d.kinds} for d in DETECTORS.values()],
            "midi": self.midi.state(),
            "osc": self.osc.state(),
        }

    def _broadcast(self, msg: dict):
        text = json.dumps(msg)
        for ws, queue in list(self._clients.items()):
            if queue.qsize() >= CLIENT_QUEUE_MAX:
                log.warning("page is not keeping up, closing it")
                self._clients.pop(ws, None)
                asyncio.ensure_future(ws.close())
                continue
            queue.put_nowait(text)

    def set_running(self, running: bool):
        if running != self.running:
            self.running = running
            self.midi.clock.set_running(running)
            log.info("transport %s", "running" if running else "paused")
            if not running:
                self.midi.all_notes_off()
                self.playout.clear()
            self._broadcast(self._status())

    def set_detection(self, detection: str):
        if detection in DETECTORS and detection != self.detector.id:
            self.detector = DETECTORS[detection]()
            log.info("detection: %s", self.detector.label)
            self._broadcast(self._status())

    async def close(self):
        """Closes the outputs and every page's socket, so shutdown doesn't wait on them."""
        self.midi.close()
        self.osc.close()
        clients = list(self._clients)
        self._clients.clear()
        await asyncio.gather(*(ws.close() for ws in clients), return_exceptions=True)

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=10)
        await ws.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        self._clients[ws] = queue
        queue.put_nowait(json.dumps(self._status()))

        async def writer():
            while True:
                await ws.send_str(await queue.get())

        writer_task = asyncio.create_task(writer())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                kind = data.get("type")
                if kind == "set_running":
                    self.set_running(bool(data.get("running")))
                elif kind == "set_detection":
                    self.set_detection(str(data.get("detection")))
                elif kind == "midi_refresh":
                    self.midi.refresh()
                elif kind == "midi_connect":
                    self.midi.connect(str(data.get("port")))
                elif kind == "midi_disconnect":
                    self.midi.disconnect()
                elif kind == "midi_set":
                    self.midi.update(data)
                elif kind == "osc_connect":
                    self.osc.connect()
                elif kind == "osc_disconnect":
                    self.osc.disconnect()
                elif kind == "osc_set":
                    self.osc.update(data)
        finally:
            writer_task.cancel()
            self._clients.pop(ws, None)
        return ws


async def index(_request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


async def serve(
    host: str,
    port: int,
    name_prefix: str | None = None,
    address: str | None = None,
    simulate: bool = False,
    on_ready: Callable[[str], None] | None = None,
    detection: str = "waveform",
):
    """Runs until cancelled. `on_ready` gets the UI's URL once the server is listening."""
    hub = Hub(name_prefix, address, simulate, detection)
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", hub.handle_ws)
    app.router.add_static("/", WEB_DIR)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}/"
    log.info("UI at %s", url)
    if on_ready:
        on_ready(url)
    hub.midi.start()
    playout = asyncio.create_task(hub.playout.run(hub._on_wave_value))
    try:
        await hub.device.run()
    finally:
        playout.cancel()
        await hub.close()
        await runner.cleanup()

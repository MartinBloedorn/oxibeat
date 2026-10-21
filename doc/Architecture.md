# Architecture and design decisions

## Overview

```
 oximeter --BLE--> backend/device.py --> backend/server.py (Hub) --WebSocket--> web/ (browser UI)
                   (scan/connect/      |  - sample numbering         - transport bar
                    reconnect loop,    |  - detector (detection.py)  - scrolling graph
                    protocol.py)       |  - pause state              - settings menu
                                       |  - broadcast to all pages
                                       |--> backend/playout.py --> midi.py (CC) / osc.py (/data)
                                       |--> marks -------------> midi.py (notes) / osc.py (events)
                                       '--> heartbeats --------> clock.py (MIDI clock thread) --> midi.py
```

One Python process, one asyncio event loop: the BLE client (bleak), the HTTP server and the WebSocket connections all live in it. The one thread besides it is the MIDI clock's (see below), because asyncio's timers are too coarse for evenly spaced clock ticks.

| File | Role |
|---|---|
| `oxibeat.py` | CLI entry point: arguments, logging, opens the browser, Ctrl+C handling |
| `backend/protocol.py` | Packet parser (sync, CRC, decode) → `Vitals` / `Wave` |
| `backend/device.py` | `Device`: BLE scan → connect → stream → back to scanning, forever |
| `backend/simulator.py` | `Simulator`: same interface as `Device`, synthetic pulse (`--simulate`) |
| `backend/detection.py` | Beat detectors (see `Detection.md`) |
| `backend/playout.py` | `Playout`: smooths the bursty waveform for the outputs (buffer, steady pace, interpolation) |
| `backend/rate.py` | `effective_bpm()`: the pulse rate the outputs use (device rate, or 120 bpm) |
| `backend/midi.py` | `MidiOutput`: port handling, MIDI settings, CC and note output |
| `backend/clock.py` | `MidiClock`: MIDI clock phase-locked to the heartbeats, on its own thread |
| `backend/osc.py` | `OscOutput`: OSC over UDP, settings, waveform and event messages |
| `backend/server.py` | `Hub` (state, detection, broadcast) and the aiohttp app |
| `web/index.html`, `style.css`, `app.js` | The UI; plain files, no build step, no libraries |

## Device connection (`backend/device.py`)

The device turns off whenever the finger is out, so the connection loop is the core of the backend:

1. Scan up to 5 s for a matching advertisement (`BleakScanner.find_device_by_filter`); none → scan again.
2. Connect (15 s timeout), subscribe to NUS TX notifications, report `connected`.
3. Wait until either bleak's disconnect callback fires **or no notification has arrived for 5 s** (`DATA_TIMEOUT`). The second condition exists because on Windows the disconnect callback can be late; in testing the callback was immediate.
4. Report `scanning` (in a `finally`, so errors also reset the state), wait 1 s, start over.

Any `BleakError` / `OSError` / timeout is logged as a warning and the loop continues. States reported to the UI: `scanning`, `connecting`, `connected` (with name and address). Tested: finger out → `scanning` immediately; finger back in → `connected` again ~3 s after the device advertises.

## Server and Hub (`backend/server.py`)

- **aiohttp** serves `web/` (static) and a WebSocket at `/ws`. Chosen over FastAPI/uvicorn because it does HTTP and WebSockets with one dependency and runs in the same loop as bleak.
- The `Hub` holds the shared state: `running` (play/pause), the current detector, and a **running sample number** (`_n`) that numbers every waveform sample since the server started.
- **Each page has its own outgoing queue and writer task**, so a slow page cannot block the device callback; a page more than 500 messages behind is disconnected.
- **Detector errors are contained**: `Hub._on_wave` catches any exception from `detector.feed()`, logs it with a traceback and resets the detector; the samples still go to the pages and MIDI. (Before, an exception there also dropped that packet's samples, leaving a hole in the graph.)
- **Pause is server-side and global**: while paused, no wave/vitals/marks go out to any page, and nothing to MIDI or OSC. The detector keeps running while paused so it is in step on resume. This was decided so that pause can gate the outputs, not just the display.
- **Detection mode is also server-side and global**, switchable from any page; switching creates a fresh detector. The detector is reset whenever the device state changes (connect/disconnect).
- `Hub._on_marks()` is the single place where detected marks leave the detector; it computes the rate once (`effective_bpm(self._rate)`, from the last `Vitals.pr`, reset when the device disconnects) and triggers MIDI notes and OSC events. Waveform samples go from `Hub._on_wave()` into the shared `Playout`, whose task calls `Hub._on_wave_value()` every tick, which feeds `midi.send_wave()` and `osc.send_wave()`. A further output would hook into these two places.
- **Waveform analysis is the default detection** (since the MIDI step, at the user's request); `--detection device` starts with the device flag.

## MIDI output (`backend/midi.py`)

- **mido + python-rtmidi**: the standard pairing; wheels for Windows/Python 3.11, no compiler needed. Port names come from `mido.get_output_names()` (Windows names have an index suffix, e.g. `TouchOSC Bridge 1`). rtmidi on Windows can't create virtual ports, so a loopback driver (e.g. loopMIDI) is needed to feed another program on the same machine.
- **State lives on the server** (`MidiOutput`), like play/pause: one port, shared by all pages, reported in `status.midi`. Pages send partial updates (`midi_set`); the server validates and clamps (channel 1–16, CC 0–127, notes 0–127) and broadcasts the result. The UI never assumes its own input was accepted; it re-renders from `status`, but doesn't overwrite a field that has focus.
- **Waveform CC playout** (now `backend/playout.py`, shared with OSC; a task started by `serve()`): sent as they arrive, samples came in bursts of 5 with stalls of up to ~0.5 s, which the user found too choppy. So the server plays them out like the graph does: a buffer of ~0.4 s (`TARGET_DELAY`; playback starts once it is full), consumption at 50 samples/s with the same gentle speed control as `app.js`, and on every tick (`TICK` = 5 ms, ~6 ms in practice) the value is **linearly interpolated** between the two current samples and sent if it changed. Measured against bursty delivery of a real recording: updates every ~6 ms, value steps median 1 / max 7, versus bursts of 18-value jumps with 0.4 s gaps before. Cost: the CC is ~0.4 s behind the device (notes are not delayed, so they now lead the CC by roughly 0.15 s for systolic peaks). Pause clears the buffer, and so does a silence of more than 1 s in the stream, so after a reconnect the CC waits ~0.4 s for a full buffer instead of playing from a near-empty one.
- **Windows timing pitfall**: `loop.time()` / `time.monotonic()` only advance in **15.6 ms steps** on Windows, even though `asyncio.sleep(0.005)` really wakes every ~6 ms. Measuring elapsed time with them made the playout jump in 15.6 ms chunks (value steps up to 14). The playout uses `time.perf_counter()`. Use it for any other fine timing too.
- **Second Windows timing pitfall**: asyncio runs timers that are due within its clock resolution (15.6 ms) as soon as the loop wakes for I/O, so `asyncio.sleep(0.005)` returns early whenever packets arrive. With OSC sent to a receiver in the same process (a test), each outgoing datagram woke the loop and the playout ran ~1,500 ticks/s. `Playout.run()` therefore skips wake-ups that come less than 0.8 × `TICK` after the last tick. Ticks are ~6 ms apart normally and ~10 ms under heavy I/O.
- **14-bit CC was tried and removed**: the device's data is 7-bit, so splitting it over MSB/LSB added range, not resolution.
- **Notes**: note on when a mark arrives, note off after the chosen length via `loop.call_later`. **Velocity** is fixed (setting, default 100) or the device's current pulse rate clamped to 1–127. **Length** is a fraction of a whole note at the current pulse rate, a quarter note being one beat: `60 / bpm × {1/4: 1, 1/8: 0.5, 1/16: 0.25, 1/32: 0.125}` s. The rate is the last `Vitals.pr` (`Hub._on_vitals`; reset to none when the device disconnects), passed to `trigger(kind, bpm)`; none or outside 20–300 bpm → 120 bpm (`backend/rate.py`; the user's rule: fall back silently). At 1/4 a note lasts about one beat, so it is often cut short by the next retrigger. `call_later` uses the event loop's 15.6 ms Windows clock, so note-offs can be ~15 ms off. A retrigger of a still-sounding note sends its note off first, so on/off stay paired. `all_notes_off()` ends sounding notes on pause, channel change, disconnect and shutdown. The device's `beat` mark plays the systolic note (`KIND_ALIASES`). Notch/diastolic notes are sent when the mark is detected, i.e. ~0.3–0.6 s after the event (see `Detection.md`).
- **Errors**: a port that can't be opened, or that fails while sending (e.g. a USB interface unplugged), is closed and the message is shown under the port selector.
- **Persistence is in the browser**, not on the server: see "Settings in the browser" below.

## MIDI clock (`backend/clock.py`)

The user asked for MIDI clock (24 ticks per quarter note, plus Start/Stop) with the systolic peaks as the downbeats: each heartbeat is a quarter or a half note (`fraction`), so a 70 bpm pulse drives a 70 or 140 bpm clock. Settings live in `MidiClock` and appear as `status.midi.clock`: `enabled`, `auto` (auto start/stop), `sensitivity` 0–100, `fraction` "1/4" | "1/2".

- **Its own thread** (`midi-clock`): asyncio's timers fire in 15.6 ms steps on Windows; a tick is due every 15–40 ms. The thread sleeps with `time.sleep` (≤ 5 ms at a time; high resolution on Windows since Python 3.11) and sends through `MidiOutput.send_realtime()`. A lock in `MidiOutput` serialises the clock thread and the notes/CC on the port; a send failure is handed back to the event loop (`call_soon_threadsafe`) to update the status. Measured on macOS with the simulator: ticks median 0.8 ms late, p95 ~4 ms, max ~15 ms. An isolated sleep loop shows the same tail, so this is the OS scheduling floor, not the GIL (a shorter switch interval, a higher QoS class and spinning the last ms made no consistent difference). Not yet measured on Windows.
- **Heartbeat times come from sample numbers** (`SampleTimes`): marks arrive 0.2–0.6 s late and BLE delivers in bursts. The smallest arrival-minus-sample-time over the last 10 s gives the offset to `perf_counter`, since bursts only ever make packets late. It resets after a 1 s gap in the stream.
- **Phase-locked loop** (`BeatPll`), second order, updated once per heartbeat. It keeps 10 s of its own phase (piecewise linear segments) so it can measure how far off it was at the moment the heartbeat happened. The error, net of corrections still under way, adjusts the tempo (integral) and is worked off by the next expected heartbeat as a temporary tempo change (proportional). The clock never jumps or skips ticks while running. Gains come from one time constant τ in heartbeats: both poles at a = e^(−1/τ), so Kp = 1 − a², Ki = (1 − a)² (critically damped; derived for the per-beat discrete loop). Sensitivity maps logarithmically to τ = 16 (0, "smooth") … 1.5 (100, "tight"); 50 → ~4.9.
- **The trade-off is physiological**: breathing alone moves the heart rate ±5–10% over 10–25 beats, so the beats drift ±100–200 ms against any smooth tempo. `tools/clock_sim.py` on the clean recording: τ 1.5 → beats 47 ms off (median), clock tempo changing 3.8 bpm rms from beat to beat; τ 4 → 64 ms / 2.1 bpm; τ 16 → 138 ms / 1.2 bpm. A correction never moves the tempo more than 50% (`MAX_SLEW`), so the clock can't stall.
- **Robustness**: a heartbeat less than 0.6 beat after the last trusted one is a double detection and is ignored (a missed beat needs nothing: the phase error is the same). An earlier version rejected beats with a phase error > 0.3 beat; with heart-rate variability that threw away real beats at slow settings, so it was replaced. The loop locks afresh (tempo from the interval, full phase correction) on the first two beats, after 4 s without a trusted beat, after 3 "double detections" in a row, or when the median of the last 5 intervals is > 20% off its tempo and the last 3 intervals agree within 20% (a steady new tempo). Without that last condition, the erratic "beats" of a finger coming out (tested live on 2026-10-18: intervals of 50, 81, 56, 88, 79 bpm) relocked the clock to ~79 bpm for a ~60 bpm pulse, which with auto start/stop off it then kept. Before the first lock, the device's pulse rate (or 120 bpm) sets the tempo.
- **Start/Stop are tied to the clock running**, never to settings changes: the clock thread works out on every step whether the clock should run, and sends Start just before the first tick when it starts and Stop when it stops (pause, disable, port closing, shutdown, and with auto: pulse lost). The clock always sends Start before ticking.
  - **Auto start/stop on**: it should run while playing, locked, and within 5 s (`STOP_AFTER`) of the last trusted heartbeat. The first tick falls on the loop's next whole beat, i.e. a heartbeat.
  - **Off**: it should run whenever playing. Start goes out at once: `BeatPll.realign()` shifts the loop's phase (its whole history, so later errors stay consistent) to a whole beat now, at the last tempo. The loop then pulls the beats onto the pulse at its normal pace. Locking afresh while the clock runs is "gentle" (phase corrected with the loop's Kp, not all at once), so even the first lock after a cold start slides rather than lurches. In simulation: after a resume, −69 ms to within ±5 ms over ~6 heartbeats at the default sensitivity.
  - **History**: the first version had a "Start/Stop" setting meaning "send Start/Stop at all" (off: tick without them), and switching it restarted the clock, which sent Stop/Start on every click. The user pointed out that toggling must not send anything by itself, and that with it off, play/pause should still send Start/Stop (Start at once). Renamed to *Auto start / stop* (`auto`; a stored `transport` setting from before is ignored).
  - While paused, the loop keeps following the heartbeats, like the detector, so it's in step on resume.
- **Known limitation**: switching 1/4 ↔ 1/2 while running renumbers ticks from the loop's phase, so the receiver's bar count can end up shifted against the heartbeats until the next Start.
- **Both detection modes** drive it: `Hub._on_wave` passes `systolic` and the device's `beat` marks, paused or not.

## OSC output (`backend/osc.py`)

- **python-osc** `SimpleUDPClient`. UDP has no connection: *Connect* resolves the host (`getaddrinfo`; an unknown host shows an error) and opens a socket; nothing checks that a receiver exists. Host and port only take effect on the next connect, so the UI locks them while connected. A send failure (`OSError`) closes the output and shows the error, like MIDI.
- **Addresses**: `/<prefix>/data`, `/<prefix>/systolic`, `/<prefix>/notch`, `/<prefix>/diastolic`. The user asked for "<prefix>/data"; OSC requires a leading `/`, so it is added. `clean_prefix()` strips spaces, OSC pattern characters (`#*,?[]{}`) and empty parts; `/` inside the prefix is kept for nested addresses. Empty prefix → `/data` etc.
- **Device on**: `/<prefix>/device`, int 1 while the oximeter is connected, 0 otherwise (also while only "connecting"). `Hub._on_state` hands every device state to `set_device_on()` on both outputs, which send only when the on/off value changes; MIDI does the same as a CC (127/0, own number, default 2). Because a change can happen while an output is closed or disabled, the current value is also resent when the option is enabled, the port/socket opens and (MIDI) the channel changes.
- **Waveform**: the shared playout value divided by 127, rounded to 4 decimals, sent when it changes (~95–170 messages/s while the pulse moves, none on flat stretches). Same ~0.4 s delay as the CC.
- **Events**: one float, the pulse rate in bpm (`effective_bpm`), sent the moment the mark is detected. The device's `beat` mark is sent as `systolic`. Notch/diastolic come ~0.3–0.6 s after the event, like their MIDI notes.
- **State in `status.osc`**, partial updates via `osc_set`, like MIDI. Settings and whether it was connected are kept in the browser.
- **Testing**: send to a `pythonosc.osc_server.AsyncIOOSCUDPServer` on 127.0.0.1 in the test process; no external software needed. (That loopback is what exposed the timer pitfall above.)
### WebSocket protocol

All JSON. Server → page:

```json
{"type": "status", "device": {"state": "connected", "name": "OxySmart 6116", "address": "..."},
 "running": true, "detection": "waveform",
 "detectors": [{"id": "device", "label": "Device built-in", "kinds": ["beat"]},
               {"id": "waveform", "label": "Waveform analysis", "kinds": ["systolic", "notch", "diastolic"]}]}
{"type": "vitals", "spo2": 97, "pr": 81, "pi": 8.9}
{"type": "wave", "n": 12345, "samples": [53, 52, 51, 48, 46]}
{"type": "marks", "marks": [[12340, "systolic"], [12301, "notch"]]}
```

- `status` is sent on connect and on every change of device state, pause state or detection mode.
- `wave.n` is the running number of the first sample. **Marks refer to samples by that number and may arrive after their samples were sent** (the notch is only known ~0.5 s later), so the page must keep marks in a map keyed by sample number rather than attach them to incoming samples.

Page → server: `{"type": "set_running", "running": bool}`, `{"type": "set_detection", "detection": id}`, and `midi_refresh` / `midi_connect` (`port`) / `midi_disconnect` / `midi_set` (any part of `channel`, `cc`, `device`, `notes`). `status` also carries `midi`: ports, open port, error, channel, cc, device and notes settings; the full shapes are in the docstring of `backend/server.py`.

### Startup and shutdown (`oxibeat.py`)

- `serve()` calls `on_ready(url)` once listening; `oxibeat.py` passes `webbrowser.open_new_tab` (unless `--no-browser`).
- **Ctrl+C**: `asyncio.run` turns SIGINT into cancellation of `serve()`; its `finally` closes all page sockets (otherwise aiohttp's shutdown waits for them) and the web server, and cancelling `Device.run()` exits the `BleakClient` context, which disconnects from the device. Tested: shutdown is immediate and frees the port.
- An `OSError` at startup (e.g. port in use) is logged and exits with code 1.

## UI (`web/`)

Layout: transport bar on top (brand, play/pause, connection state, SpO2/rate/PI, beat light), a 240 px scrollable menu column on the left (Detection type; MIDI output: port, channel, a "Control change" row and three note rows, each with on/off and a number, velocity and length, and a "Clock" group with on/off, Start/Stop, heartbeat = 1/4 or 1/2 note, and a Smooth–Tight sensitivity slider; OSC output: host/port, prefix, four message rows showing their address). Notes and OSC messages for marks the current detector doesn't produce are shown greyed out (notch/diastolic in Device built-in mode), the graph filling the rest. **Dark by default**; the toggle at the top left of the transport bar switches to light. The choice is stored in `localStorage` (`oxibeat.theme`) and applied by an inline script in `index.html`'s head before the first paint (`data-theme` on `<html>`); the OS preference is deliberately ignored. All colours are CSS variables in `style.css`. Space toggles play/pause wherever the focus is, except in text and number fields and selects, where it types or opens the list. It used to work only with nothing focused, so after clicking a checkbox or the slider, Space toggled that control instead; now the keydown is taken and the keyup's default is prevented too, because buttons and checkboxes activate on keyup (without that, Space on the focused play button toggled twice). Below 700 px width the menu stacks above the graph.

### Settings in the browser (`app.js`)

The server holds the live settings (shared by all pages) and forgets them on restart. Each page keeps a copy in `localStorage` under `oxibeat.settings`: `{detection, midi: {channel, cc, device, notes, velocity, length, clock}, midiPort, osc: {host, port, prefix, wave, events, device}, oscConnected}`.

- On the **first `status` of every WebSocket connection** (page load, or reconnect after a server restart) the page sends its stored settings to the server: `set_detection`, one `midi_set`, and `midi_connect` if a port is stored, the server has none open, and the port is in the list; then `osc_set` and, if `oscConnected`, `osc_connect`. With nothing stored, it stores the server's settings instead.
- After that, every `status` is written back to storage, so changes from any page are kept.
- `midiPort` and `oscConnected` are only written on the user's Connect/Disconnect click, so a port that drops out because of an error is retried next time.
- When several browsers or machines are connected, the last page to (re)connect pushes its copy.
- All storage access is in try/catch; without storage the UI works, it just doesn't remember.

### The scrolling graph (`app.js`)

- **Playout queue**: samples arrive in bursts (see `Device.md`), so they're queued and played out at the sample rate, keeping ~0.4 s (`TARGET_DELAY`) queued as a jitter buffer. The display is therefore ~0.4 s behind the device; this only affects the display, not detection or outputs.
- **Rebuffering**: when the queue runs dry (the stream stopped, resume after pause), playback waits until it holds the full target delay again before moving, a brief freeze instead of a stutter while it refills.
- **Speed control**: the playout speed is nudged (0.8–1.25×) to keep the queue near its target, and the speed itself is low-pass filtered, so bursts don't show up as jerks.
- **Sample rate**: measured from arrivals over 5 s but only adopted when it differs from the current value by more than 15 %, because it sets the x scale. Arrivals are forgotten after a silence of more than 1 s, so a pause in the stream (device off, reconnect) isn't counted as a slow rate. *(Fixed after the user saw jitter when the stream restarted.)* *(Decision after a bug: re-estimating it on every packet made the x axis rescale constantly, which looked like twitching at the end of the timeline.)*
- Backlog over 1 s (e.g. after the tab was hidden) is dropped down to the target delay; on resume after pause the queue is cleared.
- 6 s visible, newest sample at the right edge; sub-sample offset for smooth scrolling; faint grid with one vertical line per second, scrolling with the data. Fixed y range 0..127.
- **Marks** are drawn as full-height vertical lines; style per kind in the `MARKS` table in `app.js` (colour token + dash pattern), with a legend overlay listing the current detector's kinds. Kinds marked `beat: true` (device `beat`, `systolic`) flash the beat light when their sample is displayed.

## Decisions worth knowing

- **Python package is `backend/`**, not `oxibeat/`, so it doesn't clash with the `oxibeat.py` entry script. (It was `oxibeat/` with `python -m oxibeat` at first.)
- **No frontend build or libraries**: the UI is small; canvas drawing is a few dozen lines.
- **`--simulate`** exists so the UI, MIDI and OSC can be developed without the device; its pulse has a notch and a diastolic hump, and it sets the beat flag at the systolic peak.
- **Workspace venv**: dependencies live in `synestechia/.venv` (workspace root, shared), as the user asked.
- **Dependencies**: `aiohttp`, `bleak` (BLE, cross-platform; bleak 3.x in use), `numpy` + `scipy` (filters for detection), `mido` + `python-rtmidi` (MIDI), `python-osc` (OSC).

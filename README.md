# oxibeat

Turns real-time data from a Viatom PC-60FW pulse oximeter (Bluetooth LE) into MIDI and OSC for interactive installations. A Python server talks to the device and serves a web UI that shows the live signal.

## Setup

```
git clone --recursive https://github.com/MartinBloedorn/oxibeat.git
```

`--recursive` also fetches `external/viatom_pc60fw`, the reference implementation by [sza2](https://github.com/sza2/viatom_pc60fw) that the device protocol was worked out from (kept for reference and credit; oxibeat doesn't import it).

Then, with Python 3.11, create a virtual environment and install the dependencies. In the author's setup the venv lives one level up, at the root of the `synestechia` workspace that contains `oxibeat`:

```
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r oxibeat\requirements.txt
```

On macOS, same idea, with Python 3.11 specifically (e.g. `brew install python@3.11`):

```
python3.11 -m venv .venv
.venv/bin/python -m pip install -r oxibeat/requirements.txt
```

Python 3.11 isn't a style preference here: `python-rtmidi` only publishes prebuilt wheels up to cp3.12, so on 3.13/3.14 pip falls back to compiling RtMidi's bundled C++ from source. On macOS that build fails outright — the JACK code path it pulls in calls `sem_timedwait`, a POSIX semaphore function Darwin doesn't implement. If `pip install` dies on `python-rtmidi` with an error like `use of undeclared identifier 'sem_timedwait'`, delete `.venv` and recreate it with an explicit `python3.11` (not the bare `python3`, which Homebrew keeps pointed at its latest release) and reinstall.

## Running

From the `oxibeat` folder, with the venv activated (`..\.venv\Scripts\activate` on Windows, `source ../.venv/bin/activate` on macOS):

```
python oxibeat.py
```

This opens the UI (http://localhost:8080/) in a browser tab. Ctrl+C disconnects from the device and stops the server.

The server keeps scanning for the oximeter and connects whenever it shows up. The device switches itself off a few seconds after the finger is removed; the server notices (on the disconnect event, or after 5 s without data) and goes back to scanning.

| Option | |
|---|---|
| `--port 8080` / `--host 127.0.0.1` | where the UI is served; use `--host 0.0.0.0` to reach it from other machines |
| `--name OxySmart` | only connect to devices whose BLE name starts with this (by default, anything named PC-60F… / OxySmart… or advertising the Nordic UART service) |
| `--address XX:XX:...` | connect only to this device |
| `--simulate` | synthetic pulse signal, for working without the oximeter |
| `--detection device` | beat detection to start with: `waveform` (default) or `device`; also switchable in the UI |
| `--no-browser` | don't open a browser tab |
| `-v` | debug logging |

## UI

- **Transport bar:** light/dark toggle (dark by default; the choice is remembered in the browser), play/pause (also the Space key, wherever the focus is except in a text field or list), the device connection state, and SpO₂, pulse rate and perfusion index as shown on the device's display. Pausing is server-side: it stops the data going to every open page and to MIDI and OSC, not just the page where you clicked.
- **Menu (left):** settings. **Detection type** picks how beats are found (shared by all open pages):
  - *Device built-in* uses the beat flag the oximeter puts in its waveform.
  - *Waveform analysis* runs oxibeat's own detector on the waveform and marks the systolic peak, the dicrotic notch and the diastolic peak of every beat. See below.
- **MIDI output (left menu):** pick an output port (↻ re-lists the system's ports), Connect/Disconnect, and the channel everything is sent on.
  - *Control change → Waveform*: sends the waveform as a control change on the chosen CC number. Like the graph, it is buffered ~0.4 s to even out Bluetooth's bursts and interpolated between samples, so it moves smoothly (~100 updates/s, typically 1 step at a time) instead of in jumps.
  - *Notes*: a note for each systolic peak, dicrotic notch and diastolic peak, each with its own on/off and note number. In *Device built-in* mode only the systolic note is available; it's played by the device's beat flag.
    - *Fixed velocity* (on by default, 100); when off, the velocity is the pulse rate in bpm (capped at 127).
    - *Length* 1/4, 1/8, 1/16 or 1/32 of a note at the current pulse rate (a quarter note is one beat), e.g. 1/16 at 72 bpm = 208 ms.
    - The pulse rate is the device's (`RATE` in the transport bar); when it has none or an implausible one (outside 20–300 bpm), 120 bpm is used.
  - *Clock*: MIDI clock (24 ticks per quarter note) that follows the pulse, with each systolic peak (or the device's beat flag) on a beat. See below.
  - Nothing is sent while paused.
- **OSC output (left menu):** host and port (editable while disconnected), Connect/Disconnect, and the address prefix (default `oxibeat`). OSC goes over UDP, so "connected" means oxibeat is sending; it can't tell whether anything is listening. Messages, each with its own on/off:
  - `/oxibeat/data` *float*: the waveform normalised to 0–1, smoothed like the graph and the MIDI CC (sent when it changes, up to ~100/s).
  - `/oxibeat/systolic`, `/oxibeat/notch`, `/oxibeat/diastolic` *float*: on every detected mark, carrying the pulse rate in bpm, e.g. `/oxibeat/systolic 94.0` (same rate rule as for MIDI: 120 when the device has none). In *Device built-in* mode only `/systolic` is available, sent on the device's beat flag.
  - The prefix may contain `/` for nested addresses (`stage/left` gives `/stage/left/data`); spaces and OSC pattern characters (`#*,?[]{}`) are removed.
- **Settings are kept in the browser** (localStorage): detection type, MIDI port, channel, CC and notes, and OSC host, port, prefix, messages and whether it was connected. The server holds the live settings for all pages but forgets them when it restarts; a page hands its saved copy back to the server whenever it connects, including reconnecting the saved MIDI port if it's available.
- **Graph:** the pulse waveform, scrolling right to left, with a vertical line at every detected mark (legend top left).

## Waveform analysis

`backend/detection.py`, run on the server as the samples arrive (50 Hz):

1. **Systolic peak:** Elgendi et al.'s two-moving-average method (PLoS ONE 2013), as in NeuroKit2: band-pass 0.5–8 Hz, square the positive part, and find the stretches where a 111 ms average exceeds a 667 ms average plus an offset; the waveform's maximum there is the peak. The long average trails instead of being centred, which cuts the delay to about 0.2 s at no measurable cost in accuracy.
2. **Dicrotic notch and diastolic peak**, along the lines of pyPPG (Goda, Charlton & Behar 2024), on a Savitzky–Golay smoothed beat. If the pulse dips after the systolic peak, the dip is the notch and the next maximum is the diastolic peak. If there's only a shoulder (typical for fingertips, older or stiffer arteries), the notch is the "e" wave of the second derivative and the diastolic peak is where the down-slope is flattest (first-derivative maximum).

The notch and diastolic peak can only be judged once the down-slope has been seen, so they're reported about 0.6 beat intervals after the systolic peak (roughly 0.3–0.6 s after they happen); their marks carry the sample they belong to, so the graph shows them in the right place.

## MIDI clock

*Heartbeat =* 1/4 or 1/2 note sets what one heartbeat is worth, so a 70 bpm pulse drives a 70 or a 140 bpm clock. The heart doesn't keep strict time: breathing alone moves the rate by ±5–10% over a few breaths. So the clock runs at its own steady tempo, and a phase-locked loop nudges it towards each detected heartbeat. It never jumps or drops ticks. Heartbeats are timed by when they happened, not when they were detected, so the clock's beats line up with the pulse itself, ahead of the notes.

- **Sensitivity** sets how hard it follows. *Tight* (right) catches up within one or two heartbeats; beats land ~50 ms from the pulse, but the tempo wobbles by a few bpm from beat to beat. *Smooth* (left) takes ~16 heartbeats; the tempo is steady, and beats drift up to ~150 ms either side of the pulse as the heart speeds up and slows down. The default is in between (~5 heartbeats).
- **Start and Stop** are sent whenever the clock starts or stops, and only then; switching *Auto start / stop* sends something only if that starts or stops the clock.
  - *Auto start / stop* on: the clock runs while there is a pulse (and oxibeat is playing). It sends Start, landing on a heartbeat, once it has found the pulse (two heartbeats), and Stop 5 s after the pulse is lost (finger out, device off) or on pause. When the pulse returns it starts again on a heartbeat.
  - Off: the clock follows play/pause only. Play sends Start at once and the clock runs at the last tempo; its beats then move onto the pulse gradually, at the pace the sensitivity sets. Without a pulse it keeps the last tempo until paused, which sends Stop.
- Missed or doubled detections are absorbed. If the pulse jumps or comes back after a gap, the clock locks onto it afresh within a beat or two.

`tools/clock_sim.py` measures all of this offline; the design is in [doc/Architecture.md](doc/Architecture.md).

## Code

- `oxibeat.py` is the command-line entry point.
- `backend/protocol.py` parses the device's packets (format described in `external/viatom_pc60fw/README.md` and `doc/Device.md`; bit 7 of a waveform sample is the beat flag).
- `backend/device.py` handles the BLE scan / connect / reconnect loop (using [bleak](https://github.com/hbldh/bleak)).
- `backend/detection.py` holds the beat detectors.
- `backend/midi.py` is the MIDI output ([mido](https://mido.readthedocs.io) with python-rtmidi), `backend/clock.py` the MIDI clock (a phase-locked loop on its own thread), `backend/osc.py` the OSC output ([python-osc](https://github.com/attwad/python-osc)).
- `backend/playout.py` smooths the waveform for both outputs; `backend/rate.py` holds the pulse-rate rule they share.
- `backend/server.py` serves `web/` and relays data to pages over a WebSocket at `/ws`; the message format is at the top of that file.
- `web/` is the UI: plain HTML/CSS/JS, no build step.
- `tools/` has scripts to record the device, replay recordings through the detector, score it on synthetic data, simulate the MIDI clock, and screenshot the UI; `data/recordings/` has real recordings.

Design notes, findings about the device, the detection research and results, and the current status are in [doc/](doc/README.md).

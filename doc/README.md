# oxibeat documentation

Start here when picking up the work.

| Document | Contents |
|---|---|
| [Device.md](Device.md) | The oximeter: BLE, packet format, waveform, beat flag timing, artifacts, everything observed on the real unit |
| [Architecture.md](Architecture.md) | Components, data flow, WebSocket protocol, UI internals, design decisions |
| [Detection.md](Detection.md) | Beat detection: research, algorithm, parameters, results, limitations, how to tune |
| [Development.md](Development.md) | Environment, running, tools, how to test with and without the device |

The user-facing overview is `../README.md`; the project brief is `../CLAUDE.md`.

## Status (2026-10-18)

Done and tested on the real device:

1. **Python server** (`oxibeat.py` + `backend/`): finds the oximeter (it advertises as `OxySmart 6116`), streams, survives the device switching off and on, serves the UI, opens a browser tab, shuts down cleanly on Ctrl+C.
2. **Web UI** (`web/`): transport bar (play/pause, connection state, SpO2 / rate / PI, beat light), settings menu on the left, smoothly scrolling waveform graph with detected marks and a legend.
3. **Detection modes**, switchable in the UI or with `--detection`:
   - *Device built-in*: the oximeter's own beat flag;
   - *Waveform analysis*: oxibeat's streaming detector marking systolic peak, dicrotic notch and diastolic peak per beat.
4. **MIDI output** (left menu): port selection/refresh, connect/disconnect, channel; waveform as a smoothed CC (buffered and interpolated like the graph); a note per systolic peak / dicrotic notch / diastolic peak with on/off and note number each; fixed or rate-based velocity; note length 1/4–1/32 of the pulse rate. Tested end to end with a fake port, the simulator and a headless browser (settings restore); real ports open and close, but no real MIDI has been sent to a synth or DAW yet.
5. **OSC output** (left menu): host/port, connect/disconnect, prefix (default `oxibeat`); `/<prefix>/data` (waveform 0–1, smoothed) and `/<prefix>/systolic|notch|diastolic` (pulse rate as float), each switchable. Tested end to end over UDP to a local python-osc receiver, and in a headless browser; not yet with the user's own OSC software.
6. **MIDI clock** (MIDI menu, "Clock"): 24 ppqn clock phase-locked to the heartbeats (1 heartbeat = 1/4 or 1/2 note), Start/Stop sent only when the clock starts or stops (*Auto start / stop*: with the pulse, on a heartbeat, Stop 5 s after it's lost; off: with play/pause only, Start at once and gradual sync), sensitivity slider (loop time constant 16 → 1.5 heartbeats). Simulated offline on the recordings (`tools/clock_sim.py`), tested end to end on macOS into a virtual MIDI port with the simulator, and confirmed working by the user with Ableton Live on macOS. Not yet tested on Windows (the clock thread relies on Python 3.11's high-resolution `time.sleep` there).
7. Settings (detection, MIDI, OSC) are kept in the browser's localStorage and pushed back to the server when a page connects. Light/dark theme toggle, dark by default.

Not started:

- Vitals (SpO2, rate, PI) are not sent over MIDI or OSC.
- Real-world test of MIDI and OSC with the user's software (DAW, TouchDesigner, etc.).
- Further menus/options (the user said more will come).

## Suggested next steps

1. Try MIDI and OSC with the user's actual software and adjust (latency of the smoothed waveform vs. events, message rates).
2. Settings are kept in the browser (localStorage); if the server should run headless with saved settings (no page open), add server-side persistence.
3. Detection improvements listed in `Detection.md` → "Known limitations and ideas", mainly lower latency for MIDI use and recordings at higher heart rates.

## Working agreements with the user

- Keep solutions simple; ask when something is genuinely the user's call.
- The user can put the oximeter on for live tests when asked.
- Dependencies go in the workspace venv (`synestechia/.venv`).

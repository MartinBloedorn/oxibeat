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
4. **MIDI output** (left menu): port selection/refresh, connect/disconnect, channel; waveform as a smoothed CC (buffered and interpolated like the graph); a note per systolic peak / dicrotic notch / diastolic peak with on/off and note number each. Tested end to end with a fake port, the simulator and a headless browser (settings restore); real ports open and close, but no real MIDI has been sent to a synth or DAW yet.

Not started:

- **OSC output**. Hook: `Hub._on_marks()` in `backend/server.py` receives every detected mark (MIDI is triggered there too); `Hub._on_wave()` gets the samples; `Hub.running` is the play/pause gate; vitals arrive in `Hub._on_vitals()`. `backend/midi.py` is the model to follow (server-side settings in `status`, partial updates from the page).
- Vitals (SpO2, rate, PI) are not sent over MIDI.
- Further menus/options (the user said more will come).

## Suggested next steps

1. OSC output (e.g. `python-osc`), same pattern as MIDI. Ask the user for the address scheme.
2. Settings are kept in the browser (localStorage); if the server should run headless with saved settings (no page open), add server-side persistence.
3. Detection improvements listed in `Detection.md` → "Known limitations and ideas", mainly lower latency for MIDI use and recordings at higher heart rates.

## Working agreements with the user

- Keep solutions simple; ask when something is genuinely the user's call.
- The user can put the oximeter on for live tests when asked.
- Dependencies go in the workspace venv (`synestechia/.venv`).

# Development

## Environment

- Windows 10, Python 3.11. The venv is at the **workspace root**: `synestechia/.venv` (shared by the workspace, by the user's choice).
- Setup: `py -3.11 -m venv .venv` then `.venv\Scripts\python -m pip install -r oxibeat\requirements.txt` (from the workspace root).
- Bare `python` on this machine may be the Microsoft Store stub; use `..\.venv\Scripts\python` from `oxibeat/`, or activate the venv first.
- Git: https://github.com/MartinBloedorn/oxibeat (branch `main`). `external/viatom_pc60fw` is a submodule of [sza2/viatom_pc60fw](https://github.com/sza2/viatom_pc60fw), kept as the reference the protocol work is based on (credit to its author); no oxibeat code imports it. Clone with `--recursive`, or run `git submodule update --init` after cloning.

## Running

From `oxibeat/`:

```
python oxibeat.py                         # real device, opens the browser
python oxibeat.py --simulate              # synthetic pulse, no device needed
python oxibeat.py --detection waveform    # start with oxibeat's own detector
python oxibeat.py --port 8091 --no-browser -v
```

## Tools (`tools/`)

| Tool | Use |
|---|---|
| `record.py <csv> <seconds>` | Record the live waveform + device beat flags. Waits up to 2 min for the device. Can't run alongside `oxibeat.py` (one BLE connection). |
| `replay.py <csv> [--plot x.html]` | Run the waveform detector over a recording; prints stats, optional plot. |
| `evaluate_synthetic.py` | Score the detector on synthetic beats with known answers. Run after any detection change. |
| `screenshot.py <url> <png>` | Screenshot a page (the live UI or a plot) with headless Edge via the DevTools protocol. |

Recordings are in `data/recordings/` (CSV, one row per sample: `value,flag`). See `Detection.md` for what each contains.

## Testing without and with the device

- **UI work**: `--simulate`, then look at it, or `python tools/screenshot.py http://localhost:8080/ ui.png --wait 6` (the UI is dark by default; `--js "document.getElementById('theme').click()"` switches to light before the capture; `--js` runs any JavaScript on the page first, e.g. to click controls). Edge's built-in `--screenshot` flag fires before the graph has data, which is why the tool exists.
- **Server**: connect a WebSocket client to `/ws` and check message counts. Example used during development: count `wave`/`marks`/`vitals` messages for a few seconds, send `set_running: false`, check that only `status` arrives.
- **MIDI without hardware or noise**: run `serve()` in-process after replacing `backend.midi.mido.open_output` with a fake port object (`send()` appends to a list, `close()`) and `mido.get_output_names` with a fixed list; then drive the server over `/ws` (`midi_connect`, `midi_set`, ...) and inspect the recorded messages. The machine's real outputs are `Microsoft GS Wavetable Synth 0` (makes sound) and `TouchOSC Bridge 1` (forwards to the user's TouchOSC devices): don't send to them without asking.
- **Detection**: `tools/replay.py` on the recordings and `tools/evaluate_synthetic.py`; neither needs the device.
- **Device**: the user can put the oximeter on when asked. It switches off when the finger is out, so ask right before you need it. A disconnect/reconnect test is: run the server, ask the user to remove the finger for ~15 s and put it back, check the log shows `scanning` then `connected`.
- **Ctrl+C**: hard to send to a child process on Windows. During development `signal.raise_signal(SIGINT)` from a timer thread inside the process was used to simulate it (same code path as a real Ctrl+C).

## Agent notes (Claude Code on this machine)

- The Git Bash tool failed intermittently in this environment ("No such file or directory" on its cwd file); PowerShell worked throughout.
- `node` on this machine is old (no ES modules), so JS tooling that needs modern Node won't run.

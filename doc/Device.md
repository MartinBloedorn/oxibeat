# The oximeter: what we know

Everything here was either taken from the reference implementation in `external/viatom_pc60fw` or observed on the actual unit on 2026-10-18. Observed facts are marked *(observed)*.

## The unit

- A Viatom PC-60FW fingertip pulse oximeter. **Our unit is rebranded and advertises as `OxySmart 6116`** *(observed)*, not `PC-60F…` as the reference implementation assumes. BLE address `00:00:00:00:17:E4`.
- It **switches itself off a few seconds after the finger is removed**, and comes back (advertising again) when a finger is inserted. Disconnects are the normal case, not errors.
- BLE allows **one connection at a time**: while `oxibeat.py` is connected, `tools/record.py` (or any other client) cannot connect, and vice versa.

## Bluetooth LE

- Nordic UART Service (NUS), service UUID `6e400001-b5a3-f393-e0a9-e50e24dcca9e`, which the unit includes in its advertisement *(observed)*.
- Data comes as notifications on the TX characteristic `6e400003-b5a3-f393-e0a9-e50e24dcca9e`. Nothing needs to be written to the device to start the stream; subscribing is enough *(observed)*.
- The RX characteristic (`6e400002-…`) is unused.

How `backend/device.py` finds it: a device matches if its name starts with `PC-60F` or `OxySmart`, **or** it advertises the NUS service. `--name` / `--address` narrow this down (other NUS gadgets nearby would also match by default).

## Packet format

From `external/viatom_pc60fw/README.md`, confirmed in use:

```
AA 55 | dir | len | payload ... | crc
```

- `AA 55` sync word; `dir` is `0x0F` (device to host) or `0xF0`; `len` counts the payload *including* the CRC byte.
- CRC is CRC-8/MAXIM over the whole packet; computed over the packet including the CRC byte, it is 0. Implemented in `backend/protocol.py` (no external CRC library).
- Packets can be split across notifications and several can share one; the parser buffers and resyncs on the sync word (and skips a sync word whose packet fails the CRC).

Payload types (first payload byte = function):

| func | content | handled |
|---|---|---|
| `0x01` | vitals: `[01, SpO2, PR, ?, PI×10, ?, ?]` | yes → `Vitals(spo2, pr, pi)` |
| `0x02` | waveform: `[02, s1, s2, s3, s4, s5]` | yes → `Wave(samples, pulses)` |
| `0x21` | unknown (`21 02 00 00 00`) | ignored |
| `0x03` (dir `F0`) | unknown | ignored |

- **PR** is read from one byte. The byte after it might be a high byte (rates above 255 are not physiological, so this hasn't mattered); unverified.
- The device reports **0 for SpO2/PR/PI while it has no reading** (e.g. finger just inserted); the UI shows "–" for 0.

## The waveform

- **7-bit samples (0..127), 5 per packet, ~50 samples/s** *(observed: 50.6/s measured over 8 s; detection assumes exactly 50 Hz)*.
- **Bit 7 of a sample is the device's beat flag**: the sample value is `byte & 0x7F` and the flag is `byte & 0x80`. The reference README describes this as a "spike" in every pulse cycle.
- **The beat flag comes 4–5 samples (80–100 ms) after the actual systolic peak**, very consistently *(observed: over 69 beats of a clean recording, offsets were 3 samples ×2, 4 ×53, 5 ×14; see `tools/replay.py`)*. The reference README says "third sample after the peak".
- The waveform is the device's display curve: already filtered and auto-scaled (amplitude varies from beat to beat and after finger movement), so absolute values mean little.
- **Notifications arrive in bursts** *(observed)*: packets are ~100 ms apart on average (median gap 119 ms) but gaps of up to ~480 ms occur, followed by several packets at once. Anything that plays the stream out in real time needs a jitter buffer (the UI holds 0.4 s).
- **Artifacts when the finger moves** *(observed, see `data/recordings/2026-10-18_rest_artifacts_90s.csv`, around 20–30 s)*: the waveform clips at 127 for several samples, then flatlines (often at 0) for a second or more, then recovers with irregular shapes while the device re-scales. The device's own flag also fires irregularly during the recovery.

## The pulse shape on this unit

On the recordings made so far (one person, at rest, fingertip):

- A clear systolic peak, then usually a **real dip (dicrotic notch) followed by a small diastolic hump**; sometimes only a shoulder or a plateau.
- Notch **~180 ms after the systolic peak** (range 100–220 ms on a clean recording), diastolic peak **~260 ms after** (240–340 ms).
- Heart rate 60–80 bpm at rest.
- The 7-bit quantisation makes the slow parts of the curve step-like (several equal samples in a row), which matters for derivative-based detection (we smooth with Savitzky–Golay before differentiating).

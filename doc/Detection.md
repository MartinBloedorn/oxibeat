# Beat detection

Code: `backend/detection.py`. Tools: `tools/replay.py`, `tools/evaluate_synthetic.py`, `tools/record.py`. Recordings: `data/recordings/`.

## Goal

Besides the beat flag the device sends ("Device built-in" mode), oxibeat runs its own detector ("Waveform analysis" mode) that marks, for every beat:

- the **systolic peak** (top of the main pulse wave),
- the **dicrotic notch** (the dip, or the bend, after it, when the aortic valve closes),
- the **diastolic peak** (the smaller reflected wave after the notch).

These are meant as triggers for MIDI/OSC, so the detector has to work **streaming**, with as little delay as possible, on the device's 50 Hz 7-bit waveform.

## Interface

Every detector has `id`, `label`, `kinds` (the mark kinds it emits), `reset()`, and `feed(n0, samples, pulses) -> list[Mark]`, called once per 5-sample packet. `n0` is the running number of the first sample; a `Mark` is `(n, kind)` with `n` the sample's running number, possibly referring to an earlier packet. Detectors are registered in `DETECTORS`; the UI's dropdown and the `--detection` option list whatever is registered there, so a new detector only needs adding to that dict.

## Research (October 2026)

- **There is no widely accepted standard** for automatic PPG fiducial-point detection (noted by the nomenclature review, PMC10292016), and no published head-to-head benchmark of *real-time* dicrotic-notch detectors.
- **pyPPG** (Goda, Charlton & Behar, Physiol. Meas. 2024; https://pyppg.readthedocs.io) is the most complete open-source reference. It works offline on whole recordings:
  - peaks/onsets with its "PPGdet" detector (an improved Aboy detector: resample to 75 Hz, 10 s windows, Chebyshev band-passes, FFT heart-rate estimate);
  - **dicrotic notch** = argmax of a weighted second derivative (APG) between the systolic peak and the next onset, with the weight centred on an expected notch time;
  - **diastolic peak** = first local maximum of the PPG after the notch within 80 % of the beat; if there is none, the first local maximum of the first derivative (VPG) after the APG "e" point;
  - APG **e point** = highest local maximum of APG between the systolic peak and 60 % of the beat.
- **Elgendi et al. 2013** (PLoS ONE 8(10): e76585; PMC3805543) is the best-validated systolic-peak detector for PPG (sensitivity 99.89 %, +P 99.84 % on 5071 beats): band-pass 0.5–8 Hz, square the positive part, compare a 111 ms moving average with a 667 ms one plus an offset (β = 0.02 × mean); each stretch where the short one is higher ("block of interest", at least 111 ms wide) holds one peak. NeuroKit2's `ppg_findpeaks(method="elgendi")` uses these values with a 0.3 s minimum delay between peaks.
- **Iterative envelope mean** (IEM; PMC10942507, 2024) is the most accurate recent notch method (≈5 ms error vs ≈97 ms for the plain second-derivative method), but needs 4 s windows, spline envelopes and iteration; offline only. Its rule that the notch is **at least 0.1 s after the systolic peak** was adopted.
- Chakraborty et al. (IJBET 2021) use first/second derivatives and slope reversal for the notch; only the abstract was available.
- Physiology: the notch and diastolic wave come from wave reflection; they **fade with age and arterial stiffness**, leaving only a shoulder. Fingertip PPG is smoother than arterial pressure. So a detector must handle both "valley" and "shoulder" beats.

## The algorithm

### 1. Systolic peak: streaming Elgendi

Per sample: causal 2nd-order Butterworth band-pass 0.5–8 Hz (`sosfilt` with carried state), square the positive part, then:

- `ma_peak` = mean of the last 111 ms (6 samples), `ma_beat` = mean of the last 667 ms (33 samples);
- threshold = `ma_beat + 0.02 × running mean of the squared signal` (the mean is over ~10 s; cumulative at startup);
- a block opens when `ma_peak` rises above the threshold and closes when it drops below; blocks shorter than 111 ms are ignored;
- the systolic peak is the **maximum of the raw waveform** from 0.1 s before the block to 0.05 s after it (the causal filter shifts the block later than the peak; searching the raw signal also gives the true sample);
- after more than 2.5 s without a systolic peak (`MAX_INTERVAL`; finger out while the device still streams, long artifacts) the next peak starts afresh: no interval is recorded and the old peak is forgotten. *(Bug fixed 2026-10-18: after a gap longer than the 6 s history, the old peak had left the history, the onset lookup raised in `islice` and every following beat failed the same way.)*
- rejected: peaks less than 0.3 s after the previous one; peaks in a clipped stretch (3+ samples at 127); peaks less than 4 units above the minimum of the preceding second (flatline). The last two were added after seeing finger-movement artifacts on the real device.

**Deviation from the paper:** offline, both averages are centred on the same sample, which needs a third of a second of future samples. Here the long average **trails** (it acts as an adaptive threshold from the recent beat) and only the short one is effectively centred. This cut the reporting delay from ~500 ms to ~220–240 ms with no measurable change in accuracy on the synthetic tests.

### 2. Dicrotic notch and diastolic peak: per beat, pyPPG-style

When a systolic peak is found, the beat is queued for analysis over a window from the peak to **peak + 0.6 × beat interval** (median of the last 5 intervals; 0.8 s before any are known), cut short at the next beat's onset (minimum between the two peaks) if the next peak comes first. The analysis runs once that window plus 4 samples of look-ahead has arrived:

1. Smooth the window and take first and second derivatives with a **Savitzky–Golay filter (7 samples, cubic)**. Needed because the 7-bit samples come in flat steps.
2. Search from **0.1 s after the systolic peak** to the window end.
3. **Valley beat:** the first sample where the slope turns from falling to rising is a candidate notch (lower of the two samples around the sign change). If the pulse then rises to a maximum (slope turns rising → falling) that is at least `max(1, 2 % of the pulse amplitude)` higher, that valley is the **notch** and that maximum the **diastolic peak**.
4. **Shoulder beat** (no qualifying valley): the **notch** is the highest positive local maximum of the second derivative (APG "e" wave). The **diastolic peak** is the first local maximum of the first derivative after it, where the down-slope is flattest. If there is no positive APG maximum, the beat gets no notch/diastolic marks.

Only the first sign change is considered for the valley; if it doesn't qualify, the beat falls through to the shoulder rule.

### Parameters (class constants of `WaveformDetector`)

| Constant | Value | Why |
|---|---|---|
| `PEAK_WINDOW` | 0.111 s | Elgendi / NeuroKit2 |
| `BEAT_WINDOW` | 0.667 s | Elgendi / NeuroKit2 |
| `BEAT_OFFSET` | 0.02 | Elgendi's β |
| `MIN_DELAY` | 0.3 s | NeuroKit2 (200 bpm) |
| `NOTCH_MIN_DELAY` | 0.1 s | IEM paper's rule |
| `BEAT_FRACTION` | 0.6 | pyPPG's e-point window (onset + 0.6 × interval); here measured from the peak, so slightly later |
| `MIN_RISE` | 0.02 | our choice: keeps quantisation steps from counting as a notch |
| `SMOOTH` | 7 samples | our choice: ~140 ms, enough to smooth 7-bit steps without flattening the notch |
| `MIN_AMPLITUDE` | 4 units | our choice: flatline rejection |
| `FS` | 50 Hz | fixed; the device's measured rate is ~50.6 Hz |

## Results

### Synthetic (`tools/evaluate_synthetic.py`)

Skewed systolic + diastolic Gaussians with known fiducial points, rate variation, baseline wander, noise, 7-bit quantisation; 57 scored beats per run; a hit is within 50 ms. "Late" is how long after the event the mark is reported.

| Case | Systolic | Notch | Diastolic |
|---|---|---|---|
| valley, 70 bpm | 57/57, +1 ± 5 ms, 220 ms late | 57/57, −2 ± 6 ms, 460 ms late | 57/57, +10 ± 6 ms, 340 ms late |
| valley, 50 bpm | 57/57, +1 ± 6 ms | 57/57, −1 ± 6 ms, 600 ms late | 57/57, +9 ± 6 ms |
| shoulder, 70 bpm | 57/57, +1 ± 5 ms | 57/57, +3 ± 7 ms, 480 ms late | 57/57, +7 ± 7 ms |
| shoulder, 50 bpm | 57/57 | 57/57, +0 ± 7 ms | 57/57, +10 ± 8 ms |
| shoulder, amplitude 30, more noise | 57/57, −1 ± 8 ms | 50/57, −8 ± 15 ms | 49/57, −2 ± 19 ms |
| valley, 120 bpm, more noise | 57/57 | 35/57 | 32/33 (+22 extra) |
| shoulder, 120 bpm, more noise | 57/57 | 54/57, +16 ± 9 ms | ground truth has none (see caveat) |

Caveat for 120 bpm: the synthetic shape scales with the beat interval, so the notch ends up ~0.1 s after the peak (inside the excluded zone) and the diastolic wave runs into the next beat. Real systole lasts ~0.3 s regardless of rate, so these rows understate real performance, but **high heart rates are untested on real data**.

### Real device (`tools/replay.py`, one person at rest)

`2026-10-18_rest_clean_60s.csv` (60 s, ~68 bpm, clean):

- 69 systolic peaks for 70 device beats (the miss is in the first second, before the detector's 0.67 s warm-up is over); 68 notches, 68 diastolic peaks.
- Notch 180 ms after the systolic peak (100–220), diastolic 260 ms (240–340): consistent from beat to beat.
- Reported late (median): systolic 240 ms, notch 460 ms, diastolic 380 ms.
- **The device's flag is 4–5 samples (80–100 ms) after our systolic peak.** Our mark is reported ~240 ms after the peak, so in real time it arrives ~150 ms *after* the device flag, but it is placed at the true peak. For a trigger where latency matters more than placement, the device flag is the faster signal.

`2026-10-18_rest_artifacts_90s.csv` (90 s, includes finger repositioning around 20–30 s):

- Visual check of plots: on clean beats all three marks sit where a human would put them (notch at the dip, diastolic on the hump or at the start of the plateau).
- The clip/flatline artifact no longer produces marks. During the recovery afterwards (irregular shapes while the device re-scales) marks are unreliable, as are the device's own flags; 12 device flags have no systolic peak near them there.
- There is still at least one double detection (a 300 ms interval) in the artifact section.

## Known limitations and ideas

- **Delay of notch/diastolic marks** (0.3–0.6 s after the event) is inherent to waiting for 60 % of the beat. An option: analyse incrementally and report a valley-type notch as soon as the following rise is confirmed (would roughly halve its delay); shoulder-type notches need the whole window.
- **Systolic delay** (~0.2 s): the block only closes once the band-passed energy drops. A faster rule (e.g. report once the raw signal has fallen a few units below its running maximum inside an open block) could cut this, at some cost in robustness.
- Only tested on one person at rest. Needs recordings at higher heart rates (after exercise), other people, and cold fingers (low perfusion).
- `FS` is fixed at 50 Hz; at the measured 50.6 Hz time constants are ~1 % off, which is negligible.
- Motion artifacts are only handled by the clip/flatline guard. A signal-quality index (e.g. template correlation per beat) would allow suppressing marks during the recovery phase.
- The shoulder rule follows pyPPG; pyPPG additionally weights the APG with an expected notch time from previous beats. If notch placement in shoulder beats proves jumpy, that weighting is the next thing to add.

## How to tune

1. Record: `python tools/record.py data/recordings/<name>.csv 60` (not while `oxibeat.py` is running).
2. Replay: `python tools/replay.py <csv> --plot plot.html --start 0 --seconds 6`, open the HTML (or `python tools/screenshot.py file:///…/plot.html plot.png --width 1400 --height 680 --wait 1`).
3. Check the synthetic suite still passes: `python tools/evaluate_synthetic.py`.

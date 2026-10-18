"""Runs the MIDI clock's phase-locked loop offline, at several loop time constants.

    python tools/clock_sim.py [recording.csv ...]

Without arguments: the recordings in data/recordings plus two synthetic pulses (a
tempo ramp from 60 to 100 bpm, and a 6 s dropout after which the pulse comes back
out of phase). The waveform goes through the real detector in 5-sample packets
that arrive like over BLE (usually ~20 ms late, now and then held back up to 0.5 s
and then delivered in a burst), and the systolic peaks drive backend.clock.BeatPll
in virtual time.

Per time constant tau (in heartbeats; the UI's sensitivity maps to 16..1.5, see
backend.clock.time_constant) it prints how far off the clock was at each heartbeat
(median and 90th percentile, in ms; beats while locking are left out), how much the
clock's tempo between heartbeats changed from one to the next (rms, in bpm,
corrections included) next to the same for the raw beat intervals, how many
heartbeats were ignored, and, for the dropout, how many heartbeats it took to be
within 30 ms again. Breathing alone moves the heart rate ±5-10%, so a smooth tempo
(large tau) necessarily lands further from each beat.
"""

import csv
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from backend.clock import BeatPll, SampleTimes  # noqa: E402
from backend.detection import FS, WaveformDetector  # noqa: E402
from evaluate_synthetic import pulse  # noqa: E402

PACKET = 5
TAUS = (1, 1.5, 2, 3, 4, 6, 8, 12, 16, 32)


def synthetic(bpms, rng, gap_at=None, gap=6.0):
    """A pulse following `bpms` (one per beat); optionally flat for `gap` s after beat `gap_at`."""
    x = []
    for b, bpm in enumerate(bpms):
        if b == gap_at:
            x.extend([15.0] * int(gap * FS + rng.uniform(0, 1) * 60 / bpm * FS))  # lands out of phase
        n = int(60 / bpm * FS)
        x.extend(15 + 85 * pulse(np.arange(n) / n, "shoulder"))
    x = np.asarray(x) + rng.normal(0, 0.6, len(x))
    return np.clip(np.round(x), 0, 127).astype(int)


def arrivals(count, rng):
    """Arrival time of each packet: ~20 ms after its last sample, with occasional stalls."""
    out, held_until = [], 0.0
    for k in range(count):
        taken = ((k + 1) * PACKET - 1) / FS
        if rng.random() < 0.03:
            held_until = taken + rng.uniform(0.2, 0.5)
        out.append(max(out[-1] if out else 0.0, taken + 0.02 + rng.exponential(0.01), held_until))
    return out


def run(x, tau, seed=0):
    rng = np.random.default_rng(seed)
    det, times, pll = WaveformDetector(), SampleTimes(), BeatPll()
    packets = range(0, len(x), PACKET)
    errors, phases, beats, ignored = [], [], [], 0
    for n0, now in zip(packets, arrivals(len(packets), rng)):
        chunk = x[n0 : n0 + PACKET].tolist()
        times.arrived(n0 + len(chunk) - 1, now)
        for m in det.feed(n0, chunk, []):
            if m.kind != "systolic":
                continue
            t = times.time(m.n)
            was_locked, trusted = pll.locked, pll.last_trusted
            phase = pll.phase(t)  # where the clock was (it only changes from `now` on)
            error_ms = (phase - math.floor(phase + 0.5)) / pll.rate * 1000
            pll.beat(t, now, tau)
            beats.append(m.n)
            if was_locked and pll.last_trusted == trusted:
                ignored += 1
            if was_locked:
                errors.append((m.n, error_ms))
                phases.append((t, phase))
    # the clock's tempo between heartbeats, as a receiver hears it (corrections included)
    tempos = [60 * (p1 - p0) / (t1 - t0) for (t0, p0), (t1, p1) in zip(phases, phases[1:]) if t1 - t0 < 1.5]
    return errors, tempos, beats, ignored


def report(name, x, gap_at_sample=None):
    print(f"--- {name} ({len(x) / FS:.0f} s)")
    _, _, beats, _ = run(x, 4)
    ibi_bpm = 60 * FS / np.diff(beats)
    ibi_bpm = ibi_bpm[(ibi_bpm > 20) & (ibi_bpm < 300)]
    print(f"  raw beat intervals: {np.median(ibi_bpm):.0f} bpm median, beat-to-beat change {np.sqrt(np.mean(np.diff(ibi_bpm) ** 2)):.1f} bpm rms")
    print("   tau  |err| median   p90  tempo change  ignored  relock")
    for tau in TAUS:
        errors, tempos, _, ignored = run(x, tau)
        e = np.abs([err for _, err in errors])
        change = np.sqrt(np.mean(np.diff(tempos) ** 2)) if len(tempos) > 2 else float("nan")
        relock = ""
        if gap_at_sample is not None:
            after = [abs(err) for n, err in errors if n > gap_at_sample]
            relock = next((f"{k} beats" for k in range(len(after)) if all(a < 30 for a in after[k : k + 3])), "never")
        print(f"  {tau:4.1f}  {np.median(e):8.0f} ms {np.percentile(e, 90):5.0f} ms "
              f"{change:8.2f} bpm  {ignored:6d}   {relock}")


def main():
    paths = sys.argv[1:] or sorted(str(p) for p in (ROOT / "data" / "recordings").glob("*.csv"))
    for path in paths:
        x = np.array([int(row[0]) for row in csv.reader(open(path))])
        report(Path(path).name, x)
    rng = np.random.default_rng(1)
    ramp = [60 + 40 * min(1, b / 60) + 3 * math.sin(b / 4) + rng.normal(0, 1.5) for b in range(120)]
    report("synthetic ramp 60 -> 100 bpm, with variation", synthetic(ramp, rng))
    steady = [72 + 3 * math.sin(b / 4) + rng.normal(0, 1.5) for b in range(90)]
    x = synthetic(steady, rng, gap_at=45)
    gap_sample = int(sum(60 / bpm * FS for bpm in steady[:45]) + 6 * FS)
    report("synthetic 72 bpm with a 6 s dropout", x, gap_sample)


if __name__ == "__main__":
    main()

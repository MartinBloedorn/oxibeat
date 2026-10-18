"""Scores the waveform detector on synthetic pulses with known fiducial points.

    python tools/evaluate_synthetic.py

Each beat is a skewed systolic Gaussian plus a diastolic Gaussian, in two shapes:
"valley" (a real dip between them) and "shoulder" (no dip, like many fingertip
pulses). Ground truth is computed on the clean beat oversampled 20x with the same
rules the detector uses (valley/next maximum, or APG e-wave/VPG maximum). The
signal then gets beat-to-beat rate variation, baseline wander, noise and 7-bit
quantisation, and is fed in 5-sample packets at 50 Hz.

A hit is a detection within 2.5 samples (50 ms) of the truth. Caveat: the shape
scales with the beat interval, so at high rates the notch moves unrealistically
close to the systolic peak; real systole lasts ~0.3 s regardless of rate.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.detection import FS, WaveformDetector  # noqa: E402

UP = 20  # oversampling for ground truth
MS = 1000 / FS


def pulse(phase, shape):
    rise = np.exp(-((phase - 0.2) / 0.07) ** 2)
    fall = np.exp(-((phase - 0.2) / 0.11) ** 2)
    systolic = np.where(phase < 0.2, rise, fall)
    if shape == "valley":
        return systolic + 0.45 * np.exp(-((phase - 0.5) / 0.07) ** 2)
    return systolic + 0.42 * np.exp(-((phase - 0.43) / 0.1) ** 2)


def truth_points(y):
    d1 = np.gradient(y)
    d2 = np.gradient(d1)
    n = len(y)
    sp = int(np.argmax(y))
    window = range(sp + int(0.1 * FS * UP), int(0.85 * n))
    valleys = [k for k in window if y[k - 1] > y[k] <= y[k + 1]]
    if valleys:
        dn = valleys[0]
        dp = next((k for k in range(dn + 1, n - 1) if y[k - 1] < y[k] >= y[k + 1]), None)
    else:
        maxima = [k for k in window if d2[k - 1] < d2[k] >= d2[k + 1] and d2[k] > 0]
        dn = max(maxima, key=lambda k: d2[k])
        dp = next((k for k in range(dn + 1, n - 1) if d1[k - 1] < d1[k] >= d1[k + 1]), None)
    return {"systolic": sp, "notch": dn, "diastolic": dp}


def make(shape, rng, beats=60, bpm=70, noise=0.6, amp=85):
    hi_res, truth = [], {"systolic": [], "notch": [], "diastolic": []}
    for b in range(beats):
        ibi = 60 / (bpm + 8 * np.sin(b / 6) + rng.normal(0, 2))
        n_hi = int(ibi * FS * UP)
        y = pulse(np.arange(n_hi) / n_hi, shape)
        for kind, k in truth_points(y).items():
            if k is not None:
                truth[kind].append((len(hi_res) + k) / UP)
        hi_res.extend(y)
    x = np.asarray(hi_res)[::UP]
    n = np.arange(len(x))
    x = 15 + amp * x + 6 * np.sin(2 * np.pi * 0.15 * n / FS) + rng.normal(0, noise, len(x))
    return np.clip(np.round(x), 0, 127).astype(int), truth


def run(shape, **kw):
    x, truth = make(shape, np.random.default_rng(1), **kw)
    det = WaveformDetector()
    found = {k: [] for k in truth}
    latency = {k: [] for k in truth}
    for n0 in range(0, len(x), 5):
        for m in det.feed(n0, x[n0 : n0 + 5].tolist(), []):
            found[m.kind].append(m.n)
            latency[m.kind].append(n0 + 5 - m.n)
    print(f"--- {shape} {kw or ''}")
    for kind in truth:
        expected = np.asarray(truth[kind][2:-1])  # skip warm-up and the cut-off last beat
        f = np.asarray(found[kind])
        if not len(f) or not len(expected):
            print(f"  {kind:9s} found {len(f)}, expected {len(expected)}")
            continue
        err = np.array([f[np.argmin(abs(f - t))] - t for t in expected])
        hit = abs(err) <= 2.5
        extra = max(0, len(f) - len(truth[kind]))
        print(f"  {kind:9s} hits {hit.sum()}/{len(expected)}, extra {extra}, "
              f"error {err[hit].mean() * MS:+.0f} ± {err[hit].std() * MS:.0f} ms, "
              f"reported {np.median(latency[kind]) * MS:.0f} ms late")


if __name__ == "__main__":
    for shape in ("valley", "shoulder"):
        run(shape)
        run(shape, bpm=50)
        run(shape, bpm=120, noise=1.5)
    run("shoulder", noise=1.0, amp=30)

"""Beat detection: turns the pulse waveform into marks (fiducial points) on its samples.

Two detectors share one interface (`reset()`, `feed(n0, samples, pulses) -> [Mark]`,
where `n0` is the running number of the first sample):

- DeviceDetector passes on the beat flag the oximeter puts in its waveform.
- WaveformDetector analyses the waveform itself, streaming, and marks the systolic
  peak, the dicrotic notch and the diastolic peak of each beat:

  * Systolic peaks: Elgendi et al. 2013, "Systolic peak detection in acceleration
    photoplethysmograms...", PLoS ONE 8(10): e76585. Band-pass 0.5-8 Hz, square the
    positive part, and compare a short (111 ms, one systolic peak) with a long
    (667 ms, one beat) moving average; a stretch where the short one is above the
    long one plus an offset is a "block of interest" holding one systolic peak.
    Parameters as in NeuroKit2's ppg_findpeaks(method="elgendi").
  * Dicrotic notch and diastolic peak: per beat, after the systolic peak, along the
    lines of pyPPG (Goda, Charlton & Behar 2024, Physiol. Meas. 45 045001). If the
    pulse has a real valley after the systolic peak, that valley is the notch and the
    next maximum is the diastolic peak. Often (fingertip sensors, older or stiffer
    arteries) there is only a shoulder; then the notch is the APG "e" wave (largest
    maximum of the second derivative) and the diastolic peak is the following
    maximum of the first derivative, where the down-slope is flattest.

  The notch and diastolic peak can only be judged once the beat's down-slope has
  been seen, so they are reported about 0.6 beat intervals after the systolic peak.
"""

from collections import deque
from dataclasses import dataclass
from itertools import islice
from statistics import median

import numpy as np
from scipy.signal import butter, savgol_filter, sosfilt, sosfilt_zi

FS = 50.0  # the PC-60FW's waveform sample rate
VALUE_MAX = 127  # full scale of a waveform sample
MIN_AMPLITUDE = 4  # a systolic peak must rise at least this much above the second before it
MAX_INTERVAL = 2.5  # s; a longer gap between systolic peaks is a pause, not a beat interval (24 bpm)


@dataclass
class Mark:
    n: int  # running sample number
    kind: str


class DeviceDetector:
    id = "device"
    label = "Device built-in"
    kinds = ("beat",)

    def reset(self):
        pass

    def feed(self, n0: int, samples: list[int], pulses: list[int]) -> list[Mark]:
        return [Mark(n0 + i, "beat") for i in pulses]


class WaveformDetector:
    id = "waveform"
    label = "Waveform analysis"
    kinds = ("systolic", "notch", "diastolic")

    # systolic peaks (Elgendi)
    PEAK_WINDOW = 0.111  # s
    BEAT_WINDOW = 0.667  # s
    BEAT_OFFSET = 0.02  # fraction of the mean squared signal added to the threshold
    MIN_DELAY = 0.3  # s between systolic peaks (200 bpm)
    # beat morphology
    NOTCH_MIN_DELAY = 0.1  # s after the systolic peak
    BEAT_FRACTION = 0.6  # look this far into the beat interval after the systolic peak
    MIN_RISE = 0.02  # a valley counts as a notch if the pulse then rises by this fraction of its amplitude
    SMOOTH = 7  # Savitzky-Golay window (samples) for smoothing and derivatives
    HISTORY = 6.0  # s of waveform kept

    def __init__(self, fs: float = FS):
        self.fs = fs
        self._sos = butter(2, [0.5, 8.0], btype="bandpass", fs=fs, output="sos")
        self._w1 = round(self.PEAK_WINDOW * fs)
        self._w2 = round(self.BEAT_WINDOW * fs)
        self.reset()

    def reset(self):
        self._zi = sosfilt_zi(self._sos) * 0.0
        self._raw: deque[float] = deque(maxlen=round(self.HISTORY * self.fs))
        self._end = 0  # running number of the sample after the last one in _raw
        self._sq: deque[float] = deque(maxlen=self._w2)
        self._sq_mean = 0.0
        self._count = 0
        self._block_start: int | None = None
        self._last_peak: int | None = None
        self._ibis: deque[int] = deque(maxlen=5)
        self._pending: list[list[int]] = []  # [systolic peak, end of its analysis window]

    # --- streaming ---------------------------------------------------------

    def feed(self, n0: int, samples: list[int], _pulses: list[int]) -> list[Mark]:
        if self._end and n0 != self._end:
            self.reset()  # a gap in the numbering means the stream restarted
        filtered, self._zi = sosfilt(self._sos, np.asarray(samples, float), zi=self._zi)
        marks = []
        for i, (v, f) in enumerate(zip(samples, filtered)):
            self._raw.append(float(v))
            self._end = n0 + i + 1
            z = f * f if f > 0 else 0.0
            self._sq.append(z)
            self._count += 1
            # running mean of the squared signal over about 10 s
            self._sq_mean += (z - self._sq_mean) / min(self._count, 10 * self.fs)
            marks += self._step(n0 + i)
        marks += self._finish_due()
        return marks

    def _step(self, n: int) -> list[Mark]:
        if len(self._sq) < self._w2:
            return []
        # Offline, both averages are centred on the same sample. Centring the long one
        # would delay every detection by a third of a second, so it trails instead and
        # acts as an adaptive threshold taken from the last beat.
        c = n - self._w1 // 2
        ma_peak = sum(islice(self._sq, self._w2 - self._w1, self._w2)) / self._w1
        ma_beat = sum(self._sq) / self._w2
        above = ma_peak > ma_beat + self.BEAT_OFFSET * self._sq_mean
        if above and self._block_start is None:
            self._block_start = c
        elif not above and self._block_start is not None:
            start, self._block_start = self._block_start, None
            if c - start >= self._w1:
                return self._on_block(start, c)
        return []

    def _on_block(self, start: int, end: int) -> list[Mark]:
        # the systolic peak is the waveform's maximum around the block (the causal
        # band-pass shifts the block a little later than the peak it holds)
        lo = max(start - round(0.1 * self.fs), self._end - len(self._raw))
        if self._last_peak is not None:
            lo = max(lo, self._last_peak + 1)
        hi = min(end + round(0.05 * self.fs), self._end - 1)
        if hi <= lo:
            return []
        seg = self._segment(lo, hi + 1)
        peak = lo + int(np.argmax(seg))
        # artifacts while the finger moves: the waveform clips at full scale, or
        # flatlines with no pulse in it
        before = self._segment(max(peak - round(self.fs), self._end - len(self._raw)), peak + 1)
        if np.count_nonzero(seg >= VALUE_MAX) >= 3 or seg.max() - before.min() < MIN_AMPLITUDE:
            return []
        if self._last_peak is not None and peak - self._last_peak > MAX_INTERVAL * self.fs:
            # no beat for a long time (finger out, artifacts): start over rather than
            # take this as an interval; the old peak may also have left the history
            self._last_peak = None
        if self._last_peak is not None:
            ibi = peak - self._last_peak
            if ibi < self.MIN_DELAY * self.fs:
                return []
            self._ibis.append(ibi)
            # a beat still waiting for analysis ends where this one starts
            prev = self._last_peak
            onset = prev + int(np.argmin(self._segment(prev, peak + 1)))
            for p in self._pending:
                p[1] = min(p[1], onset)
        self._last_peak = peak
        ibi = median(self._ibis) if self._ibis else 0.8 * self.fs
        self._pending.append([peak, peak + round(self.BEAT_FRACTION * ibi)])
        return [Mark(peak, "systolic")]

    def _finish_due(self) -> list[Mark]:
        marks = []
        lookahead = self.SMOOTH // 2 + 1
        while self._pending and self._end >= self._pending[0][1] + lookahead:
            peak, end = self._pending.pop(0)
            marks += self._analyse(peak, end)
        return marks

    def _segment(self, a: int, b: int) -> np.ndarray:
        """Samples a..b-1 (running numbers) from the history; the part still in it."""
        first = self._end - len(self._raw)
        return np.fromiter(islice(self._raw, max(0, a - first), max(0, b - first)), float)

    # --- beat morphology ---------------------------------------------------

    def _analyse(self, peak: int, end: int) -> list[Mark]:
        half = self.SMOOTH // 2
        a = max(peak - half, self._end - len(self._raw))
        x = self._segment(a, end + half + 1)
        if len(x) < self.SMOOTH + 3:
            return []
        s = savgol_filter(x, self.SMOOTH, 3)
        d1 = savgol_filter(x, self.SMOOTH, 3, deriv=1)
        d2 = savgol_filter(x, self.SMOOTH, 3, deriv=2)
        i0 = peak - a + round(self.NOTCH_MIN_DELAY * self.fs)
        i1 = min(end - a, len(x) - 1)
        if i1 - i0 < 3:
            return []
        amp = s[peak - a] - s.min()

        notch = diastolic = None
        # a real valley: the slope turns from falling to rising, and the pulse then
        # rises noticeably to a maximum
        for i in range(max(i0, 1), i1):
            if d1[i - 1] < 0 <= d1[i]:
                v = i - 1 if s[i - 1] < s[i] else i
                top = next((j for j in range(v + 1, i1 + 1) if d1[j - 1] > 0 >= d1[j]), None)
                if top is not None and s[top] - s[v] >= max(1.0, self.MIN_RISE * amp):
                    notch, diastolic = v, top
                break
        if notch is None:
            # only a shoulder: APG e-wave, then the flattest point of the down-slope
            maxima = [i for i in range(max(i0, 1), i1) if d2[i - 1] < d2[i] >= d2[i + 1] and d2[i] > 0]
            if not maxima:
                return []
            notch = max(maxima, key=lambda i: d2[i])
            diastolic = next(
                (i for i in range(notch + 1, i1) if d1[i - 1] < d1[i] >= d1[i + 1]),
                None,
            )

        marks = [Mark(a + notch, "notch")]
        if diastolic is not None:
            marks.append(Mark(a + diastolic, "diastolic"))
        return marks


DETECTORS = {d.id: d for d in (DeviceDetector, WaveformDetector)}

"""Replays a recording (see record.py) through the waveform detector.

    python tools/replay.py data/recordings/2026-10-18_rest_clean_60s.csv [--plot out.html --start 0 --seconds 10]

Prints counts, latencies, beat intervals, notch/diastolic timing relative to the
systolic peak, and how the device's own beat flag lines up with the detected
systolic peak. With --plot, writes an HTML page with four strips of the waveform:
systolic (solid pink), notch (dashed orange), diastolic (dotted teal) lines, and a
grey dot at each device beat flag. Each strip is scaled to its own min/max.
The recording is fed in 5-sample packets, like the device sends it.
"""

import argparse
import csv
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.detection import FS, WaveformDetector  # noqa: E402

PACKET = 5
MS = 1000 / FS


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("recording")
    ap.add_argument("--plot", help="write an HTML plot here")
    ap.add_argument("--start", type=float, default=0, help="plot from this second")
    ap.add_argument("--seconds", type=float, default=10, help="seconds per plot strip")
    args = ap.parse_args()

    rows = [(int(a), int(b)) for a, b in csv.reader(open(args.recording))]
    x = np.array([r[0] for r in rows])
    flagged = np.array([i for i, r in enumerate(rows) if r[1]])

    det = WaveformDetector()
    marks, latency = [], {}
    for n0 in range(0, len(x), PACKET):
        for m in det.feed(n0, x[n0 : n0 + PACKET].tolist(), []):
            marks.append(m)
            latency.setdefault(m.kind, []).append(min(n0 + PACKET, len(x)) - m.n)

    sp = np.array([m.n for m in marks if m.kind == "systolic"])
    count = {k: sum(m.kind == k for m in marks) for k in ("systolic", "notch", "diastolic")}
    print(f"{len(x)} samples ({len(x) / FS:.0f} s); device beat flags {len(flagged)}; detected {count}")
    for kind, lat in latency.items():
        print(f"  {kind:9s} reported {np.median(lat) * MS:.0f} ms after it happens (median)")
    if len(sp) > 2:
        ibi = np.diff(sp)
        print(f"  beat interval: median {np.median(ibi) * MS:.0f} ms, min {ibi.min() * MS:.0f}, max {ibi.max() * MS:.0f}")
        for kind in ("notch", "diastolic"):
            d = [m.n - sp[sp <= m.n].max() for m in marks if m.kind == kind and (sp <= m.n).any()]
            if d:
                print(f"  {kind:9s} after systolic: median {np.median(d) * MS:.0f} ms, range {min(d) * MS:.0f}-{max(d) * MS:.0f}")
    if len(sp) and len(flagged):
        off = np.array([f - sp[np.argmin(abs(sp - f))] for f in flagged])
        near = off[abs(off) <= 10]
        values, counts = np.unique(near, return_counts=True)
        print("  device flag minus systolic peak (samples: count):", dict(zip(values.tolist(), counts.tolist())),
              f"({len(off) - len(near)} flags with no detected peak within 10 samples)")

    if args.plot:
        write_plot(args.plot, x, marks, flagged, int(args.start * FS), int(args.seconds * FS))
        print(f"plot written to {args.plot}")


def write_plot(path, x, marks, flagged, start, span, width=1400, height=170):
    style = {"systolic": ("#d6336c", ""), "notch": ("#e8590c", "6 4"), "diastolic": ("#0c8599", "2 3")}
    parts = []
    for r in range(4):
        a, b = start + r * span, start + (r + 1) * span
        if a >= len(x):
            break
        seg = x[a:b]
        lo, hi = seg.min(), seg.max()

        def px(n):
            return (n - a) * width / span

        def py(v):
            return r * height + height - 10 - (v - lo) / max(1, hi - lo) * (height - 20)

        for m in marks:
            if a <= m.n < b:
                color, dash = style[m.kind]
                parts.append(f'<line x1="{px(m.n):.1f}" x2="{px(m.n):.1f}" y1="{r * height}" y2="{(r + 1) * height}" '
                             f'stroke="{color}" stroke-width="2" stroke-dasharray="{dash}"/>')
        for f in flagged[(flagged >= a) & (flagged < b)]:
            parts.append(f'<circle cx="{px(f):.1f}" cy="{r * height + 6}" r="3" fill="#555"/>')
        points = " ".join(f"{px(a + i):.1f},{py(v):.1f}" for i, v in enumerate(seg))
        parts.append(f'<polyline points="{points}" fill="none" stroke="#1f6fd1" stroke-width="1.5"/>')
        parts.append(f'<line x1="0" x2="{width}" y1="{(r + 1) * height}" y2="{(r + 1) * height}" stroke="#ccc"/>')
    Path(path).write_text(
        f'<!doctype html><html><body style="margin:0;background:#fff">'
        f'<svg width="{width}" height="{4 * height}">{"".join(parts)}</svg></body></html>'
    )


if __name__ == "__main__":
    main()

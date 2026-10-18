"""Records the oximeter's waveform to a CSV, for tuning detection offline.

    python tools/record.py data/recordings/<name>.csv 60

Waits (up to 2 minutes) for the device, then records the given number of seconds.
Each row is `value,flag`: the 7-bit waveform sample (0..127) and the device's
built-in beat flag (1 on flagged samples). Rows are consecutive samples at ~50 Hz.
Don't run it while oxibeat.py is connected to the device: BLE allows one connection.
"""

import argparse
import asyncio
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.device import Device  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", help="CSV file to write")
    ap.add_argument("seconds", type=float, help="how long to record once data flows")
    args = ap.parse_args()

    rows = []
    started = None

    def on_wave(w):
        nonlocal started
        started = started or time.time()
        for i, v in enumerate(w.samples):
            rows.append((v, int(i in w.pulses)))

    async def run():
        dev = Device(lambda s: print("device", s["state"], s["name"] or "", flush=True), lambda v: None, on_wave)
        task = asyncio.create_task(dev.run())
        t0 = time.time()
        while time.time() - t0 < args.seconds + 120 and not (started and time.time() - started > args.seconds):
            await asyncio.sleep(0.5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    with open(args.out, "w", newline="") as f:
        csv.writer(f).writerows(rows)
    print(f"{len(rows)} samples written to {args.out}")


if __name__ == "__main__":
    main()

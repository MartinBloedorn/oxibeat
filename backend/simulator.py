"""Stand-in for Device that synthesises a pulse waveform, for working without the oximeter."""

import asyncio
import math
import random

from .protocol import Vitals, Wave

RATE = 50  # samples/s, sent in packets of 5 like the real device


class Simulator:
    def __init__(self, on_state, on_vitals, on_wave):
        self._on_state = on_state
        self._on_vitals = on_vitals
        self._on_wave = on_wave
        self.state = {"state": "connected", "name": "Simulator", "address": "sim"}

    async def run(self):
        self._on_state(self.state)
        bpm, phase, n = 70.0, 0.0, 0
        while True:
            samples, pulses = [], []
            for _ in range(5):
                prev = phase
                phase = (phase + bpm / 60 / RATE) % 1.0
                if prev < 0.2 <= phase:
                    pulses.append(len(samples))
                # systolic wave (fast rise, slower fall) plus a smaller diastolic wave after the notch
                width = 0.07 if phase < 0.2 else 0.11
                v = 90 * math.exp(-((phase - 0.2) / width) ** 2) + 38 * math.exp(-((phase - 0.48) / 0.08) ** 2)
                samples.append(int(min(127, 10 + v)))
            self._on_wave(Wave(samples, pulses))
            n += 1
            if n % RATE == 0:  # about once every 5 s
                bpm = min(110, max(50, bpm + random.uniform(-4, 4)))
            if n % 10 == 0:
                self._on_vitals(Vitals(spo2=random.choice((97, 98, 98, 99)), pr=round(bpm), pi=round(random.uniform(2, 6), 1)))
            await asyncio.sleep(5 / RATE)

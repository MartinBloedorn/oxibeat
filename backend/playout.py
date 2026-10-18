"""Plays the waveform out at a steady pace for the outputs (MIDI CC, OSC).

The waveform reaches the server in bursts (5 samples per BLE packet, packets
sometimes 0.5 s apart). Sent on as it arrives it would be just as choppy, so this
does what the UI's graph does (web/app.js): a short buffer absorbs the bursts,
samples are consumed at a steady 50 Hz with a gentle speed control, and on every
tick the value is interpolated between the two current samples.
"""

import asyncio
import time
from collections import deque
from typing import Callable

RATE = 50.0  # waveform samples per second
TARGET_DELAY = 0.4  # s of samples kept buffered; BLE packets can arrive ~0.5 s apart
MAX_BACKLOG = 1.0  # s; beyond this the buffer is cut back to the target
TICK = 0.005  # s between output updates (~6 ms in practice on Windows)
RESTART_GAP = 1.0  # s of silence after which the stream counts as restarted


class Playout:
    def __init__(self):
        self._queue: deque[int] = deque()
        self._pos = 0.0  # fractional position between _queue[0] and _queue[1]
        self._speed = 1.0
        self._playing = False
        self._last_push = 0.0

    def push(self, samples: list[int]):
        now = time.perf_counter()
        if now - self._last_push > RESTART_GAP:
            # the stream paused (device off, reconnect): start over with a full buffer
            # instead of playing the new samples from a near-empty one
            self.clear()
        self._last_push = now
        self._queue.extend(samples)
        if len(self._queue) > RATE * MAX_BACKLOG:
            for _ in range(len(self._queue) - round(RATE * TARGET_DELAY)):
                self._queue.popleft()

    def clear(self):
        """Drops buffered samples (on pause), so resuming doesn't replay stale ones."""
        self._queue.clear()
        self._playing = False

    async def run(self, on_value: Callable[[float], None]):
        """Calls `on_value` with the current (interpolated, 0..127) value every tick, until cancelled."""
        # perf_counter, not loop.time(): on Windows the latter only moves in 15.6 ms steps
        last = time.perf_counter()
        while True:
            await asyncio.sleep(TICK)
            now = time.perf_counter()
            if now - last < TICK * 0.8:
                # asyncio runs timers up to its clock resolution (15.6 ms on Windows) early
                # whenever the loop wakes for I/O; without this, busy I/O floods the outputs
                continue
            value = self._advance(min(now - last, 0.1))
            last = now
            if value is not None:
                on_value(value)

    def _advance(self, dt: float) -> float | None:
        """Moves the playout position on by `dt` seconds; returns the value there."""
        target = RATE * TARGET_DELAY
        if not self._playing:
            # (re)start only with a full buffer, so playback doesn't stutter at once
            if len(self._queue) < target:
                return None
            self._playing, self._pos, self._speed = True, 0.0, 1.0
        if len(self._queue) < 2:
            self._playing = False
            return float(self._queue[0]) if self._queue else None
        # nudge the speed so the buffer stays near its target; smoothed so it doesn't jerk
        wanted = min(1.25, max(0.8, 1 + 0.25 * (len(self._queue) - target) / target))
        self._speed += (wanted - self._speed) * min(1.0, dt * 1.5)
        self._pos += RATE * self._speed * dt
        while self._pos >= 1 and len(self._queue) > 2:
            self._queue.popleft()
            self._pos -= 1
        self._pos = min(self._pos, 1.0)
        a, b = self._queue[0], self._queue[1]
        return a + (b - a) * self._pos

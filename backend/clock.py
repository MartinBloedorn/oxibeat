"""MIDI clock that follows the heartbeat.

Every heartbeat (the systolic peak, or the device's beat flag) is one quarter note
("1/4") or one half note ("1/2") of the clock, so a 70 bpm pulse gives a 70 or a
140 bpm clock. The clock (24 ticks per quarter note) runs on its own thread so its
ticks stay evenly spaced; asyncio's timers are far too coarse on Windows.

A phase-locked loop keeps it in step, updated once per heartbeat:

- Heartbeats are timed by their sample number, not by when they reach us (BLE
  bursts, 0.2-0.6 s of detection delay). `SampleTimes` maps sample numbers to the
  local clock. The loop keeps the last few seconds of its own phase, so it can tell
  how far off it was at the moment the heartbeat happened.
- The phase error then corrects the tempo a little (integral part) and is worked off
  by the next heartbeat by running slightly faster or slower (proportional part).
  The clock never jumps and never skips or doubles ticks while it runs.
- Both gains come from one time constant `tau`, in heartbeats, set by the
  sensitivity. With both poles of the loop at a = exp(-1/tau): Kp = 1 - a^2 and
  Ki = (1 - a)^2 (critically damped; see tools/clock_sim.py).
- A heartbeat too soon after the last one is a double detection and is ignored; a
  missed one just makes a longer interval, which the loop handles anyway. When the
  recent intervals say the tempo jumped (or the "double detections" keep coming),
  the loop locks onto the pulse afresh rather than drift towards it.
- Without heartbeats the clock keeps its last tempo and phase. With Start/Stop on,
  it stops after STOP_AFTER and starts again (on a heartbeat) once it has locked
  again; with Start/Stop off it runs on as long as it's enabled.
"""

import asyncio
import math
import threading
import time
from collections import deque

FS = 50.0  # waveform samples per second
TICKS_PER_QUARTER = 24
FRACTIONS = {"1/4": 1, "1/2": 2}  # quarter notes per heartbeat
RATE_RANGE = (20 / 60, 300 / 60)  # plausible heartbeats per second
DEFAULT_RATE = 2.0  # heartbeats per second (120 bpm) until there is a pulse rate
TAU_RANGE = (16.0, 1.5)  # loop time constant in heartbeats at sensitivity 0 and 100
SPURIOUS = 0.6  # heartbeats; one sooner than this after the last is a double detection
RELOCK_AFTER = 3  # such heartbeats in a row after which the loop locks afresh
TEMPO_JUMP = 0.2  # the loop locks afresh when the typical interval is this far off its tempo
UNLOCK_AFTER = 4.0  # s without a trusted heartbeat after which the next ones lock afresh
STOP_AFTER = 5.0  # s without a trusted heartbeat after which Start/Stop stops the clock
MAX_SLEW = 0.5  # a correction never changes the tempo by more than this fraction
HISTORY = 10.0  # s of the loop's past phase kept
STREAM_GAP = 1.0  # s without samples after which the stream counts as restarted
POLL = 0.005  # s; the clock thread's longest sleep
RESYNC = 0.25  # s; a clock this far behind (e.g. after a system stall) skips ahead


def time_constant(sensitivity: float) -> float:
    """Loop time constant in heartbeats: 16 at sensitivity 0, 1.5 at 100, logarithmic between."""
    slow, fast = TAU_RANGE
    return slow * (fast / slow) ** (sensitivity / 100)


def _wrap(phase: float) -> float:
    """Distance to the nearest whole heartbeat, -0.5..0.5."""
    return phase - math.floor(phase + 0.5)


class SampleTimes:
    """When each waveform sample was taken, on the local clock (time.perf_counter).

    A packet can't arrive before its last sample was taken, so the smallest
    arrival-minus-sample-time seen recently is the best estimate of the offset;
    BLE bursts only ever make packets late.
    """

    def __init__(self):
        self._offsets: deque[tuple[float, float]] = deque()  # (arrival, offset)

    def arrived(self, last: int, now: float):
        """A packet whose last sample is number `last` arrived at `now`."""
        if self._offsets and now - self._offsets[-1][0] > STREAM_GAP:
            self._offsets.clear()  # the stream restarted; old offsets no longer apply
        self._offsets.append((now, now - last / FS))
        while now - self._offsets[0][0] > HISTORY:
            self._offsets.popleft()

    def time(self, n: int) -> float | None:
        if not self._offsets:
            return None
        return n / FS + min(o for _, o in self._offsets)


class BeatPll:
    """The loop itself; times are passed in, so it can be run offline (tools/clock_sim.py).

    Phase counts heartbeats. It is piecewise linear: each segment starts at time t
    with phase p and advances at `rate` plus `slew` (the correction being worked
    off) until `until`, then at `rate` alone.
    """

    def __init__(self, rate: float = DEFAULT_RATE):
        self.rate = rate
        self.locked = False
        self.last_trusted: float | None = None  # time of the last heartbeat the loop used
        self._segments: deque[tuple[float, float, float, float, float]] = deque([(0.0, 0.0, rate, 0.0, 0.0)])
        self._last_beat: float | None = None
        self._intervals: deque[float] = deque(maxlen=5)  # recent intervals between trusted heartbeats
        self._rejected = 0

    def phase(self, t: float) -> float:
        t0, p0, rate, slew, until = self._segment(t)
        return p0 + rate * (t - t0) + slew * (max(t0, min(t, until)) - t0)

    def time_of(self, phase: float) -> float:
        """When the current segment reaches `phase`."""
        t0, p0, rate, slew, until = self._segments[-1]
        until = max(t0, until)
        p_until = p0 + (rate + slew) * (until - t0)
        if phase < p_until:
            return t0 + (phase - p0) / (rate + slew)
        return until + (phase - p_until) / rate

    def hint(self, rate: float, now: float):
        """The device's pulse rate; sets the tempo while the loop isn't locked yet."""
        if not self.locked and RATE_RANGE[0] <= rate <= RATE_RANGE[1]:
            self.rate = rate
            self._start_segment(now, rate, 0.0, 0.0)

    def beat(self, t: float, now: float, tau: float):
        """A heartbeat happened at `t` (reported at `now`)."""
        if t > now or t < now - HISTORY + 1:
            return
        interval = t - self._last_beat if self._last_beat is not None else None
        self._last_beat = t
        if interval is not None and not 1 / RATE_RANGE[1] <= interval <= 1 / RATE_RANGE[0]:
            interval = None
        if self.locked and t - self.last_trusted > UNLOCK_AFTER:
            self.locked = False
        if not self.locked:
            if interval is not None:
                self._lock(t, now, interval)
            return
        since = t - self.last_trusted
        if since < SPURIOUS / self.rate:
            # too soon after the last heartbeat: a double detection, unless it keeps happening
            self._rejected += 1
            if self._rejected >= RELOCK_AFTER and interval is not None:
                self._lock(t, now, interval)
            return
        self._rejected = 0
        if since < 2 * SPURIOUS / self.rate:  # not after a missed heartbeat
            self._intervals.append(since)
        typical = sorted(self._intervals)[len(self._intervals) // 2]
        if len(self._intervals) >= 3 and abs(1 / (typical * self.rate) - 1) > TEMPO_JUMP:
            # the pulse moved further than the loop should follow gradually
            self._lock(t, now, typical)
            return
        a = math.exp(-1 / tau)
        error = self._error(t)
        self._correct(t, now, error, 1 - a * a, self.rate * (1 - (1 - a) ** 2 * error))
        self.last_trusted = t

    def _lock(self, t: float, now: float, interval: float):
        """Locks afresh: the tempo from `interval`, the whole phase error corrected."""
        self._correct(t, now, _wrap(self._error(t)), 1.0, 1 / interval)
        self._intervals.clear()
        self._intervals.append(interval)
        self._rejected = 0
        self.locked = True
        self.last_trusted = t

    def _error(self, t: float) -> float:
        """Phase error at `t` (heartbeats, + = clock ahead), once the corrections under way are done."""
        return _wrap(self.phase(t)) + self._slew_between(t, math.inf)

    def _correct(self, t: float, now: float, error: float, kp: float, rate: float):
        old = self.rate
        self.rate = min(RATE_RANGE[1], max(RATE_RANGE[0], rate))
        # what's left of the corrections under way, minus this one, plus what the new
        # tempo would have added had it applied from t instead of from now
        correction = self._slew_between(now, math.inf) - kp * error + (self.rate - old) * (now - t)
        correction = min(0.5, max(-0.5, correction))
        # done by the next heartbeat (the beat was reported late, so that's sooner than a beat
        # from now), but never running the clock more than 50% off its tempo
        duration = max(0.25 / self.rate, t + 1 / self.rate - now, abs(correction) / (MAX_SLEW * self.rate))
        self._start_segment(now, self.rate, correction / duration, duration)

    def _start_segment(self, now: float, rate: float, slew: float, duration: float):
        self._segments.append((now, self.phase(now), rate, slew, now + duration))
        while len(self._segments) > 1 and self._segments[1][0] < now - HISTORY:
            self._segments.popleft()

    def _segment(self, t: float):
        for segment in reversed(self._segments):
            if segment[0] <= t:
                return segment
        return self._segments[0]

    def _slew_between(self, a: float, b: float) -> float:
        """Phase the corrections add between times a and b."""
        total = 0.0
        segments = list(self._segments)
        for i, (t0, _p, _r, slew, until) in enumerate(segments):
            end = min(until, segments[i + 1][0]) if i + 1 < len(segments) else until
            total += slew * max(0.0, min(b, end) - max(a, t0))
        return total


class MidiClock:
    """The clock's settings and its thread. `midi` is the MidiOutput it sends through."""

    def __init__(self, midi):
        self._midi = midi
        self.enabled = False
        self.transport = True  # send Start/Stop; also stops the clock without a pulse
        self.sensitivity = 50  # 0..100, see time_constant()
        self.fraction = "1/4"  # note value of one heartbeat
        self._running = True  # the hub's play/pause
        self._lock = threading.Lock()
        self._pll = BeatPll()
        self._times = SampleTimes()
        self._ticking = False
        self._start_pending = False
        self._ticks_per_beat = TICKS_PER_QUARTER
        self._next = 0  # number of the next tick; tick k falls at phase k / ticks per heartbeat
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def state(self) -> dict:
        return {
            "enabled": self.enabled,
            "transport": self.transport,
            "sensitivity": self.sensitivity,
            "fraction": self.fraction,
        }

    def update(self, settings: dict):
        with self._lock:
            if "enabled" in settings:
                self.enabled = bool(settings["enabled"])
            if "transport" in settings and bool(settings["transport"]) != self.transport:
                # restart, so a receiver gets its Start (or its last Stop) on the new terms
                self._halt()
                self.transport = bool(settings["transport"])
            if "sensitivity" in settings:
                try:
                    self.sensitivity = max(0, min(100, int(settings["sensitivity"])))
                except (TypeError, ValueError):
                    pass
            if settings.get("fraction") in FRACTIONS:
                self.fraction = settings["fraction"]

    # --- input, from the hub (asyncio thread) -----------------------------------

    def samples(self, last: int):
        """A waveform packet whose last sample is number `last` just arrived."""
        with self._lock:
            self._times.arrived(last, time.perf_counter())

    def beat(self, n: int):
        """A heartbeat at sample number `n`."""
        with self._lock:
            t = self._times.time(n)
            if t is not None:
                self._pll.beat(t, time.perf_counter(), time_constant(self.sensitivity))

    def pulse_rate(self, bpm: float):
        with self._lock:
            self._pll.hint(bpm / 60, time.perf_counter())

    def set_running(self, running: bool):
        with self._lock:
            self._running = running

    # --- the thread ---------------------------------------------------------

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="midi-clock", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join()
            self._thread = None
        self.halt()

    def halt(self):
        """Stops ticking now (sending Stop if Start/Stop is on), e.g. before the port closes."""
        with self._lock:
            self._halt()

    def _halt(self):
        if self._ticking and self.transport and not self._start_pending:
            self._midi.send_realtime("stop")
        self._ticking = False
        self._start_pending = False

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                wait = self._step(time.perf_counter())
            if wait > 0:
                time.sleep(wait)  # high resolution on Windows since Python 3.11

    def _step(self, now: float) -> float:
        """Sends what is due; returns how long to sleep."""
        pll = self._pll
        wanted = self.enabled and self._running and self._midi.is_open
        if wanted and self.transport:
            wanted = pll.locked and now - pll.last_trusted < STOP_AFTER
        if not wanted:
            self._halt()
            return POLL
        ticks_per_beat = TICKS_PER_QUARTER * FRACTIONS[self.fraction]
        if not self._ticking:
            self._ticking = True
            self._ticks_per_beat = ticks_per_beat
            if self.transport:
                # Start goes out just before the first tick, which falls on a heartbeat
                self._start_pending = True
                self._next = math.ceil(pll.phase(now)) * ticks_per_beat
            else:
                self._next = math.ceil(pll.phase(now) * ticks_per_beat)
        elif ticks_per_beat != self._ticks_per_beat:
            self._next = math.ceil(pll.phase(now) * ticks_per_beat)
            self._ticks_per_beat = ticks_per_beat
        due = pll.time_of(self._next / ticks_per_beat)
        if due > now:
            return min(POLL, due - now)
        if now - due > RESYNC and not self._start_pending:
            self._next = math.ceil(pll.phase(now) * ticks_per_beat)
            return 0
        if self._start_pending:
            self._midi.send_realtime("start")
            self._start_pending = False
        self._midi.send_realtime("clock")
        self._next += 1
        return 0

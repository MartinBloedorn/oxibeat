"""The pulse rate the outputs use (MIDI velocity and note length, OSC event values)."""

DEFAULT_BPM = 120.0  # used when the device reports no (or an implausible) pulse rate
BPM_RANGE = (20, 300)


def effective_bpm(rate: float | None) -> float:
    """The device's pulse rate, or DEFAULT_BPM when it has none or it is implausible."""
    if rate is None or not BPM_RANGE[0] <= rate <= BPM_RANGE[1]:
        return DEFAULT_BPM
    return float(rate)

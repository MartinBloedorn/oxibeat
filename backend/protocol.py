"""Packet parser for the Viatom PC-60FW oximeter (see external/viatom_pc60fw/README.md).

A packet is: 0xAA 0x55, a direction byte (0x0F / 0xF0), a length byte, then
`length` bytes of payload whose last byte is a CRC8/MAXIM over the whole packet.
"""

from dataclasses import dataclass

SYNC = b"\xaa\x55"

FUNC_VITALS = 0x01
FUNC_WAVE = 0x02


@dataclass
class Vitals:
    spo2: int  # %
    pr: int  # beats per minute
    pi: float  # perfusion index, %


@dataclass
class Wave:
    samples: list[int]  # 0..127
    pulses: list[int]  # indices into `samples` the device flagged as a beat


def _crc8_maxim(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


class Parser:
    """Turns a stream of notification chunks into Vitals / Wave objects."""

    def __init__(self):
        self._buf = bytearray()
        self.crc_errors = 0

    def reset(self):
        self._buf.clear()

    def feed(self, chunk: bytes) -> list[Vitals | Wave]:
        self._buf.extend(chunk)
        out = []
        while True:
            idx = self._buf.find(SYNC)
            if idx < 0:
                # keep a trailing 0xAA in case the sync word is split across chunks
                del self._buf[: max(0, len(self._buf) - 1)]
                break
            del self._buf[:idx]
            if len(self._buf) < 4:
                break
            total = 4 + self._buf[3]
            if len(self._buf) < total:
                break
            packet = bytes(self._buf[:total])
            if _crc8_maxim(packet) != 0:
                self.crc_errors += 1
                del self._buf[:2]  # skip this sync word and resync
                continue
            del self._buf[:total]
            msg = self._decode(packet[4:-1])
            if msg is not None:
                out.append(msg)
        return out

    @staticmethod
    def _decode(payload: bytes) -> Vitals | Wave | None:
        if not payload:
            return None
        func = payload[0]
        if func == FUNC_VITALS and len(payload) >= 5:
            return Vitals(spo2=payload[1], pr=payload[2], pi=payload[4] / 10)
        if func == FUNC_WAVE:
            samples, pulses = [], []
            for i, b in enumerate(payload[1:]):
                # bit 7 marks the detected pulse; the remaining bits are the value
                if b & 0x80:
                    pulses.append(i)
                samples.append(b & 0x7F)
            return Wave(samples, pulses)
        return None

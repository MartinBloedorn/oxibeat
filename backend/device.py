"""BLE connection to the oximeter: scans until it shows up, streams, and starts over on disconnect.

The PC-60FW switches itself off when no finger is inserted, so dropping out and
coming back is the normal case, not an error.
"""

import asyncio
import logging
import time
from typing import Callable

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from .protocol import Parser, Vitals, Wave

log = logging.getLogger(__name__)

NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"  # Nordic UART service
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # notify: device -> host
# names the PC-60FW is sold under; "OxySmart" is a rebrand of the same device
KNOWN_NAMES = ("PC-60F", "OxySmart")

SCAN_TIMEOUT = 5.0
CONNECT_TIMEOUT = 15.0
DATA_TIMEOUT = 5.0  # no notifications for this long = treat the link as dead
RETRY_DELAY = 1.0


class Device:
    """Owns the BLE link. Reports state changes and decoded packets through callbacks."""

    def __init__(
        self,
        on_state: Callable[[dict], None],
        on_vitals: Callable[[Vitals], None],
        on_wave: Callable[[Wave], None],
        name_prefix: str | None = None,
        address: str | None = None,
    ):
        self._on_state = on_state
        self._on_vitals = on_vitals
        self._on_wave = on_wave
        self._name_prefix = name_prefix
        self._address = address.upper() if address else None
        self._parser = Parser()
        self._last_data = 0.0
        self.state = {"state": "scanning", "name": None, "address": None}

    def _set_state(self, state: str, name: str | None = None, address: str | None = None):
        self.state = {"state": state, "name": name, "address": address}
        log.info("device %s%s", state, f" {name} ({address})" if name else "")
        self._on_state(self.state)

    def _matches(self, device, adv) -> bool:
        if self._address:
            return device.address.upper() == self._address
        name = adv.local_name or device.name or ""
        if self._name_prefix:
            return name.startswith(self._name_prefix)
        return name.startswith(KNOWN_NAMES) or NUS_SERVICE in adv.service_uuids

    def _on_notify(self, _sender, data: bytearray):
        self._last_data = time.monotonic()
        for msg in self._parser.feed(data):
            if isinstance(msg, Wave):
                self._on_wave(msg)
            else:
                self._on_vitals(msg)

    async def run(self):
        log.info("searching for %s", self._address or (f"{self._name_prefix}*" if self._name_prefix else "oximeter"))
        while True:
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except (BleakError, OSError, asyncio.TimeoutError) as e:
                log.warning("device error: %s", e or type(e).__name__)
            except Exception:
                log.exception("unexpected device error")
            await asyncio.sleep(RETRY_DELAY)

    async def _session(self):
        if self.state["state"] != "scanning":
            self._set_state("scanning")
        found = await BleakScanner.find_device_by_filter(self._matches, timeout=SCAN_TIMEOUT)
        if found is None:
            return

        name = found.name or found.address
        self._set_state("connecting", name, found.address)
        lost = asyncio.Event()
        self._parser.reset()

        try:
            async with BleakClient(
                found, disconnected_callback=lambda _c: lost.set(), timeout=CONNECT_TIMEOUT
            ) as client:
                await client.start_notify(NUS_TX, self._on_notify)
                self._last_data = time.monotonic()
                self._set_state("connected", name, found.address)
                # the disconnect callback isn't always prompt, so also watch for silence
                while not lost.is_set():
                    try:
                        await asyncio.wait_for(lost.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        if time.monotonic() - self._last_data > DATA_TIMEOUT:
                            log.info("no data for %.0fs, dropping link", DATA_TIMEOUT)
                            break
        finally:
            self._set_state("scanning")

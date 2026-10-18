"""oxibeat: Viatom PC-60FW oximeter to MIDI/OSC. Run with `python oxibeat.py`, stop with Ctrl+C."""

import argparse
import asyncio
import logging
import webbrowser

from backend.detection import DETECTORS
from backend.server import serve


def main():
    ap = argparse.ArgumentParser(description="PC-60FW oximeter to MIDI/OSC")
    ap.add_argument("--host", default="127.0.0.1", help="address to serve the UI on (default: %(default)s)")
    ap.add_argument("--port", type=int, default=8080, help="port to serve the UI on (default: %(default)s)")
    ap.add_argument("--name", help="only connect to devices whose BLE name starts with this")
    ap.add_argument("--address", help="connect only to this BLE address")
    ap.add_argument("--simulate", action="store_true", help="use a synthetic signal instead of the oximeter")
    ap.add_argument(
        "--detection", choices=DETECTORS, default="waveform", help="beat detection at startup (default: %(default)s)"
    )
    ap.add_argument("--no-browser", action="store_true", help="don't open the UI in a browser tab")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not args.verbose:
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    on_ready = None if args.no_browser else webbrowser.open_new_tab
    try:
        # asyncio.run turns Ctrl+C into a cancellation of serve(), which closes
        # the BLE link and the web server before KeyboardInterrupt lands here
        asyncio.run(serve(args.host, args.port, args.name, args.address, args.simulate, on_ready, args.detection))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        logging.error("%s", e)
        raise SystemExit(1)
    logging.info("stopped")


if __name__ == "__main__":
    main()

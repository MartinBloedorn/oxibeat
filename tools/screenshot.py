"""Screenshots a page with headless Microsoft Edge, after letting it run for a while.

    python tools/screenshot.py http://localhost:8080/ out.png [--width 1200 --height 560 --wait 6 --dark --js "..."]

Edge's own --screenshot flag fires too early for the live graph, so this drives
Edge over the DevTools protocol instead. Windows only (Edge path below); works for
file:// URLs too, e.g. a plot from replay.py.
"""

import argparse
import asyncio
import base64
import subprocess
import tempfile

import aiohttp

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9333


async def capture(args):
    async with aiohttp.ClientSession() as s:
        for _ in range(50):
            try:
                tabs = await (await s.get(f"http://127.0.0.1:{PORT}/json")).json()
                break
            except aiohttp.ClientError:
                await asyncio.sleep(0.2)
        tab = next(t for t in tabs if t["type"] == "page")
        async with s.ws_connect(tab["webSocketDebuggerUrl"], max_msg_size=0) as ws:
            seq = 0

            async def call(method, **params):
                nonlocal seq
                seq += 1
                await ws.send_json({"id": seq, "method": method, "params": params})
                while True:
                    reply = await ws.receive_json()
                    if reply.get("id") == seq:
                        return reply.get("result")

            await call("Emulation.setDeviceMetricsOverride", width=args.width, height=args.height,
                       deviceScaleFactor=1, mobile=False)
            if args.dark:
                await call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
            await call("Page.navigate", url=args.url)
            if args.js:
                # e.g. click a button before the capture
                await asyncio.sleep(1.5)
                result = await call("Runtime.evaluate", expression=args.js, returnByValue=True)
                if "value" in result.get("result", {}):
                    print(result["result"]["value"])
            await asyncio.sleep(args.wait)
            shot = await call("Page.captureScreenshot")
            with open(args.out, "wb") as f:
                f.write(base64.b64decode(shot["data"]))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("url")
    ap.add_argument("out")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=560)
    ap.add_argument("--wait", type=float, default=6, help="seconds to let the page run")
    ap.add_argument("--dark", action="store_true", help="emulate an OS dark-mode preference")
    ap.add_argument("--js", help="JavaScript to run on the page (1.5 s after loading) before the capture")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as profile:
        edge = subprocess.Popen([EDGE, "--headless=new", f"--remote-debugging-port={PORT}",
                                 f"--user-data-dir={profile}", "about:blank"])
        try:
            asyncio.run(capture(args))
        finally:
            edge.kill()
            edge.wait()


if __name__ == "__main__":
    main()

"""Manual web UI regression (not run in CI; needs google-chrome and the venv websockets package).
Headful Chrome on $DISPLAY + real X11 input via xdotool (no CDP input), ~500px wide window.
Checks mobile drawers close on outside click + Escape, More sheet too. usage: x11_regress.py <url> <token_file> [outdir]  (needs $DISPLAY and xdotool)"""
import asyncio, json, subprocess, sys, tempfile, time, urllib.request
import websockets
URL, TOKEN = sys.argv[1], open(sys.argv[2]).read().strip()
PORT = 9335
def xdo(*a): subprocess.run(["xdotool", *a], check=True)
async def main():
    prof = tempfile.mkdtemp(prefix="cdpx-")
    chrome = subprocess.Popen(["google-chrome", f"--remote-debugging-port={PORT}", "--no-first-run", "--no-default-browser-check",
        f"--user-data-dir={prof}", "--window-position=0,0", "--window-size=500,760", "--app=about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fails = []
    try:
        for _ in range(60):
            try:
                page = next(t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json")) if t["type"] == "page"); break
            except Exception: time.sleep(0.25)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            n = [0]
            async def send(m, p=None):
                n[0] += 1; i = n[0]; await ws.send(json.dumps({"id": i, "method": m, "params": p or {}}))
                while True:
                    r = json.loads(await ws.recv())
                    if r.get("id") == i: return r.get("result", r.get("error"))
            async def js(e):
                return (await send("Runtime.evaluate", {"expression": e, "awaitPromise": True, "returnByValue": True})).get("result", {}).get("value")
            await send("Page.enable")
            await send("Page.addScriptToEvaluateOnNewDocument", {"source": f"try{{sessionStorage.setItem('conveyor-token', {json.dumps(TOKEN)})}}catch(e){{}}"})
            await send("Page.navigate", {"url": URL}); time.sleep(4)
            await send("Page.bringToFront")
            wid = subprocess.run(["xdotool", "search", "--sync", "--onlyvisible", "--pid", str(chrome.pid)], capture_output=True, text=True).stdout.split()
            if wid:  # windowactivate needs a window manager; plain Xvfb only supports windowfocus
                if subprocess.run(["xdotool", "windowactivate", "--sync", wid[-1]], capture_output=True).returncode != 0:
                    xdo("windowfocus", "--sync", wid[-1])
            info = await js("({iw:innerWidth, ih:innerHeight, sx:screenX, sy:screenY, ow:outerWidth, oh:outerHeight})")
            print("viewport", info)
            W = wid[-1]
            offx = (info["ow"] - info["iw"]) // 2; offy = info["oh"] - info["ih"] - (info["ow"] - info["iw"]) // 2
            nav = lambda t: f"[...document.querySelectorAll('.mobile-bottom-nav button')].find(b=>b.textContent.includes('{t}'))"
            item = lambda t: f"[...document.querySelectorAll('.mobile-sheet-item')].find(b=>b.textContent.includes('{t}'))"
            async def click_el(expr):
                c = await js(f"(()=>{{const e={expr}; if(!e) return null; const b=e.getBoundingClientRect(); return [b.left+b.width/2,b.top+b.height/2]}})()")
                if not c: raise RuntimeError("no element " + expr)
                xdo("mousemove", "--window", W, str(int(offx + c[0])), str(int(offy + c[1]))); xdo("click", "1"); time.sleep(0.7)
            def click_at(x, y): xdo("mousemove", "--window", W, str(int(offx + x)), str(int(offy + y))); xdo("click", "1"); time.sleep(0.8)
            await click_el(nav("More"))
            print("calibration: sheet open after X click =", await js("!!document.querySelector('.mobile-sheet')"))
            xdo("key", "Escape"); time.sleep(0.6)
            print("calibration: sheet closed after X Escape =", not await js("!!document.querySelector('.mobile-sheet')"))
            if await js("!!document.querySelector('.mobile-sheet')"): await js("document.querySelector('.mobile-sheet-overlay').click()")
            for which, sel in (("Sessions", "aside.sessions-panel"), ("Context", "aside.context-panel")):
                for how in ("outside-click", "escape"):
                    await click_el(nav("More")); await click_el(item(which))
                    r = await js(f"(()=>{{const b=document.querySelector('{sel}').getBoundingClientRect(); return [b.left,b.right]}})()")
                    opened = r[0] >= -1 and r[1] <= info["iw"] + 1
                    if how == "outside-click":
                        ox = (r[1] + info["iw"]) / 2 if r[1] < info["iw"] - 20 else r[0] / 2
                        click_at(ox, info["ih"] * 0.45)
                    else:
                        xdo("key", "Escape"); time.sleep(0.8)
                    r2 = await js(f"(()=>{{const b=document.querySelector('{sel}').getBoundingClientRect(); return [b.left,b.right]}})()")
                    closed = r2[1] <= 1 or r2[0] >= r[1] - 1
                    ok = opened and closed
                    print(("PASS" if ok else "FAIL"), which, how, "open", r, "after", r2, "focus:", await js("document.activeElement && (document.activeElement.className||document.activeElement.tagName)"))
                    if not ok:
                        fails.append(f"{which}/{how}")
                        await js(f"(document.querySelector('{sel} .drawer-close-btn')||{{click(){{}}}}).click()"); time.sleep(0.5)
            import base64
            open("" + (sys.argv[3] if len(sys.argv) > 3 else "/tmp") + "/x11-regress.png", "wb").write(base64.b64decode((await send("Page.captureScreenshot", {"format": "png"}))["data"]))
    finally:
        chrome.terminate()
    print("RESULT:", "ALL PASS" if not fails else f"FAIL {fails}")
asyncio.run(main())

"""Manual web UI regression (not run in CI; needs google-chrome and the venv websockets package).
Real-input UI regression for PR #60: CHANGES collapse persistence across Page.reload,
mobile drawers closing via real backdrop tap/click and real Escape key. Never prints token.
usage: cdp_regress.py <base_url> <token_file> <outdir>"""
import asyncio, base64, json, subprocess, sys, tempfile, time, urllib.request
import websockets
URL, TOKF, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
TOKEN = open(TOKF).read().strip()
PORT = 9334
FAIL = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond: FAIL.append(name)

async def session(mobile: bool, body):
    prof = tempfile.mkdtemp(prefix="cdp-")
    chrome = subprocess.Popen(["google-chrome", "--headless=new", f"--remote-debugging-port={PORT}", "--no-first-run",
                               f"--user-data-dir={prof}", "--window-size=1280,800", "--disable-gpu", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                page = next(t for t in tabs if t["type"] == "page"); break
            except Exception: time.sleep(0.2)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            st = {"n": 0, "logs": []}
            async def send(method, params=None):
                st["n"] += 1; mid = st["n"]
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == mid: return m.get("result", m.get("error"))
                    meth = m.get("method"); p = m.get("params", {})
                    if meth == "Runtime.exceptionThrown": st["logs"].append("exception")
                    elif meth == "Runtime.consoleAPICalled" and p["type"] in ("error", "warning"): st["logs"].append("console." + p["type"])
                    elif meth == "Network.responseReceived" and p["response"]["status"] >= 400: st["logs"].append(f"http {p['response']['status']} {p['response']['url']}")
            async def js(expr):
                r = await send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
                return r.get("result", {}).get("value")
            async def sleep(s): await js(f"new Promise(r=>setTimeout(r,{int(s*1000)}))")
            async def center(sel_js):
                return await js(f"(()=>{{const e={sel_js}; if(!e) return null; const b=e.getBoundingClientRect(); return [b.left+b.width/2, b.top+b.height/2]}})()")
            async def click(x, y):
                for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                    await send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
            async def tap(x, y):
                await send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
                await send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
            async def key_escape():
                for t in ("rawKeyDown", "keyUp"):
                    await send("Input.dispatchKeyEvent", {"type": t, "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
            for d in ("Runtime.enable", "Network.enable", "Page.enable"): await send(d)
            if mobile:
                await send("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True})
                await send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
            await send("Page.addScriptToEvaluateOnNewDocument", {"source": f"try{{sessionStorage.setItem('conveyor-token', {json.dumps(TOKEN)})}}catch(e){{}}"})
            await send("Page.navigate", {"url": URL}); await sleep(3)
            api = dict(send=send, js=js, sleep=sleep, center=center, click=click, tap=tap, key_escape=key_escape)
            await body(api)
            shot = await send("Page.captureScreenshot", {"format": "png"})
            open(f"{OUT}/{'mobile' if mobile else 'desktop'}-regress.png", "wb").write(base64.b64decode(shot["data"]))
            check(("mobile" if mobile else "desktop") + " console/network clean", not st["logs"], "; ".join(st["logs"])[:300])
    finally:
        chrome.terminate()

TOGGLE = "document.querySelector('.context-section-toggle')"
async def desktop(a):
    await a["js"]("localStorage.removeItem('conveyor-changes-collapsed')"); await a["send"]("Page.reload"); await a["sleep"](3)
    check("toggle starts expanded", await a["js"](f"{TOGGLE}.getAttribute('aria-expanded')") == "true")
    x, y = await a["center"](TOGGLE); await a["click"](x, y); await a["sleep"](0.4)
    check("click collapses", await a["js"](f"{TOGGLE}.getAttribute('aria-expanded')") == "false")
    check("stored true", await a["js"]("localStorage.getItem('conveyor-changes-collapsed')") == "true")
    await a["send"]("Page.reload", {"ignoreCache": True}); await a["sleep"](3.5)
    check("still collapsed after reload", await a["js"](f"{TOGGLE}.getAttribute('aria-expanded')") == "false",
          str(await a["js"](f"[{TOGGLE}&&{TOGGLE}.getAttribute('aria-expanded'), localStorage.getItem('conveyor-changes-collapsed')]")))
    pos = await a["js"]("(()=>{const sec=document.querySelector('.context-section--changes'); const p=document.querySelector('aside.context-panel'); const secs=[...p.querySelectorAll(':scope > .context-section')].map(e=>[e.getBoundingClientRect().top, e.classList.contains('context-section--changes')]).sort((x,y)=>x[0]-y[0]); return {top: sec&&sec.getBoundingClientRect().top, first: secs.length&&secs[0][1], vh: innerHeight}})()")
    check("collapsed CHANGES stays at top of panel and on screen", bool(pos and pos["first"] and pos["top"] is not None and 0 <= pos["top"] < pos["vh"]), str(pos))
    await a["sleep"](6)  # survive a few poll/refresh cycles
    check("still collapsed after polls", await a["js"](f"{TOGGLE}.getAttribute('aria-expanded')") == "false")
    x, y = await a["center"](TOGGLE); await a["click"](x, y); await a["sleep"](0.4)
    check("click re-expands", await a["js"](f"{TOGGLE}.getAttribute('aria-expanded')") == "true",
          str([x, y, await a["js"](f"(()=>{{const e=document.elementFromPoint({x},{y}); return e&&(e.className||e.tagName)}})()"), await a["js"]("localStorage.getItem('conveyor-changes-collapsed')")]))
    await a["send"]("Page.reload"); await a["sleep"](3.5)
    check("expanded persists after reload", await a["js"](f"{TOGGLE}.getAttribute('aria-expanded')") == "true")

def nav_btn(t): return f"[...document.querySelectorAll('.mobile-bottom-nav button')].find(b=>b.textContent.includes('{t}'))"
def sheet_item(t): return f"[...document.querySelectorAll('.mobile-sheet-item')].find(b=>b.textContent.includes('{t}'))"
OPEN = {"Sessions": "document.querySelector('aside.sessions-panel.drawer-open')", "Context": "document.querySelector('aside.context-panel.drawer-open')"}
async def mobile(a):
    for which in ("Sessions", "Context"):
        for how in ("tap", "click", "esc"):
            x, y = await a["center"](nav_btn("More")); await a["tap"](x, y); await a["sleep"](0.5)
            x, y = await a["center"](sheet_item(which)); await a["tap"](x, y); await a["sleep"](0.6)
            opened = await a["js"](f"!!{OPEN[which]}")
            check(f"{which} drawer opens", opened)
            rect = await a["js"](f"(()=>{{const b={OPEN[which]}.getBoundingClientRect(); return [b.left,b.right]}})()")
            ox = (rect[1] + 380) / 2 if rect[1] < 370 else rect[0] / 2   # a point outside the drawer
            if how == "tap": await a["tap"](ox, 420)
            elif how == "click": await a["click"](ox, 420)
            else: await a["key_escape"]()
            await a["sleep"](0.6)
            check(f"{which} drawer closes via {how}", not await a["js"](f"!!{OPEN[which]}"), f"outside x={ox:.0f} drawer={rect}")
            sel = "aside.sessions-panel" if which == "Sessions" else "aside.context-panel"
            await a["sleep"](0.4)  # transition
            vis = await a["js"](f"(()=>{{const b=document.querySelector('{sel}').getBoundingClientRect(); return [b.left,b.right]}})()")
            check(f"{which} drawer visually off-screen after {how}", vis[1] <= 1 or vis[0] >= 389, str(vis))

async def main():
    await session(False, desktop)
    await session(True, mobile)
    print("RESULT:", "ALL PASS" if not FAIL else f"{len(FAIL)} FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)
asyncio.run(main())

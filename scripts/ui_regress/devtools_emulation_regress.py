"""Manual web UI regression (not run in CI; needs google-chrome and the venv `websockets` package).

Reproduces the Chrome DevTools *Device Toolbar* input path that PR #60's human testers use:
  Emulation.setDeviceMetricsOverride(390x844, deviceScaleFactor 3, mobile)
  Emulation.setTouchEmulationEnabled(true, maxTouchPoints 5)
  Emulation.setEmitTouchEventsForMouse(true, configuration "mobile")
and drives it with *mouse* input (Input.dispatchMouseEvent), which Chrome converts into
pointer + touch events and a synthesized click, plus Esc via Input.dispatchKeyEvent
(key/code "Escape", windowsVirtualKeyCode 27).

Checks, for both the Sessions and the Context & Changes drawer:
  * tap outside the drawer closes it, and the tap does not click through to the page;
  * Esc closes it after a tap inside the drawer, and while the chat textarea has focus;
  * a pinch-zoomed / panned page (Shift-drag / trackpad pinch in DevTools) is reset to
    scale 1 / offset 0 while a drawer is open, so the backdrop strip is always on-screen;
  * no horizontal overflow or scroll offset (scrollX, visualViewport.offsetLeft, scrollWidth).
Never prints the token.
usage: devtools_emulation_regress.py <base_url> <token_file>"""
import asyncio, json, subprocess, sys, tempfile, time, urllib.request
import websockets

URL, TOKEN = sys.argv[1], open(sys.argv[2]).read().strip()
PORT = 9338
W, H = 390, 844
FAIL: list[str] = []
STATE = """(()=>{const vv=visualViewport, d=document.querySelector('aside.drawer-open');
return {drawer:d?d.className.split(' ')[0]:null, sx:scrollX, vvL:Math.round(vv.offsetLeft), vvT:Math.round(vv.offsetTop),
scale:+vv.scale.toFixed(3), sw:document.documentElement.scrollWidth, sh:document.documentElement.scrollHeight, ih:innerHeight, active:document.activeElement?document.activeElement.tagName:''}})()"""


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        FAIL.append(name)


async def main():
    prof = tempfile.mkdtemp(prefix="cdp-emu-")
    chrome = subprocess.Popen(["google-chrome", "--headless=new", f"--remote-debugging-port={PORT}", "--no-first-run",
                               f"--user-data-dir={prof}", "--window-size=1024,900", "--disable-gpu", "about:blank"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                page = next(t for t in json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json")) if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            n = [0]
            async def recv_until(ids, timeout):
                # With emitTouchEventsForMouse the mousePressed/Released acks may only arrive late
                # (Chrome waits for the synthesized gesture), so never block on them forever.
                end = time.time() + timeout
                while ids and time.time() < end:
                    try:
                        m = json.loads(await asyncio.wait_for(ws.recv(), 0.3))
                    except asyncio.TimeoutError:
                        continue
                    if m.get("id") in ids:
                        ids.discard(m["id"])
                        if len(ids) == 0:
                            return m.get("result", m.get("error"))
            async def send(method, params=None, wait=True, timeout=10):
                n[0] += 1
                mid = n[0]
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                return await recv_until({mid}, timeout) if wait else None
            async def js(expr):
                r = await send("Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True})
                return (r or {}).get("result", {}).get("value")
            async def sleep(s):
                await js(f"new Promise(r=>setTimeout(r,{int(s * 1000)}))")
            async def mouse_tap(x, y):
                # x/y are layout-viewport CSS px (getBoundingClientRect); input coordinates are in
                # visual-viewport space, so map them the way a human aiming at the pixel would.
                v = await js("[visualViewport.scale, visualViewport.offsetLeft, visualViewport.offsetTop]")
                x, y = (x - v[1]) * v[0], (y - v[2]) * v[0]
                if not (0 <= x <= W and 0 <= y <= H):
                    print(f"     note: target is off-screen in the zoomed viewport (visual {x:.0f},{y:.0f}); clamped")
                    x, y = min(max(x, 2), W - 2), min(max(y, 2), H - 2)
                for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                    await send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": "none" if t == "mouseMoved" else "left",
                                                           "buttons": 1 if t == "mousePressed" else 0, "clickCount": 1}, wait=False)
                await sleep(0.8)
            async def shift_drag_pinch(x0, y0, x1, y1, steps=8):
                ev = [("mouseMoved", x0, y0, 0), ("mousePressed", x0, y0, 1)]
                ev += [("mouseMoved", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps, 1) for i in range(1, steps + 1)]
                ev += [("mouseReleased", x1, y1, 0)]
                for t, x, y, b in ev:
                    await send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "modifiers": 8, "button": "left" if (b or t == "mouseReleased") else "none",
                                                           "buttons": b, "clickCount": 1 if t in ("mousePressed", "mouseReleased") else 0}, wait=False)
                    await asyncio.sleep(0.03)
                await sleep(0.8)
            async def esc():
                for t in ("rawKeyDown", "keyUp"):
                    await send("Input.dispatchKeyEvent", {"type": t, "key": "Escape", "code": "Escape",
                                                          "windowsVirtualKeyCode": 27, "nativeVirtualKeyCode": 27})
                await sleep(0.6)
            async def center(expr):
                return await js(f"(()=>{{const e={expr}; if(!e) return null; const b=e.getBoundingClientRect(); return [b.left+b.width/2,b.top+b.height/2]}})()")
            nav = lambda t: f"[...document.querySelectorAll('.mobile-bottom-nav button')].find(b=>b.textContent.includes('{t}'))"
            item = lambda t: f"[...document.querySelectorAll('.mobile-sheet-item')].find(b=>b.textContent.includes('{t}'))"

            for d in ("Runtime.enable", "Page.enable"):
                await send(d)
            await send("Emulation.setDeviceMetricsOverride", {"width": W, "height": H, "deviceScaleFactor": 3, "mobile": True})
            await send("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
            await send("Emulation.setEmitTouchEventsForMouse", {"enabled": True, "configuration": "mobile"})
            await send("Page.addScriptToEvaluateOnNewDocument", {"source": f"try{{sessionStorage.setItem('conveyor-token',{json.dumps(TOKEN)})}}catch(e){{}};"
                       "window.__textareaClicks=0;document.addEventListener('click',e=>{if(e.target&&e.target.tagName==='TEXTAREA')window.__textareaClicks++},false);"})
            await send("Page.navigate", {"url": URL})
            await sleep(3)
            check("mobile UI active", bool(await js("!!document.querySelector('.app-shell.mobile-ui')")))
            s0 = await js(STATE)
            check("no horizontal overflow at load", s0["sw"] <= W and s0["sx"] == 0 and s0["vvL"] == 0, json.dumps(s0))
            check("no vertical document overflow at load (shell fits 100dvh)", s0["sh"] <= s0["ih"], json.dumps(s0))

            async def open_drawer(which, pinch=False):
                if pinch:
                    # The tester's screenshot showed the page zoomed ~1.3x and panned; reproduce it.
                    # DevTools touch emulation turns a Shift+mouse-drag into a pinch gesture.
                    await shift_drag_pinch(200, 420, 200, 300)
                    z = await js(STATE)
                    check(f"{which}: precondition - page pinch-zoomed before opening", z["scale"] > 1.05, json.dumps(z))
                m = await center(nav("More"))
                if pinch or m is None:
                    await js(nav("More") + ".click()")  # nav may be off-screen while zoomed
                    await sleep(0.4)
                else:
                    await mouse_tap(*m)
                if pinch:
                    await js(item(which) + ".click()")  # the sheet itself may be partly off-screen while zoomed
                    await sleep(0.6)
                else:
                    c = await center(item(which))
                    if c:
                        await mouse_tap(*c)
                return await js(STATE)
            async def reset():
                # leave a clean state for the next case even when a check failed
                await js("(()=>{const b=document.querySelector('aside.drawer-open .drawer-close-btn'); if(b) b.click(); const o=document.querySelector('.mobile-sheet-overlay'); if(o) o.click()})()")
                await send("Emulation.setPageScaleFactor", {"pageScaleFactor": 1})
                await sleep(0.5)

            for which, cls in (("Sessions", "sessions-panel"), ("Context", "context-panel")):
                # 1) outside tap (lands on the composer area for Context, i.e. a real page control)
                await js("window.__textareaClicks=0")
                s1 = await open_drawer(which)
                check(f"{which}: drawer opens via emulated taps", s1["drawer"] == cls, json.dumps(s1))
                r = await js("(()=>{const b=document.querySelector('aside.drawer-open').getBoundingClientRect();return [b.left,b.right]})()")
                ta = await center("document.querySelector('textarea')")
                x = (r[1] + W) / 2 if r[0] < 20 else r[0] / 2
                y = ta[1] if ta else 700
                await mouse_tap(x, y)
                s2 = await js(STATE)
                check(f"{which}: outside tap closes", s2["drawer"] is None, json.dumps(s2))
                check(f"{which}: outside tap does not click through", await js("window.__textareaClicks") == 0 and s2["active"] != "TEXTAREA",
                      f"textareaClicks={await js('window.__textareaClicks')} active={s2['active']}")
                check(f"{which}: no scroll offset after close", s2["sx"] == 0 and s2["vvL"] == 0 and s2["sw"] <= W, json.dumps(s2))
                # 2) Esc after a tap inside the drawer (neutral point: drawer header)
                await reset()
                await open_drawer(which)
                hdr = await center("document.querySelector('aside.drawer-open .mobile-drawer-header strong, aside.drawer-open .mobile-drawer-header span, aside.drawer-open .mobile-drawer-header')")
                await mouse_tap(hdr[0] - 40, hdr[1])
                still = (await js(STATE))["drawer"]
                await esc()
                s3 = await js(STATE)
                check(f"{which}: Esc after inside tap closes", still == cls and s3["drawer"] is None, f"open_after_inside={still} {json.dumps(s3)}")
                await reset()
                # 3) Esc while the chat textarea has keyboard focus (a key-handling input must not swallow it)
                await open_drawer(which)
                await js("(()=>{const t=document.querySelector('textarea'); if(t){t.addEventListener('keydown',e=>e.stopPropagation(),{once:true}); t.focus()}})()")
                await esc()
                s4 = await js(STATE)
                check(f"{which}: Esc with focused textarea closes", s4["drawer"] is None, json.dumps(s4))
                await reset()
                # 4) zoomed + panned page: opening a drawer resets zoom so outside strip is visible; outside tap closes
                s5 = await open_drawer(which, pinch=True)
                check(f"{which}: zoom reset while drawer open", s5["drawer"] == cls and abs(s5["scale"] - 1) < 0.01 and s5["vvL"] == 0, json.dumps(s5))
                r = await js("(()=>{const d=document.querySelector('aside.drawer-open'); if(!d) return null; const b=d.getBoundingClientRect();return [b.left,b.right]})()")
                if r:
                    await mouse_tap((r[1] + W) / 2 if r[0] < 20 else r[0] / 2, 420)
                s6 = await js(STATE)
                check(f"{which}: outside tap closes after zoom", s6["drawer"] is None, json.dumps(s6))
                await reset()
            meta = await js("document.querySelector('meta[name=viewport]').content")
            check("viewport meta restored (user zoom allowed) when closed", "maximum-scale" not in meta, meta)
    finally:
        chrome.terminate()
    print("RESULT:", "ALL PASS" if not FAIL else f"{len(FAIL)} FAIL: {FAIL}")
    sys.exit(1 if FAIL else 0)


asyncio.run(main())

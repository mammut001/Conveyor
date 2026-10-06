# Mobile Web Console & PWA

Conveyor provides an opt-in, mobile-friendly interface for the Web Console designed for smartphone viewports (360–430px widths) with progressive web app (PWA) installation support.

## Enabling Mobile Web UI

Mobile UI is controlled by the configuration flag:

```bash
CONVEYOR_WEB_MOBILE_UI=true
```

- In `.env` or system environment: `CONVEYOR_WEB_MOBILE_UI=true` (or `1`)
- In `Settings`: `settings.web_mobile_ui: bool = False` (default is `False`)
- In `system_status()`: reported as `features.mobile_ui: bool`

When enabled, `App.tsx` attaches the `mobile-ui` class to `.app-shell`. All responsive enhancements are scoped strictly under `.mobile-ui` inside `@media (max-width: 760px)`. Desktop viewports (>760px) and deployments with the flag turned off remain completely unchanged.

## What Changes on Mobile (≤760px)

1. **Fluid Single-Column Layout & Zero Horizontal Scroll**:
   - The desktop 3-column grid is neutralized; content fits comfortably within 360–430px widths without page-level horizontal scroll (`document.documentElement.scrollWidth <= innerWidth`).
   - Long lines in logs, markdown code blocks, tables, and unified diffs scroll horizontally inside their own containers.

2. **Fixed Bottom Navigation Bar**:
   - Replaces the desktop top view tabs with an accessible bottom navigation bar:
     - **Tasks**: Primary task and job stream view.
     - **Chat**: Direct chat tier.
     - **Approvals**: Unified tool and job approval inbox, including pending count badge.
     - **Skills**: Reusable operator procedures (shown when `skills` feature is enabled).
     - **More**: Opens a slide-up sheet for additional tools.
   - All tap targets meet or exceed 44×44px, and safe-area insets (`env(safe-area-inset-bottom)`) are respected on notched displays.

3. **"More" Sheet**:
   - Provides quick access to secondary views and overlays:
     - **Inbox & Routines** (with unread count badge)
     - **Long-term Memory**
     - **Sessions Drawer**
     - **Context & Changes Drawer**
     - **Settings** (modal provider configuration)

4. **Slide-over Drawers for Sessions and Context**:
   - The Sessions sidebar and right Context & Changes panel do not stack under the main stream.
   - Instead, they open as slide-over drawers with touch-friendly backdrops, top close buttons, and Escape-key dismissal.

5. **Optimized Chat & Composer**:
   - Messages expand to full viewport width for readability.
   - The composer is sticky above the bottom navigation bar.
   - The textarea uses a font size of 16px to prevent automatic zooming on iOS Safari when focused.
   - Cards (approval requests, memory items, skills) use single-column layouts with wrapping action buttons.

6. **Condensed Top Bar**:
   - The top bar is condensed into the brand mark + Conveyor label, real-time connection status dot, and Settings icon button.

## PWA Installation

Conveyor provides a standard web app manifest at `/manifest.webmanifest` served with content type `application/manifest+json`:

- **Name**: `Conveyor`
- **Short Name**: `Conveyor`
- **Start URL**: `/`
- **Display**: `standalone` (removes browser URL bar and navigation buttons)
- **Theme & Background Color**: `#f5f6f7` (matches Web Console light theme)
- **Icons**: SVG vector icon plus 192×192 and 512×512 PNG icons.

When `features.mobile_ui` is active, the web application dynamically injects `<link rel="manifest" href="/manifest.webmanifest">` into the document head.

### iOS (Safari)
1. Open the Conveyor Web Console in Safari over HTTPS or your secure Tailscale address.
2. Tap the **Share** button in the Safari toolbar.
3. Scroll down and tap **Add to Home Screen**.
4. Confirm the name "Conveyor" and tap **Add**.
5. Launch Conveyor from your home screen. It will open in standalone mode with full viewport usage.

### Android (Chrome)
1. Open the Conveyor Web Console in Chrome.
2. Tap the three-dot overflow menu in the top right.
3. Tap **Install app** (or **Add to Home screen**).
4. Confirm the installation prompt.
5. Launch Conveyor from your app drawer or home screen.

## Why There Is No Service Worker

Conveyor explicitly **does not register a Service Worker** for the Web Console:

1. **Authentication & Token Safety**: Conveyor uses bearer tokens stored only in browser session storage (`sessionStorage`). Service Workers caching API responses or static pages run a high risk of persisting authenticated session state across untrusted origins or leaking tokens in offline storage caches.
2. **Real-time State Freshness**: Conveyor is a live operational control plane for background jobs, streaming events, and urgent approvals. Service Worker caching could serve stale job statuses, out-of-date approval cards, or expired worktree diffs, risking unintended actions on live systems.
3. **No Offline Mode**: Conveyor cannot execute commands or query Codex offline; caching assets without live API access offers no user benefit while increasing security risks.

## Known Limitations

- **Computer Screen Preview**: Read-only desktop screenshots from Mac nodes are displayed as downscaled thumbnails; full inspection is best on a tablet or desktop screen.
- **Large Unified Diffs**: Very large diffs with thousands of changed lines require horizontal and vertical scrolling within the diff viewer card; desktop remains recommended for massive multi-file code reviews.

## UI regression scripts (manual)

Two browser scripts under `scripts/ui_regress/` exercise the things unit tests cannot (they are not run in CI):

- `cdp_regress.py <url> <token_file> <outdir>` — headless Chrome over CDP with real input events (`Input.dispatchMouseEvent` / `dispatchTouchEvent` / `dispatchKeyEvent`). Desktop 1280x800: collapse CHANGES, `Page.reload`, check it is still collapsed **and still the top section of the panel**, survives polling, re-expand persists. Mobile 390x844 with touch emulation: open Sessions / Context drawers from More and close each by an outside tap, an outside mouse click and a real Escape key, checking both the state and that the drawer is visually off-screen.
- `x11_regress.py <url> <token_file> [outdir]` — headful Chrome on `$DISPLAY` (~500px window) driven by real X11 mouse/keyboard events via `xdotool`.

Both read the Web token from a file and never print it.

## Reaching the console from a phone over a VPN

The console binds to loopback by default, which means an SSH tunnel from a
laptop. To open it from a phone, let it also listen on the host's private VPN
address and connect the phone to that VPN:

```dotenv
CONVEYOR_WEB_EXTRA_HOSTS=10.10.0.1   # the host's WireGuard / Tailscale address
```

Then open `http://10.10.0.1:<CONVEYOR_WEB_PORT>` on the phone.

- The extra listeners are the same server: same bearer token, same sessions,
  same live screen. Loopback keeps working for local tools and SSH tunnels.
- They speak plain HTTP, so only private addresses are accepted. `0.0.0.0`,
  `::` and public addresses fail `web_console.py --check` and refuse to start.
  The VPN is what encrypts the connection.
- Allow the port on the VPN interface only, e.g.
  `ufw allow in on wg0 to 10.10.0.1 port 18787 proto tcp`. Never open it on
  the public interface.
- If the VPN interface comes up after the service, the console keeps retrying
  the bind every five seconds.
- On a cloud host a "private" address on the public NIC (for example the
  instance's VPC address behind a 1:1 NAT) is reachable from the internet.
  Use the VPN interface's address, not that one.

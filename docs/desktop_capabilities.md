# Desktop Capability Matrix

This matrix describes the current P5.6/P5.6.2 Mac-local Computer Use path.
It is a capability guide, not a permission bypass: direct mode, blocked
keywords, blocked apps, and action allow-lists still apply.

| Surface | Typical result | Notes |
|---|---|---|
| Native macOS App with AX controls | Reliable | Prefer `target_app`; AX click uses `pid`, `window_id`, and `element_index`. |
| Calculator, Finder, Notes, TextEdit | Reliable | Good candidates for low-risk dogfood. |
| Browser ordinary HTML controls | Usually usable | Target the browser window; page state may be incomplete in complex webviews. |
| Canvas, games, remote desktop | Best effort | Often requires screenshot/coordinate fallback and is less deterministic. |
| Electron or complex WebView | Variable | Accessibility tree quality depends on the app. |
| Password, banking, payment, crypto, Keychain, System Settings | Refused | Hard safety boundary; configuration cannot remove these blocks. |

The Mac agent never exposes Cua directly to the network. The VPS sends one
allow-listed action through the authenticated control plane; the local agent
executes it and returns metadata-only results and redacted trajectory data.

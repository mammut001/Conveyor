# Demo assets

Documentation snapshots from separate Telegram interactions. The MP4 and GIF are a screenshot sequence of those stills, with holds of 3s, 3s, and 4s. They are not a continuous real-time capture, not a live weather forecast, and not a live desktop feed.

Weather frames are not in this directory. The recipe in [docs/demos.md](../../demos.md) is not yet verified by a recording.

## Provenance

| File | Source | What is safe to say |
| :--- | :--- | :--- |
| `telegram-workers-menu.png` | Copy of `visualizations/2026/10/07/01a114f8-9c27-70e3-b048-ae2f67975b7d/telegram-persistent-workers-menu.png`. Operator-approved public demo crop. | Persistent Telegram reply keyboard in a private chat. Same week as the 2026-10-08 deployment shots. The pixels do not show a calendar date. |
| `telegram-workers-live.png` | Copy of `telegram-workers-live.png` in the same visualization directory. | Workers list and the Conveyor worker actions on that deployment. A separate interaction from the menu still. |
| `telegram-screenshot-routing.png` | Copy of `telegram-screenshot-routing-final.png` in the same visualization directory. | Real 2026-10-08 screenshot routing: selected Conveyor main session, shared VPS desktop, node `vps-desktop`. A separate interaction from the two frames above. |
| `telegram-workers-walkthrough.mp4` | Encoded from the three PNGs in this directory, in order, holds 3s + 3s + 4s. H.264, yuv420p, 712×682, faststart. | Screenshot walkthrough / 实拍截图回放. Timing is condensed. Not one continuous screen recording. |
| `telegram-workers-walkthrough.gif` | Palette GIF of that MP4, width 540, for README preview. | Same sequence. GitHub animates the GIF. The MP4 is the download fallback. |

Reviewed before commit: bot display name, a worker label, a desktop thumbnail, and a screenshot id. No tokens, passwords, or `.env` values in the frames. `workers-web-preview.jpg` is a mock and is not in this directory.

Case write-up: [docs/demos.md](../../demos.md). Gallery: [README.md](../../../README.md), [README.zh.md](../../../README.zh.md).

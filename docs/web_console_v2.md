# Web Console V2

This layer consumes the structured session transcript and runtime ownership metadata introduced by the preceding stacked PRs.

Invariants:

- runtime ownership is display-only; the client never forges or chooses an owner id;
- cancel remains an authenticated server action and may be delivered cross-process by the runtime mailbox;
- transcript text is redacted server-side and never stores hidden reasoning;
- session history is durable and independent from the bounded JSONL prompt-context cache;
- reconnecting the browser must not create duplicate messages or events;
- production still serves static assets from Python and requires no Node.js process on the VPS.

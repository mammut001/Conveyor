# Private Web Console PWA (P3-5)

The Web Console can be installed on a phone or desktop and can send *generic*
Web Push alerts when approvals or outbound draft cards need attention.
**Default: disabled.** Enable only on a private tailnet; do not publish the
Web Console via an Internet-facing reverse proxy or Tailscale Funnel.

## Setup on a private Tailscale tailnet

1. Keep **CONVEYOR_WEB_HOST=127.0.0.1** and a strong **CONVEYOR_WEB_TOKEN**
   (32+ unpredictable characters). Enable **CONVEYOR_WEB_ENABLED=1** and
   **CONVEYOR_PWA=1**. Do not bind **0.0.0.0**, forward port 8787, or use Funnel.
2. Install Tailscale on the VPS and the client device, authorize both for the
   same private tailnet, and restrict device access with tailnet ACL/grants.
   Use Tailscale **Serve** (not Funnel) for HTTPS termination on the tailnet.
   For example, with the backend on localhost:8787:

       tailscale serve --bg --https=443 http://127.0.0.1:8787

   Confirm the resulting **https://...ts.net** address is reachable only from
   your tailnet. Inspect with **tailscale serve status**.
   A private Tailscale IP over plain HTTP is *not* a secure browser context;
   service workers and Web Push generally require HTTPS. Loopback is also a
   secure context for local testing.
3. Build and deploy the frontend (**cd web && npm ci && npm run build**) and
   restart the Web Console. Open the HTTPS tailnet URL and authenticate with
   the bearer token. Use your browser's **Install app / Add to Home Screen**
   option. The service worker is **404** when the flag is off; the manifest and icons remain available.
4. To enable Web Push, generate an **application-server VAPID key pair** using
   your push tooling (for example, **python -m py_vapid --gen**). Configure the
   base64url public key as **CONVEYOR_VAPID_PUBLIC_KEY** and the PEM or
   supported private-key path as **CONVEYOR_VAPID_PRIVATE_KEY**. Never commit
   either secret. Deploy the **pywebpush** dependency in requirements.txt.
   Press **Enable alerts** in the authenticated console and allow browser
   notifications. Permission is never requested without an explicit click.

## Security and privacy

- All **/api/push/** calls require the same bearer token as the Web Console.
  Endpoint/crypto keys are stored in a mode-0600 SQLite database under
  **codex_memory_root/state/web_push.sqlite3**, keyed by the configured
  operator identity. The client cannot choose the operator. Up to 10 device
  subscriptions are retained per operator. Push endpoints are allowlisted to
  Google, Mozilla, Apple and Windows services. At each delivery, all DNS
  answers must be public. The TLS connection dials the validated numeric IP
  directly, while HTTP Host, TLS SNI and certificate verification use the
  original service hostname; no second hostname resolution or redirect follows.
  This closes DNS-rebinding TOCTOU for push delivery. Network egress isolation
  remains recommended as defense in depth.
- The push service receives only a generic title and an opaque **id**.
  No email body, draft fields, chat text, prompt, credentials, API token,
  message preview, or approval decision is included. A notification contains
  **no Approve/Reject action buttons**. Tapping opens the private
  **#approvals** view, focused on the matching item. A fresh authenticated
  session is required to see the item or act on it.
- The worker intercepts **only versioned static assets and icons**. It never
  intercepts **/api/**, page navigation, or authenticated requests; asset
  fetches are explicitly credential-free. Responses with **no-store**,
  **private**, **Set-Cookie** or authentication/cookie Vary are never stored.
  Log out with **Lock** to unsubscribe from push, clear PWA caches and
  unregister the worker.
- Browser/Web Push support varies by browser and OS. The browser may still
  communicate with its platform push delivery service even over a private
  tailnet. Messages use the Web Push encryption mechanism, but the delivery
  metadata (such as endpoint and timing) belongs to that service.
- When **CONVEYOR_PWA=0**, new subscriptions, notifications and the
  service worker are disabled. The manifest and icons remain available as on
  main; ordinary Web Console features remain.
  Use **Lock** to unregister an installed PWA worker and clear its caches. Revoke old devices if a device is lost; rotate the Web Console
  bearer token if its storage may be compromised.

## Smoke / tests

Run **python scripts/pwa_smoke.py** for offline authenticated route, feature
flag, private payload, and service-worker guard tests. Web E2E includes a
feature-on/off manifest-link test, including legacy mobile UI. Backend
HTTP tests assert the worker is flag-gated but the manifest remains served.

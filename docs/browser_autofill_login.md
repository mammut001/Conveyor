# G4 extension — model-requested automatic login (default OFF)

**Status: experimental and not approved for external production use.** This
prototype depends on G4 and the G1–G3 flags, plus a restricted server-side
Secrets Store and a one-time, same-origin login POST. Green fixture CI is not
a substitute for the process-level egress isolation and hostile-page review
identified in [browser_external_review.md](security/browser_external_review.md).

## Configuration

Set `CONVEYOR_BROWSER_AUTOFILL_LOGIN=1` and
`CONVEYOR_BROWSER_AUTH_SESSIONS=1`, in addition to all the existing G4
dependency flags. The exact desired HTTPS origin must be present in
`CONVEYOR_BROWSER_AUTH_DOMAINS`; wildcard / lookalike hosts are rejected.
All switches default off. Plain HTTP loopback is available only when
`CONVEYOR_REQUIRE_REAL_BROWSER=1`,
`CONVEYOR_BROWSER_AUTH_TEST_LOOPBACK=1`, and `DISPLAY` is unset.

The authenticated Web Console manages credentials through:
- `POST /api/browser/logins` with `{"domain":"example.org",
  "username":"user","password":"secret","login_url":"https://example.org/login"}`
  (login_url is optional). The response has only `domain`, `username`,
  and `masked:true`.
- `GET /api/browser/logins` returns an array with only those safe fields.
- `DELETE /api/browser/logins?domain=example.org` removes that entry.

The password is stored only in the existing 0600 console Secrets Store with
an origin-keyed name; the existing `/api/secrets` API can also tombstone it.
There is no model-facing credential creation, listing, or retrieval operation.
Requests and responses must never be logged with request bodies.

## Model invocation and limits

The only model tool is `browser.login` with domain-only `arg`.
Its result is restricted to `logged_in`, `failed (reason)`, or
`needs_human (reason)`. The internal executor retrieves the secret, mints
a new G4 task/temporary profile, navigates to the configured exact origin,
refuses iframes, off-origin actions, unsafe controls, or ambiguous forms,
fills one username and one password with Playwright `fill()`, and grants
one exact top-level form POST. **No other G4 submit permission is created.**
The automated context has page JavaScript disabled, so JavaScript-only
sign-in flows fail rather than weakening its containment. No screenshots
are created during the login attempt, and passwords and cookie values
never enter tool responses, journals, audit rows, or exceptions.

After success, the ephemeral cookie is confined to the G4 session.
An OTP/2FA/CAPTCHA/passkey challenge returns `needs_human`, closes the
automated session and requests the existing protected takeover (the human
may need to repeat login in that separate GUI); never forward OTPs or
passwords to the model. Failed login attempts are recorded in a private
SQLite audit with exactly domain, username, result and timestamp; three
failures in one hour lock the domain for that window, and at most three
attempts are accepted per hour, including successful attempts. Revocation,
TTL expiry and the external kill switch remove the G4 session/profile.

G4 startup cleanup searches *only* session user-data directories under the
private G4 root; it sends SIGTERM and, if necessary, SIGKILL solely to
same-UID Chromium processes with the exact matching `--user-data-dir`
argument, retries profile deletion and fails closed on persistent failure.

## Verification

`python -m unittest discover -s tests -p test_browser_autofill_login.py -v`
for offline cases; the required Chromium CI runs the same suite under
`CONVEYOR_REQUIRE_REAL_BROWSER=1`. Smoke:
`python scripts/browser_login_smoke.py`.

**Remaining security gates:** process-wide pinned network egress for
credential-bearing Chromium, hostile-script / redirect adversarial review,
browser-level interception coverage, and independent assessment of fully
automated password handling. Do not enable on genuine third-party portals
until these requirements receive separate security approval.

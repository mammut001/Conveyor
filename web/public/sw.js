/* Minimal private-console worker: only immutable same-origin static assets. */
const CACHE = 'conveyor-static-pwa-v1'
const STATIC = /^\/assets\/[A-Za-z0-9_./-]+\.(?:js|css|svg|png|webp|woff2?)$/
const ICONS = /^\/icon(?:-192|-512)?\.(?:png|svg)$/

self.addEventListener('install', () => { self.skipWaiting() })
self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) if (name.startsWith('conveyor-static-pwa-') && name !== CACHE) await caches.delete(name)
    await self.clients.claim()
  })())
})
self.addEventListener('message', (event) => {
  if (event.data?.type === 'LOGOUT' || event.data?.type === 'CLEAR_CACHES') {
    event.waitUntil((async () => {
      for (const name of await caches.keys()) if (name.startsWith('conveyor-static-pwa-')) await caches.delete(name)
    })())
  }
})
self.addEventListener('fetch', (event) => {
  const request = event.request
  const url = new URL(request.url)
  // No navigation, /api, cross-origin, non-GET or authenticated responses.
  if (request.method !== 'GET' || url.origin !== self.location.origin ||
      url.pathname.startsWith('/api/') || request.mode === 'navigate' ||
      request.headers.has('Authorization') || request.headers.has('Cookie') ||
      !(STATIC.test(url.pathname) || ICONS.test(url.pathname))) return
  event.respondWith((async () => {
    // Cache only a deliberately credential-free fetch: browser-added Cookie
    // headers may not appear in the service worker request headers.
    const anonymous = new Request(request, { credentials: 'omit' })
    const cache = await caches.open(CACHE)
    const cached = await cache.match(anonymous)
    if (cached) return cached
    const response = await fetch(anonymous)
    const policy = response.headers.get('Cache-Control') || ''
    const vary = response.headers.get('Vary') || ''
    if (response.ok && response.type === 'basic' && !response.headers.has('Set-Cookie') &&
        !/private|no-store|no-cache/i.test(policy) && !/authorization|cookie|\*/i.test(vary)) {
      await cache.put(anonymous, response.clone())
    }
    return response
  })())
})
self.addEventListener('push', (event) => {
  if (!event.data) return
  let data
  try { data = event.data.json() } catch { return }
  if (!data || typeof data.id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(data.id)) return
  // Goal pushes carry a generic Chinese title (the goal text never leaves the machine).
  const kind = data.kind === 'goal' ? 'goal' : 'review'
  const title = kind === 'goal'
    ? (typeof data.title === 'string' && data.title ? data.title : 'Conveyor：目标需要你看一下')
    : 'Conveyor needs your review'
  event.waitUntil(self.registration.showNotification(title, {
    tag: 'conveyor-' + kind + '-' + data.id,
    data: { id: data.id, kind: kind },
    icon: '/icon-192.png',
  }))
})
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const data = event.notification.data || {}
  const id = data.id
  if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(id)) return
  // Deep link only; approval decisions ALWAYS require bearer-authenticated UI.
  // A goal needs the owner on the 项目 page, not the approvals list.
  const url = data.kind === 'goal' ? '/#code' : '/?approval=' + encodeURIComponent(id) + '#approvals'
  event.waitUntil((async () => {
    const clients = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
    for (const client of clients) {
      if (new URL(client.url).origin === self.location.origin) {
        await client.navigate(url)
        await client.focus()
        return
      }
    }
    await self.clients.openWindow(url)
  })())
})

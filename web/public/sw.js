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
    const cache = await caches.open(CACHE)
    const cached = await cache.match(request)
    if (cached) return cached
    const response = await fetch(request)
    const policy = response.headers.get('Cache-Control') || ''
    const vary = response.headers.get('Vary') || ''
    if (response.ok && response.type === 'basic' && !response.headers.has('Set-Cookie') &&
        !/private|no-store|no-cache/i.test(policy) && !/authorization|cookie|\*/i.test(vary)) {
      await cache.put(request, response.clone())
    }
    return response
  })())
})
self.addEventListener('push', (event) => {
  if (!event.data) return
  let data
  try { data = event.data.json() } catch { return }
  if (!data || typeof data.id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(data.id)) return
  event.waitUntil(self.registration.showNotification('Conveyor needs your review', {
    tag: 'conveyor-review-' + data.id,
    data: { id: data.id },
    icon: '/icon-192.png',
  }))
})
self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const id = event.notification.data?.id
  if (typeof id !== 'string' || !/^[A-Za-z0-9_-]{1,128}$/.test(id)) return
  // Deep link only; approval decisions ALWAYS require bearer-authenticated UI.
  const url = '/?approval=' + encodeURIComponent(id) + '#approvals'
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

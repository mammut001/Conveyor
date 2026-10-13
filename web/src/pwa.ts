/** Authenticated, opt-in private PWA lifecycle. No bearer token reaches the worker. */
export type PushConfig = { enabled: boolean; public_key: string }
const WORKER_URL = '/sw.js'
const prefix = 'conveyor-static-pwa-'
let currentId: string | null = null

async function discardCaches() {
  if (!('caches' in window)) return
  for (const key of await caches.keys()) if (key.startsWith(prefix)) await caches.delete(key)
}

function byteArray(base64url: string): Uint8Array<ArrayBuffer> {
  const raw = atob(base64url.replace(/-/g, '+').replace(/_/g, '/').padEnd(Math.ceil(base64url.length / 4) * 4, '='))
  const bytes = new Uint8Array(new ArrayBuffer(raw.length))
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i)
  return bytes
}

export async function setupWorker(): Promise<ServiceWorkerRegistration | null> {
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return null
  return navigator.serviceWorker.register(WORKER_URL, { scope: '/' })
}

export async function enablePush(token: string): Promise<boolean> {
  if (!('PushManager' in window) || !('Notification' in window)) return false
  const registration = await setupWorker()
  if (!registration) return false
  const res = await fetch('/api/push/config', { headers: { Authorization: 'Bearer ' + token }, cache: 'no-store' })
  if (!res.ok) return false
  const config = await res.json() as PushConfig
  if (!config.enabled || !config.public_key) return false
  if ((await Notification.requestPermission()) !== 'granted') return false
  let subscription = await registration.pushManager.getSubscription()
  if (!subscription) subscription = await registration.pushManager.subscribe({
    userVisibleOnly: true, applicationServerKey: byteArray(config.public_key),
  })
  const response = await fetch('/api/push/subscribe', {
    method: 'POST', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ subscription: subscription.toJSON() }), cache: 'no-store',
  })
  if (!response.ok) return false
  const { id } = await response.json() as { id: string }
  currentId = id
  return true
}

export async function leavePwa(token: string) {
  if ('serviceWorker' in navigator) {
    for (const reg of await navigator.serviceWorker.getRegistrations()) {
      if (reg.scope !== new URL('/', location.href).href) continue
      reg.active?.postMessage({ type: 'LOGOUT' })
      reg.waiting?.postMessage({ type: 'LOGOUT' })
      const subscription = await reg.pushManager?.getSubscription().catch(() => null)
      if (subscription) {
        const endpoint = subscription.endpoint
        const enc = new TextEncoder().encode(endpoint)
        const digest = await crypto.subtle.digest('SHA-256', enc)
        const id = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
        if (token) await fetch('/api/push/subscribe', {
          method: 'DELETE', headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
          body: JSON.stringify({ id }), cache: 'no-store', keepalive: true,
        }).catch(() => undefined)
        await subscription.unsubscribe().catch(() => false)
      }
      await reg.unregister()
    }
  }
  await discardCaches()
  currentId = null
}

export async function disablePwa() {
  // Used when the server flag is off (including after a deployment).
  await leavePwa('')
}

export function pushSubscriptionId(): string | null { return currentId }

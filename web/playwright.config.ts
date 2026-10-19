import { defineConfig } from '@playwright/test'

const port = Number(process.env.CONVEYOR_E2E_PORT || 4173)
const origin = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  reporter: 'list',
  use: {
    baseURL: origin,
    headless: true,
    viewport: { width: 1440, height: 1000 },
    screenshot: 'on',
  },
  webServer: {
    command: `npx vite preview --host 127.0.0.1 --port ${port} --strictPort`,
    url: origin,
    // Never silently run against another project's server on this port.
    reuseExistingServer: false,
    timeout: 60_000,
  },
})

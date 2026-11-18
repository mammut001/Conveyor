import { expect, test, type Route } from '@playwright/test'

const TOKEN = 'n1-project-landing-mock-token-0001'
const DESKTOP = { width: 1440, height: 900 }

// Mock capabilities, repos, etc. same as n1-project-landing.spec.ts
// ... (I will just copy necessary mock setup) ...
// Actually, I can probably import them, but the file is not exported for reuse.
// I will just copy the necessary parts.

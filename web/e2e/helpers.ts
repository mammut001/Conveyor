import type { Page } from '@playwright/test'

/**
 * Open a top-level shell destination by its Chinese label (e.g. '对话', '任务', '记忆', '执行者').
 * Main nav buttons are clicked directly; items inside 「更多」 are opened through the
 * desktop 更多 popover or the phone 底部导航 -> 更多 sheet.
 */
export async function openPage(page: Page, label: string): Promise<void> {
  const bottomNav = page.getByRole('navigation', { name: '底部导航' })
  if (await bottomNav.isVisible()) {
    const direct = bottomNav.getByRole('button', { name: new RegExp(`^${label}(?:[\\s\\d,]|$)`) })
    if (await direct.count()) {
      await direct.first().click()
      return
    }
    await bottomNav.getByRole('button', { name: '更多' }).click()
    const sheet = page.getByRole('dialog', { name: '更多' })
    await sheet.getByRole('button', { name: label, exact: true }).click()
    return
  }

  const nav = page.getByRole('navigation', { name: '主导航' })
  const mainGroup = nav.locator('.shell-nav-group')
  const direct = mainGroup.getByRole('button', { name: new RegExp(`^${label}(?:[\\s\\d,]|$)`) })
  if (await direct.count()) {
    await direct.first().click()
    return
  }

  const moreBtn = nav.locator('.shell-nav-more > button')
  if ((await moreBtn.getAttribute('aria-expanded')) !== 'true') {
    await moreBtn.click()
  }
  await page.locator('#shell-nav-menu').getByRole('button', { name: label, exact: true }).click()
}

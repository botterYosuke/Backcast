import { expect, test } from '@playwright/test';

const RELEASE_ID = '1a45c99b4a6b884fbfc17240ae65759f20601dc86267df3d2b9a160de63a45da';
const RELEASE_ENTRY = `/static/strategy/releases/${RELEASE_ID}/strategy-release.mjs`;

async function openApplication(page) {
  await page.goto('/app/');
  const workspace = page.locator('[data-strategy-workspace]');
  await expect(workspace).toHaveAttribute('data-strategy-mode', 'degraded');
  await expect(page.locator('[data-strategy-source]')).not.toHaveAttribute('readonly', '');
  return workspace;
}

test('production closure preserves lazy boundaries and runs Load/Start/lifecycle recovery', async ({ page }) => {
  const requests = [];
  const violations = [];
  page.on('request', (request) => requests.push(request.url()));
  await page.exposeFunction('recordApplicationPolicyViolation', (event) => violations.push(event));
  await page.addInitScript(() => document.addEventListener('securitypolicyviolation', (event) => {
    window.recordApplicationPolicyViolation({
      blockedURI: event.blockedURI,
      violatedDirective: event.violatedDirective,
    });
  }));
  const workspace = await openApplication(page);

  expect(requests.some((url) => url.endsWith(RELEASE_ENTRY))).toBe(true);
  expect(requests.some((url) => url.includes('/vendor/codemirror/'))).toBe(false);
  expect(requests.some((url) => url.includes('/vendor/pyodide/'))).toBe(false);

  await page.getByRole('button', { name: '戦略を開く' }).click();
  await expect(workspace).toHaveAttribute('data-strategy-mode', 'enhanced');
  await expect(page.locator('.strategy-editor .cm-editor')).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Python 戦略ソース' })).toHaveCount(1);
  await expect(page.locator('[data-strategy-source]')).toHaveAttribute('aria-hidden', 'true');
  expect(requests.some((url) => url.includes('/vendor/codemirror/'))).toBe(true);
  expect(requests.some((url) => url.includes('/vendor/pyodide/'))).toBe(false);

  await page.getByRole('button', { name: 'Load', exact: true }).click();
  await expect(page.locator('[data-strategy-status]')).toContainText('loaded revision', {
    timeout: 30000,
  });
  expect(requests.some((url) => url.includes('/vendor/pyodide/'))).toBe(true);
  expect(await page.evaluate(() => window.tickReplayStrategyHost.getControllerState().tag)).toBe('ready');

  await page.getByRole('button', { name: 'Start', exact: true }).click();
  await expect(page.locator('[data-strategy-status]')).toContainText('戦略 run');
  expect(await page.evaluate(() => window.tickReplayStrategyHost.getControllerState().tag)).toBe('paused');
  await page.evaluate(() =>
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: true })),
  );
  await page.waitForFunction(() => !window.tickReplayStrategyHost.getControllerState()?.inFlight);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true })));
  await expect(page.locator('[data-strategy-status]')).toContainText('Restart が必要');
  await expect(page.getByRole('button', { name: 'Restart', exact: true })).toBeEnabled();

  const network = requests.map((value) => new URL(value));
  expect(network.filter(({ origin }) => origin !== 'http://127.0.0.1:8765')).toEqual([]);
  expect(network.some(({ hostname }) => /cdn|unpkg|jsdelivr|npmjs/.test(hostname))).toBe(false);
  expect(violations).toEqual([]);
});

test('CodeMirror and Pyodide failures stay narrower than raw-copy recovery', async ({ page }) => {
  await page.route('**/vendor/codemirror/**/strategy-editor.mjs', (route) => route.abort());
  const workspace = await openApplication(page);
  await page.getByRole('button', { name: '戦略を開く' }).click();
  await expect(workspace).toHaveAttribute('data-strategy-mode', 'degraded');
  await expect(page.locator('[data-strategy-source]')).not.toHaveAttribute('readonly', '');
  await expect(page.getByRole('button', { name: 'Save Draft' })).toBeEnabled();

  await page.route('**/vendor/pyodide/**/pyodide.mjs', (route) => route.abort());
  await page.getByRole('button', { name: 'Load', exact: true }).click();
  await expect(page.locator('[data-strategy-status]')).toContainText('Load に失敗', {
    timeout: 30000,
  });
  await expect(page.locator('[data-strategy-source]')).not.toHaveAttribute('readonly', '');
  await expect(page.getByRole('button', { name: 'Save Draft' })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Load', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Start', exact: true })).toBeDisabled();
});

test('release bootstrap failure leaves raw-copy recovery without breaking replay UI', async ({ page }) => {
  await page.route(`**${RELEASE_ENTRY}`, (route) => route.abort());
  await page.goto('/app/');
  const workspace = page.locator('[data-strategy-workspace]');

  await expect(workspace).toHaveAttribute('data-strategy-mode', 'raw-recovery');
  await expect(page.locator('[data-strategy-source]')).toHaveAttribute('readonly', '');
  await expect(page.locator('[data-strategy-source]')).toBeVisible();
  await expect(page.locator('[data-strategy-status]')).toContainText('選択とコピー');
  await expect(page.getByRole('button', { name: '▶' })).toBeVisible();
  expect(await page.locator('[data-strategy-action]').evaluateAll((controls) => (
    controls.every((control) => control.disabled)
  ))).toBe(true);
});

test('narrow workspace keeps one named editor, touch targets, and bounded width', async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith('mobile'), 'mobile-only layout evidence');
  await openApplication(page);
  await page.getByRole('button', { name: '戦略を開く' }).click();
  await expect(page.getByRole('textbox', { name: 'Python 戦略ソース' })).toHaveCount(1);
  const layout = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    viewportWidth: window.innerWidth,
    targets: [...document.querySelectorAll('.strategy-actions button, .strategy-file-button')]
      .map((element) => element.getBoundingClientRect().height),
    editorHeight: document.querySelector('.strategy-editor').getBoundingClientRect().height,
  }));
  expect(layout.scrollWidth).toBeLessThanOrEqual(layout.viewportWidth);
  expect(layout.targets.every((height) => height >= 44)).toBe(true);
  expect(layout.editorHeight).toBeGreaterThanOrEqual(page.viewportSize().height * 0.5);
});

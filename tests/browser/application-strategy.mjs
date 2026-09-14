import { expect, test } from '@playwright/test';

const RELEASE_ID = 'fde82a53f12e81c6fbac68d116c2afc3fd8e0254907dd7b6e93cb9750a63f14d';
const RELEASE_ENTRY = `/static/strategy/releases/${RELEASE_ID}/strategy-release.mjs`;
const LOAD_MARKER_SOURCE = `print("LOAD_ONLY_MARKER")
def on_event(event):
    return []
`;
const BRIDGE_SOURCE = `print("BRIDGE_LOAD_ONLY")
def on_event(event):
    tick_index = event["tick"]["session_tick_index"]
    portfolio = event["portfolio"]
    print(
        "BRIDGE",
        tick_index,
        portfolio["position_quantity"],
        portfolio["cash_minor"],
        portfolio["unrealized_pnl_minor"],
        portfolio["equity_minor"],
        event["open_orders_total"],
        len(event["fills"]),
        event["fill_cursor"],
    )
    if tick_index == 1:
        return [
            {"type": "submit_market", "client_intent_id": "market", "side": "buy", "quantity": 2},
            {"type": "submit_limit", "client_intent_id": "pending", "side": "sell", "quantity": 1, "limit_price_level": 200},
        ]
    if tick_index == 2:
        return [{"type": "cancel_strategy_orders", "client_intent_id": "cancel"}]
    return []
`;

async function openApplication(page) {
  await page.goto('/app/');
  const workspace = page.locator('[data-strategy-workspace]');
  await expect(workspace).toHaveAttribute('data-strategy-mode', 'degraded');
  await expect(page.locator('[data-strategy-source]')).not.toHaveAttribute('readonly', '');
  return workspace;
}

async function replaceStrategySource(page, source) {
  const editor = page.getByRole('textbox', { name: 'Python 戦略ソース' });
  await editor.focus();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.insertText(source);
  await expect(page.locator('[data-strategy-source]')).toHaveValue(source);
}

async function expectExecutionWarning(page) {
  const warning = page.locator('#strategy-execution-warning');
  await expect(warning).toBeVisible();
  await expect(warning).toContainText('信頼できるコードだけ');
  await expect(warning).toContainText('Start より前にトップレベルの Python が直ちに実行');
  await expect(warning).toContainText('Worker はセキュリティ sandbox ではなく');
  await expect(warning).toContainText('ネットワーク通信や同一オリジン');
  await expect(page.getByRole('button', { name: 'Load', exact: true }))
    .toHaveAttribute('aria-describedby', 'strategy-execution-warning');
  const editor = page.getByRole('textbox', { name: 'Python 戦略ソース' });
  await expect(editor).toHaveAttribute('aria-describedby', /(?:^|\s)strategy-execution-warning(?:\s|$)/);
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
  await expectExecutionWarning(page);
  expect(requests.some((url) => url.includes('/vendor/codemirror/'))).toBe(true);
  expect(requests.some((url) => url.includes('/vendor/pyodide/'))).toBe(false);

  await replaceStrategySource(page, LOAD_MARKER_SOURCE);
  await page.getByRole('button', { name: 'Load', exact: true }).click();
  await expect(page.locator('[data-strategy-status]')).toContainText('loaded revision', {
    timeout: 30000,
  });
  await expect(page.locator('[data-strategy-logs]')).toContainText('LOAD_ONLY_MARKER');
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

test('production Worker preserves causal fills, cancellation, Stop, and restart', async ({ page }) => {
  test.setTimeout(90000);
  const workspace = await openApplication(page);
  await page.getByRole('button', { name: '戦略を開く' }).click();
  await expect(workspace).toHaveAttribute('data-strategy-mode', 'enhanced');
  await replaceStrategySource(page, BRIDGE_SOURCE);
  await page.getByRole('button', { name: 'Load', exact: true }).click();
  await expect(page.locator('[data-strategy-status]')).toContainText('loaded revision', {
    timeout: 30000,
  });
  await expect(page.locator('[data-strategy-logs]')).toContainText('BRIDGE_LOAD_ONLY', {
    timeout: 30000,
  });
  const play = page.locator('#play-button');
  const logs = page.locator('[data-strategy-logs]');
  await page.getByRole('button', { name: 'x0.5' }).click();

  await play.click();
  await expect(page.locator('#tape > li')).toHaveCount(1);
  await play.click();
  const manualLimit = await page.evaluate(() => {
    const candidates = [...document.querySelectorAll('#board > li')]
      .map((row) => ({ row, price: Number(row.querySelector('.board-price')?.textContent) }))
      .filter(({ price }) => Number.isFinite(price) && price < 100)
      .sort((left, right) => right.price - left.price);
    const selected = candidates[0];
    if (!selected) return null;
    const cell = selected.row.querySelector('.order-buy');
    cell.click();
    return { price: selected.price, quantity: cell.textContent };
  });
  expect(manualLimit).not.toBeNull();
  expect(manualLimit.price).toBeLessThan(100);
  expect(manualLimit.quantity).toBe('100');

  await page.getByRole('button', { name: 'Start', exact: true }).click();
  const initialRunId = await page.evaluate(() => window.tickReplayStrategyHost.getControllerState()?.runId);
  expect(initialRunId).toBeTruthy();

  await play.click();
  await expect(logs).toContainText('BRIDGE 1 0 0 0 0 1 0 0');
  await play.click();
  await expect(page.locator('#tape > li')).toHaveCount(2);
  await expect(page.locator('#pos-qty')).toHaveText('—');
  await expect(page.locator('#scrubber')).toHaveValue('250');

  await play.click();
  await expect(logs).toContainText('BRIDGE 2 2 -20400 0 0 2 1 1');
  await play.click();
  await expect(page.locator('#tape > li')).toHaveCount(3);
  await expect(page.locator('#pos-qty')).toHaveText('買 2 株');
  await expect(page.locator('#pos-avg')).toHaveText('102');
  await expect(page.locator('#scrubber')).toHaveValue('500');

  await play.click();
  await expect(logs).toContainText('BRIDGE 3 2 -20400 200 200 1 0 1');
  await play.click();
  await expect(page.locator('#tape > li')).toHaveCount(4);
  await expect(page.locator('#scrubber')).toHaveValue('750');

  await page.getByRole('button', { name: 'Stop', exact: true }).click();
  await page.waitForFunction(() => window.tickReplayStrategyHost.getControllerState()?.tag === 'idle');
  await page.locator('#reset-button').click();
  await expect(page.locator('#tape > li')).toHaveCount(0);
  await expect(page.locator('#pos-qty')).toHaveText('—');
  await page.getByRole('button', { name: 'Restart', exact: true }).click();
  await page.waitForFunction(
    (previousRunId) => {
      const state = window.tickReplayStrategyHost.getControllerState();
      return state?.tag === 'paused' && Boolean(state.runId) && state.runId !== previousRunId;
    },
    initialRunId,
    { timeout: 30000 },
  );
  await expect.poll(async () => (
    (await logs.textContent()).match(/BRIDGE_LOAD_ONLY/g) ?? []
  ).length, { timeout: 30000 }).toBe(2);
  await play.click();
  await expect.poll(async () => (
    (await logs.textContent()).match(/BRIDGE 0 0 0 0 0 0 0 0/g) ?? []
  ).length).toBe(1);
  await play.click();
  await expect(play).toHaveText('▶');
  await page.waitForFunction(() => window.tickReplayStrategyHost.getControllerState()?.tag === 'paused');
  await expect(page.locator('#tape > li')).toHaveCount(1);
  await expect(page.locator('#pos-qty')).toHaveText('—');
});

test('production Worker converts a Python callback error and commits no order', async ({ page }) => {
  const workspace = await openApplication(page);
  await page.getByRole('button', { name: '戦略を開く' }).click();
  await expect(workspace).toHaveAttribute('data-strategy-mode', 'enhanced');
  await replaceStrategySource(page, `def on_event(event):
    raise RuntimeError("deterministic production bridge error")
`);
  await page.getByRole('button', { name: 'Load', exact: true }).click();
  await expect(page.locator('[data-strategy-status]')).toContainText('loaded revision', {
    timeout: 30000,
  });
  await page.getByRole('button', { name: 'Start', exact: true }).click();
  await page.locator('#play-button').click();
  await expect(page.locator('[data-strategy-diagnostics]')).toContainText(
    'deterministic production bridge error',
  );
  await expect(page.locator('#pos-qty')).toHaveText('—');
});

test('CodeMirror and Pyodide failures stay narrower than raw-copy recovery', async ({ page }) => {
  await page.route('**/vendor/codemirror/**/strategy-editor.mjs', (route) => route.abort());
  const workspace = await openApplication(page);
  await page.getByRole('button', { name: '戦略を開く' }).click();
  await expect(workspace).toHaveAttribute('data-strategy-mode', 'degraded');
  await expect(page.locator('[data-strategy-source]')).not.toHaveAttribute('readonly', '');
  await expect(page.getByRole('button', { name: 'Save Draft' })).toBeEnabled();
  await expectExecutionWarning(page);

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
  const workspace = await openApplication(page);
  await page.getByRole('button', { name: '戦略を開く' }).click();
  await expect(workspace).toHaveAttribute('data-strategy-mode', 'enhanced');
  await expect(page.locator('.strategy-editor .cm-editor')).toBeVisible();
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

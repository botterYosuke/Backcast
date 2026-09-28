import { expect, test } from '@playwright/test';

test('Bundled RL real backend: Load is inert and Start fills entry and exit exactly on next ticks', async ({ page }) => {
  await page.addInitScript(() => {
    class BrokerSnapshotProbe extends Error {}
    const probe = { beforeLoad: null, afterLoad: null, brokerDispatchCount: 0 };
    let probingStart = false;
    let capturedStartInput = null;
    let wrappedHost;
    window.bundledRlBrokerProbe = probe;
    Object.defineProperty(window, 'tickReplayStrategyHost', {
      get: () => wrappedHost,
      set(host) {
        const captureStartInput = async () => {
          probingStart = true;
          capturedStartInput = null;
          try {
            await host.start();
            throw new Error('probe_start_did_not_abort');
          } catch (error) {
            if (!(error instanceof BrokerSnapshotProbe)) throw error;
            return capturedStartInput;
          } finally {
            probingStart = false;
          }
        };
        wrappedHost = Object.freeze({
          ...host,
          attachController(controller) {
            const wrappedController = new Proxy(controller, {
              get(target, name) {
                const value = Reflect.get(target, name, target);
                if (name === 'start') return (...args) => {
                  if (probingStart) {
                    capturedStartInput = structuredClone(args[0]);
                    throw new BrokerSnapshotProbe('probe_start_input_captured');
                  }
                  return value.apply(target, args);
                };
                if (name === 'dispatchOneEvent') return (...args) => {
                  probe.brokerDispatchCount += 1;
                  return value.apply(target, args);
                };
                return typeof value === 'function' ? value.bind(target) : value;
              },
            });
            return host.attachController(wrappedController);
          },
          async load(...args) {
            probe.beforeLoad = await captureStartInput();
            const result = await host.load(...args);
            probe.afterLoad = await captureStartInput();
            return result;
          },
        });
      },
    });
  });
  const nativeRequests = [];
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/strategies/bundled-rl/')) {
      nativeRequests.push(`${request.method()} ${new URL(request.url()).pathname}`);
    }
  });
  await page.goto('/');
  await page.locator('#symbol-input').fill('285A');
  await page.locator('#date-input').fill('2026-09-10');
  await page.locator('#load-button').click();
  await expect(page.locator('#clock')).toHaveText('09:00:59 / 09:01:40');
  await page.getByRole('button', { name: '戦略を開く' }).click();
  await expect(page.locator('select[data-strategy-selection]')).toHaveValue('bundled_rl');

  const brokerSnapshot = () => page.evaluate(() => ({
    tapeCount: document.querySelectorAll('#tape > li').length,
    position: document.querySelector('#pos-qty')?.textContent,
    average: document.querySelector('#pos-avg')?.textContent,
    pnlFills: document.querySelector('#pnl-fills')?.textContent,
    clock: document.querySelector('#clock')?.textContent,
  }));
  const beforeLoad = await brokerSnapshot();
  await page.getByRole('button', { name: 'Load', exact: true }).click();
  await expect(page.locator('[data-strategy-status]')).toContainText('Bundled RL トレースを loaded revision');
  expect(await brokerSnapshot()).toEqual(beforeLoad);
  const loadProbe = await page.evaluate(() => window.bundledRlBrokerProbe);
  expect(loadProbe.beforeLoad).not.toBeNull();
  expect(loadProbe.afterLoad).not.toBeNull();
  for (const field of ['cursor', 'lastCommitted', 'portfolio', 'openOrders', 'openOrdersTotal', 'openOrdersTruncated']) {
    expect(loadProbe.afterLoad[field], field).toEqual(loadProbe.beforeLoad[field]);
  }
  expect(loadProbe.afterLoad.openOrders).toEqual([]);
  expect(loadProbe.afterLoad.openOrdersTotal).toBe(0);
  expect(loadProbe.afterLoad.openOrdersTruncated).toBe(false);
  expect(loadProbe.brokerDispatchCount).toBe(0);
  const requestsAfterTrace = nativeRequests.length;

  await page.getByRole('button', { name: 'Start', exact: true }).click();
  const play = page.locator('#play-button');
  await play.click();

  // observed tick 0 takes the flat branch (target 0), proving no pre-commit
  // long branch is selected despite that branch targeting 100.
  await expect(page.locator('#tape > li')).toHaveCount(1);
  await expect(page.locator('#pos-qty')).toHaveText('—');

  // Tick 1 proposes entry. It cannot fill at i and must fill at exactly i+1.
  await expect(page.locator('#tape > li')).toHaveCount(2);
  await expect(page.locator('#pos-qty')).toHaveText('—');
  await expect(page.locator('#tape > li')).toHaveCount(3);
  await expect(page.locator('#pos-qty')).toHaveText('買 100 株');
  await expect(page.locator('#pos-avg')).toHaveText('40');

  // The tick-2 dispatch observes that committed 100-share position and picks
  // its long branch. Exit is ineligible at i and fills exactly at i+1, not i+2.
  await expect(page.locator('#tape > li')).toHaveCount(4);
  await expect(page.locator('#pos-qty')).toHaveText('—');
  await play.click();
  await expect(page.locator('#tape > li')).toHaveCount(4);
  expect(await page.evaluate(() => window.bundledRlBrokerProbe.brokerDispatchCount)).toBe(4);
  expect(nativeRequests.length).toBe(requestsAfterTrace);
});

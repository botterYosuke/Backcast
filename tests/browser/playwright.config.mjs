import { defineConfig, devices } from "@playwright/test";

const repositoryRoot = new URL("../../", import.meta.url).pathname;
const pythonCommand = process.env.STRATEGY_SPIKE_PYTHON ?? "uv run python";
const systemChrome = process.env.STRATEGY_SPIKE_USE_SYSTEM_CHROME === "1"
  ? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
  : undefined;

export default defineConfig({
  testDir: new URL(".", import.meta.url).pathname,
  testMatch: ["tickreplay-strategy.mjs", "application-strategy.mjs"],
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  timeout: 45000,
  expect: { timeout: 10000 },
  outputDir: `${repositoryRoot}.agents/logs/playwright-artifacts`,
  reporter: [
    ["line"],
    ["json", { outputFile: ".agents/logs/strategy-spike-playwright.json" }],
  ],
  use: {
    baseURL: "http://127.0.0.1:8765",
    serviceWorkers: "block",
    trace: "retain-on-failure",
  },
  webServer: {
    command: `${pythonCommand} tests/browser/spike_server.py --port 8765`,
    cwd: repositoryRoot,
    url: "http://127.0.0.1:8765/spike/",
    reuseExistingServer: false,
    timeout: 15000,
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"], launchOptions: { executablePath: systemChrome } } },
    { name: "firefox", use: { ...devices["Desktop Firefox"] } },
    { name: "webkit", use: { ...devices["Desktop Safari"] } },
    { name: "mobile-chromium", use: { ...devices["Pixel 7"], browserName: "chromium", launchOptions: { executablePath: systemChrome } } },
    { name: "mobile-webkit", use: { ...devices["iPhone 14"], browserName: "webkit" } },
  ],
});

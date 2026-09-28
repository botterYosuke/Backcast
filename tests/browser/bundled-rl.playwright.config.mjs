import { defineConfig, devices } from '@playwright/test';

const repositoryRoot = new URL('../../', import.meta.url).pathname;
const systemChrome = process.env.STRATEGY_SPIKE_USE_SYSTEM_CHROME === '1'
  ? '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  : undefined;
const prepare = [
  "from pathlib import Path",
  "import duckdb",
  "root=Path('/tmp/backcast-bundled-rl-browser-cache')",
  "path=root/'stocks_trades'/'285A.duckdb'",
  "path.parent.mkdir(parents=True,exist_ok=True)",
  "path.unlink(missing_ok=True)",
  "db=duckdb.connect(str(path))",
  "db.execute(\"CREATE TABLE stocks_board (Price DOUBLE, Qty BIGINT, Type VARCHAR, source VARCHAR, Code VARCHAR, Timestamp VARCHAR)\")",
  "db.execute(\"CREATE TABLE stocks_board_metadata (Code VARCHAR, from_timestamp TIMESTAMP, to_timestamp TIMESTAMP, record_count BIGINT, last_updated TIMESTAMP)\")",
  "rows=[(100,100,'1','285A0','2026-09-10 09:01:00'),(40,100,'2','285A0','2026-09-10 09:01:10'),(40,100,'1','285A0','2026-09-10 09:01:20'),(40,100,'2','285A0','2026-09-10 09:01:30'),(40,100,'1','285A0','2026-09-10 09:01:40')]",
  "db.executemany(\"INSERT INTO stocks_board VALUES (?, ?, ?, 'browser-fixture', ?, ?)\",rows)",
  "db.execute(\"INSERT INTO stocks_board_metadata VALUES ('285A0', TIMESTAMP '2026-09-10 09:01:00', TIMESTAMP '2026-09-10 09:01:40', 5, TIMESTAMP '2026-09-28 00:00:00')\")",
  "db.close()",
].join(';');
const prepareBase64 = Buffer.from(prepare).toString('base64');

export default defineConfig({
  testDir: new URL('.', import.meta.url).pathname,
  testMatch: ['**/bundled-rl-real.mjs'],
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  outputDir: `${repositoryRoot}.agents/logs/bundled-rl-playwright-artifacts`,
  reporter: [['line']],
  use: {
    baseURL: 'http://127.0.0.1:8766',
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `uv run python -c "import base64;exec(base64.b64decode('${prepareBase64}'))" && BACKCAST_DUCKDB_CACHE_DIR=/tmp/backcast-bundled-rl-browser-cache BACKCAST_DUCKDB_LOCAL_AUTHORITATIVE=true uv run python run.py --no-browser --port 8766`,
    cwd: repositoryRoot,
    url: 'http://127.0.0.1:8766/',
    reuseExistingServer: false,
    timeout: 30_000,
  },
  projects: [{
    name: 'chromium',
    use: { ...devices['Desktop Chrome'], launchOptions: { executablePath: systemChrome } },
  }],
});

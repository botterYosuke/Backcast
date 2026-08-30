## Feature Brief: Daily chart and moving averages

### Current State
- Architecture: TickReplay has one Lightweight Charts minute candlestick/volume chart. Historical minute bars come from `/api/minute-context`, while replay ticks update the current minute bar in `app.js`.
- Relevant files: `src/tickreplay/server.py`, `src/tickreplay/static/app.js`, `src/tickreplay/static/index.html`, `src/tickreplay/static/styles.css`, `cloud-run/main.py`, and their Python/Node tests.
- Patterns: FastAPI endpoints wrap typed DuckDB loaders; browser calculations live in DOM-free native ES modules; `RequestCoordinator` rejects stale requests; focused Python and `node:test` suites protect integrations.

### Feature Goal
Allow the upper candlestick chart to switch between minute and daily views. Daily mode displays 25-session and 200-session simple moving averages without exposing future values from the selected replay day.

### Scope
- Include: an accessible minute/daily selector; official per-symbol `stocks_daily` loading; a strict-before daily REST endpoint; point-in-time raw OHLCV; a replay-derived partial current-day candle; SMA25/SMA200 in daily mode; stale-response and mode isolation; responsive styling; focused tests and documentation.
- Exclude: changes to tick chart, tape, board, paper trading, replay speed, minute-history behavior, daily trade markers, unlimited daily paging, new dependencies, database migrations, or changes to existing API response contracts.

### Complexity Classification (from Codex)
- Classification: COMPLEX
- Estimated files: 12-14
- Estimated LOC: 500-900 including tests
- Implementation route: team-execute

### Integration Points
- Daily storage: `stocks_daily/{stem}.duckdb` must span legacy/current Code partitions and return chronological raw bars before the selected session date, matching the replay tick price scale without future corporate-action adjustment.
- TickReplay API: `/api/daily-context` loads/caches the daily file without changing `/api/session` or `/api/minute-context`.
- Replay lifecycle: replay ticks build the selected day's partial daily candle; seek/reset rebuild it deterministically.
- Chart lifecycle: minute and daily series/viewport state stay isolated so replay updates and minute-history prepends cannot overwrite daily data.
- Request lifecycle: a dedicated `daily-context` request kind and session identity checks reject stale results.

### Risks
- Look-ahead leakage: never return the selected day's completed stored OHLC; derive it only from replayed ticks.
- Code transition: query the whole per-stem date series rather than only the current Code so SMA200 has enough history.
- Corporate actions: raw point-in-time prices can create split discontinuities in a long SMA; document this known limitation rather than applying a future-informed adjustment ratio.
- Race and shared-series corruption: separate daily chart state and guard async commits by session/mode identity.
- Short histories or missing files: show daily data as unavailable or omit SMA200 until 200 valid sessions while minute replay continues.

### Success Criteria
- Users can switch repeatedly between minute and daily views without resetting playback, orders, positions, or each mode's viewport.
- Daily candles contain only completed prior sessions plus the selected day's replay-derived partial candle.
- SMA25 starts at the 25th valid daily close and SMA200 at the 200th; insufficient history yields no premature line points.
- Letter-bearing stems such as `285A` load official daily data across the legacy/current Code boundary.
- Stale daily responses, missing data, and daily-load failures never corrupt the minute chart or stop replay.
- Focused Python, Node, syntax, lint, and formatting checks pass; existing minute-history tests remain green.

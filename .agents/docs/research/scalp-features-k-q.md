# Scalp feature groups K-Q (index real-time, sector peers, disclosures, regime, large prints, previous close, calendar)

Status: implemented, caches built for the full 2024-01-04 .. 2026-08-19 universe (641 dates, 449 stems).
Code: `src/trading_rl/scalp_features_ext.py` (columns + compute), `src/trading_rl/scalp_ext_build.py` (one-time builders),
`scripts/build_scalp_ext_cache.py` (CLI), `scripts/ext_feature_report.py` (coverage / sanity report),
`tests/test_scalp_features_ext.py` (35 tests). Shared files touched minimally: `scalp_features.py` (+9 lines: `ADD_GROUPS`,
`EXTRA_NAMES.update(EXT_NAMES)`, `compute_extras(..., store=)` + one `elif`), `scalp_env.py` (1 line: pass `store=store`),
`feature_screen.py` (docstring only). Groups A-J and `base` are bit-identical (golden test passes; a test also asserts that the
first columns of `base+A+C+G+H+K+...` equal `base+A+C+G+H`).

All groups: only data with time <= decision time (prints) / strictly before the session date (daily) / strictly before the stock's
`T_open` (disclosures); NaN/inf -> 0; clipped; fixed width; `*_known` flags where data can be missing (flag 0 -> columns are 0).
Spec: `base+K`, `base+K+L+M+N+O+P+Q`, any mix with A-I and `-J` (`base+C+K-J` etc.); canonical order is alphabetical
(`base+A..I+K..Q-J`). App replay (`scalp_replay.simulate_scalp_trades`) needs no change: `compute_extras` reads the ext
caches through `default_store().cache_dir` (verified on a real-cache date with a K..Q checkpoint).

## Obs dims (market features / obs_dim incl. 10 agent-state dims)

| spec | market | obs_dim |
|---|---:|---:|
| base | 28 | 38 |
| base+K | 54 | 64 |
| base+L | 37 | 47 |
| base+M | 42 | 52 |
| base+N | 42 | 52 |
| base+O | 37 | 47 |
| base+P | 32 | 42 |
| base+Q | 39 | 49 |
| base+K+L+M+N+O+P+Q | 115 | 125 |

## Columns, sources, timing rules

Wall-clock grid: second `g` = seconds since 09:00:00; the stock's decision `s` (T_open + s) maps to `g = floor(T_open_off + s)`
(<= the true decision instant, same convention as C/D).

### K - index real time (26 columns)
Source: ETF tick files `S:/jp/stocks_trades/{1570,1321,1306}.duckdb` (same `stocks_board` schema as the stock files). Cache
`ext/etf/<date>.npz`: per-second last-print grid 09:00:00..11:30:00 (float32, forward-filled). A print at time t is put in grid
second **ceil(t)**, so `grid[g]` never contains a print later than its own instant (unit-tested on sub-second prints).
Columns: `k_known_{1570,1321,1306}` (grid has a print at g), `k_beta_known`; 1570: `ret5/15/30/60`, `since_open` (vs the ETF's
own first print), `gap` (first print / previous close), `vs_prev_close`; 1321 and 1306: `ret15/60`, `since_open`, `gap`,
`vs_prev_close` (all bps); `k_idx_ret_since_topen_bps` (1306 from the stock's T_open); `k_beta60`; residuals
`k_resid_ret15/60/since_open_bps` = stock return - beta * 1306 return over the **same window** (look-back or since the stock's
own T_open). Look-back base = max(g - lag, ETF first print). ETF previous close = last row of `ext/daily_adj.parquet` before the
date. Beta = OLS slope of the stock's split-adjusted daily returns on the **TOPIX index** daily returns
(`indices_bars_daily_topix_*`) over the 60 sessions strictly before the date (>= 30 pairs, returns winsorised at +-30 %,
`AdjFactor` applied), clipped to [-1, 4].
Caveat: ETF opens are not always 09:00:01. First-print second of 1570: 09:00:01 on 431/641 dates, 09:03:00 on 105, 09:06 on 31,
later on ~14 (multiples of 3 min = special-quote renewals); 1321: 568/641 at 09:00:01; 1306: 599/641. Before the first print the
ETF columns are 0 with known=0.

### L - sector peers (9 columns)
Cache `ext/peer/<date>.npz`: for each cached universe session of the date, `(P / p_open - 1) * 1e4` on the wall-clock grid
(same construction as the ctx cache), plus Sector33 (point-in-time `sector33.parquet`). Peers = other stems of that date's
universe with the same sector and a print by g. Columns: `l_known` (>= 1 peer), `l_log1p_n_peers`, `l_peer_ret15/60` (mean
look-back return; before a peer's open the base is its open), `l_peer_since_open`, `l_rel_ret15/60/since_open` (stock - peer
mean), `l_peer_frac_up`. Caveat: the cached ticks only cover [T_open, T_open+600 s] per stock, so a peer that opened much earlier
than the stock drops out once its own window has ended; and a sector with no other member in the top-50 that day is unknown.

### M - disclosures (14 columns)
Sources: TDnet index JSON `S:/tdnet/yanoshin_index/YYYYMMDD.json` (213,276 items, 620 trading days 2024-01-04 .. 2026-07-17,
minute-resolution `pubdate`), J-Quants `fins_summary_*` (92,952 rows with DiscDate/DiscTime) and `fins_earnings-date_*`
(SchDate). Window = **[previous trading day 15:00, T_open)**: `ts >= 15:00 prev day` and `ts < T_open` strictly (an item stamped
exactly at T_open is excluded; unit-tested). The start is fixed at 15:00 as specified even after the close moved to 15:30 on
2024-11-05 (most companies still release at 15:00); items released earlier in the previous session (e.g. a 13:55 earnings
release) are therefore *not* in the window.
Title keywords (TDnet): kessan `決算`, up `上方修正|増額修正|上振れ`, down `下方修正|減額修正|下振れ`, buyback
`自己株式|自社株|自己投資口`, dividend `配当|増配|減配|剰余金`, other = none of these. Columns: `m_covered`,
`m_log1p_n_disc`, `m_earn_disc` (TDnet 決算 or a fins financial-statement record), `m_up_rev`/`m_down_rev` (title keyword OR
sign of the fins forecast revision), `m_buyback`, `m_dividend`, `m_other`, `m_earn_due_today` / `m_earn_due_prev` (J-Quants
schedule `SchDate` == session date / previous trading day, only schedules whose `PubDate` < session date),
`m_rev_known`/`m_rev_pct` (latest forecast OP vs the previous forecast OP of the same fiscal year, from fins_summary, clipped
+-1), `m_surprise_known`/`m_op_surprise` (FY actual OP vs the latest earlier forecast OP of that fiscal year, clipped +-1).
`m_covered = 0` (and every other M column 0) when the session date is after 2026-07-17, or the TDnet file of the session day or
of the previous trading day is missing (e.g. 2024-01-04, whose previous day 2023-12-29 has no file).

### N - index daily regime (14 columns)
Sources: TOPIX index daily (`ext/topix_daily.parquet`), 1321 daily (`ext/daily_adj.parquet`, split-adjusted returns), the stock's
daily (same file). All as of the last session strictly before the date (stale > 10 calendar days -> unknown).
`n_known` (TOPIX and 1321 both usable); TOPIX `ret1/5/20_pct`, `vol20_pct` (std of 20 daily returns), `dist_ma25_pct`; the same
five for 1321 (`n_nk_*`, the Nikkei proxy); `n_beta_known`, `n_beta60`, `n_corr60` (stock vs TOPIX, prior 60 sessions).

### O - large prints (9 columns)
From the stock's own cached ticks. Large print = `qty >= max(2e-4 * avg_volume_20d, 500)` (about the top 5 % of prints; the
threshold is a function of prior-day data only, so it is causal and scale free). Tick-rule sign (zero tick keeps the previous
sign; the opening auction print(s) at `t_rel == 0` are excluded from all O columns). Windows (30 s, 60 s, since open) ending at
the decision: `o_large_share{30,60,_open}` (large volume / volume), `o_large_imb{30,60,_open}` ((buy - sell large volume) /
large volume, 0 with no large print), `o_log1p_nlarge60`, `o_log1p_nlarge_open`; `o_known` (= avg_volume_20d > 0).

### P - previous-day close flow (4 columns)
Source: J-Quants minute bars `equities_bars_minute_*` (2024-01 .. 2026-08-19) -> `ext/prevday.parquet` per (stem, date).
Last-30-min window = bars with time >= 15:00 (>= 14:30 before 2024-11-05) including the closing-auction bar. Columns:
`p_known`, `p_last30_ret_bps` (last bar close / first window bar open - 1), `p_close_vs_vwap_bps` (last close vs full-day
VWAP = sum(Va) / sum(Vo)), `p_last30_vol_share` (the closing auction is inside the window, median share ~0.27). Uses the
previous trading day from the trading calendar (days with >= 500 daily bars). Unknown for 2024-01-04 (previous day 2023-12-29
has no minute bars) and for stems without bars.

### Q - calendar (11 columns)
`q_dow_mon..fri` (one-hot), `q_sq_day` (2nd Friday; moves to the previous trading day when that Friday is a holiday),
`q_sq_major` (SQ day in months 3/6/9/12), `q_month_end` / `q_month_start` (last / first trading day of the month, from the
trading calendar), `q_after_gap2` (>= 2 non-trading days since the previous trading day: **every Monday** and after
holidays), `q_after_long` (>= 3 non-trading days: long weekends / Golden Week / New Year). Constant over the session;
no known flag (weekday rule is the fallback without a calendar).

## Cache files (all new, under `.cache/trading_rl/ext/`, 96 MB; parquet files are set read-only on purpose)
`daily_adj.parquet` (all 5-digit-code stems, 2023-09..2026-08, with AdjFactor), `topix_daily.parquet`, `prevday.parquet`,
`disclosures.parquet`, `earn_sched.parquet`, `tdnet_days.parquet`, `etf/<date>.npz` (641), `peer/<date>.npz` (641).
Rebuild: `uv run python scripts/build_scalp_ext_cache.py --steps daily,topix,disc,prevday,etf,peers` (read-only on S: and on
the existing caches; below-normal priority, 2 DuckDB threads). Remove the read-only attribute of a parquet before rebuilding it.
Build timings: daily 1 min, topix 5 s, disc ~1.5 min, prevday ~9 min (30 monthly minute CSVs), etf 1 min, peers 1.5 min.

## Coverage (share of VAL sessions, 2025-07-01..2025-12-31, 6,172 sessions) with the known flag = 1

"first" = at the first decision (T_open + 10 s), "last" = at the last decision (T_open + 599 s), "ever" = at any decision.

| group | flag | first | last | ever |
|---|---|---:|---:|---:|
| K | k_known_1570 | 0.806 | 0.996 | 0.996 |
| K | k_known_1321 | 0.974 | 1.000 | 1.000 |
| K | k_known_1306 | 0.994 | 1.000 | 1.000 |
| K | k_beta_known | 0.988 | 0.994 | 0.994 |
| L | l_known (>= 1 same-sector peer printing) | 0.835 | 0.787 | 0.854 |
| M | m_covered | 1.000 | 1.000 | 1.000 |
| M | m_rev_known (forecast revision in window) | 0.006 | 0.006 | 0.006 |
| M | m_surprise_known | 0.000 | 0.000 | 0.000 |
| M | any disclosure in window / earnings disclosure / earnings due today | 0.052 / 0.012 / 0.014 | | |
| N | n_known | 1.000 | 1.000 | 1.000 |
| N | n_beta_known | 0.994 | 0.994 | 0.994 |
| O | o_known | 1.000 | 1.000 | 1.000 |
| P | p_known | 1.000 | 1.000 | 1.000 |
| Q | (always defined) | - | - | - |

Notes: M is sparse by nature (the top-50 turnover names rarely have a disclosure between yesterday 15:00 and the open);
`m_surprise_known` is ~0.03 % (only FY statements released after 15:00). Test split: M `m_covered` = 1 for 132 of 153 test
dates (6,600 / 7,650 sessions); 2026-07-18..2026-08-19 have `m_covered = 0` (TDnet index ends 2026-07-17). L `last` < `first`
because peers' cached windows end 600 s after their own open.

## Sanity on TRAIN sessions only (2024-01-04..2025-06-30, every 4th date, 4,536 sessions; not for selection)

Per new column: share of non-zero values, range, and Pearson corr with the stock's next-60 s return at decision seconds 30 / 120 /
300 (corr rows in the JSON; all |corr| <= 0.08 -- nothing leaks, nothing looks like a target proxy). No column is dead: the only
zero-variance columns are the constant-1 flags `n_known` / `o_known` (flags are all 1 in train/val by construction; the PPO
normaliser maps std < 1e-8 to 1). Rare event columns (`m_down_rev` 0.1 %, `m_up_rev` 0.3 %, `m_rev_known` 0.8 %,
`m_surprise_known` 0.2 %, `m_dividend` 0.6 %, `m_earn_disc` 1.5 %, `q_month_end` / `q_after_long` 1.1 %) are alive but sparse.
Largest absolute correlations seen (all small): `k_1570_ret5_bps@300 +0.075`, `k_1306_ret15@300 +0.063`,
`k_1570_ret30@120 +0.069`, `n_topix_ret1_pct@30 +0.076`, `p_close_vs_vwap_bps@120 +0.066`, `q_dow_mon@120 +0.060`.

## Data gaps / caveats
* TDnet index ends 2026-07-17 (M covered = 0 afterwards); TDnet files before 2024-01-04 are sparse (not used).
* 1570 (and sometimes 1321/1306) open late on special-quote days: known flags are 0 until the first print (1570: 80.6 % known
  at +10 s, 99.6 % by +599 s).
* ETF/peer/TDnet/disclosure inputs are only wall-clock aligned to the stock's own T_open; peers' return grids only exist inside
  their own [T_open, T_open + 600 s] cache window.
* `daily_adj.parquet` has all stems from 2023-09-01, so beta needs >= 30 pairs within 60 prior sessions (unknown for new
  listings and the first weeks of 2024-01).
* Replay needs the ext caches (the app reads `default_store().cache_dir/ext`); a date without them silently gets zeros +
  known = 0 (same as training on such a date).
* Per-session cost: +13 ms for all of K..Q on top of ~14 ms for `base` (one-time ~2 s per process for loading daily / prevday /
  disclosure tables).

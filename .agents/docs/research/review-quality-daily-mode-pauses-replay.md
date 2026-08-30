# Quality Review: daily-mode-pauses-replay

## Summary

指定された5ファイルの worktree 対 HEAD 差分を確認した。Critical / High / Medium / Low の所見はいずれも 0 件で、日足切替時の再生継続を損なう品質上の問題は見つからなかった。

## Findings

None.

## Invariants Checked

- `setChartMode()` は `state.playing` を代入せず、`setPlaying()` も呼ばない。日足への切替は再生ループの状態を変えない。
- `runReplayFrame()` は約定ティックがないフレームでも時計、スクラバー、日足再生表示を更新する。
- `ReplayLivenessPresenter` は hidden、状態、秒単位時刻、進捗率が同一なら DOM 書き込みを省略する。
- 仮想時刻は既存時計と同じ UTC の `HH:MM:SS` で、進捗率は無効区間を 0、範囲外を 0〜100 に制限する。
- 分足では表示を hidden にし、非表示中の時刻変化だけでは追加書き込みを行わない。日足へ戻ると最新状態を再描画する。
- 既存の独立 viewport の capture/restore 経路は変更されておらず、日足の live edge を強制しない。
- SMA25/SMA200 の履歴系列と rolling window は `DailyChartSession.complete()` で一度だけ precompute され、再生フレームでは terminal point のみ導出する。

## Validation

- `node --test src/tickreplay/static/daily-chart.test.mjs`: 36 passed, 0 failed.
- `node --test src/tickreplay/static/*.test.mjs`: 111 passed, 0 failed.
- `node --check src/tickreplay/static/daily-chart.mjs`: passed.
- `node --check src/tickreplay/static/app.js`: passed.

## Codex Consultation

読み取り専用レビューを2回開始したが、Windows の標準入力を wrapper が UTF-8 ログへ保存する段階で `UnicodeEncodeError` となり、Codex 本体は実行されなかった。したがって Codex の回答はレビュー根拠に含めていない。

## Residual Risks

- 実ブラウザでの日足切替中の視覚更新、レスポンシブ配置、長時間再生はこの品質レビューでは未検証。Lead の Chrome 回帰確認で補完する。

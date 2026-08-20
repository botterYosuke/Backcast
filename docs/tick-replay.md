# 歩み値リプレイ

J-Quants のティックデータ（歩み値）を、その日の時間の流れのまま再生する
ローカル Web アプリ。ティックチャート・分足チャート・歩み値テープが同じ
仮想時刻で同時に進む。

![スクリーンショット](./tick-replay.png)

## 起動

```bash
uv sync
uv run python run.py
```

既定で <http://127.0.0.1:8765/> を開く。オプション:

| フラグ | 既定 | 意味 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 待ち受けアドレス |
| `--port` | `8765` | 待ち受けポート |
| `--no-browser` | off | ブラウザを自動で開かない |
| `--reload` | off | 開発用オートリロード |

## データ

データの場所はハードコードせず、`BACKCAST_JQUANTS_DUCKDB_ROOT`（環境変数 →
リポジトリ直下 `.env` の順）から解決する。使うのは
`${BACKCAST_JQUANTS_DUCKDB_ROOT}/stocks_trades/<銘柄>.duckdb` の
`stocks_board` テーブルのみで、DB は常に read-only で開く。

分足は **歩み値から逐次集計**している（`stocks_minute` は読まない）。
そのため再生中の「形成中の足」がティックの進行に合わせて正しく伸縮する。

データ側の性質に対応済みの点:

- 1 ファイルに複数の `Code` が混ざることがあるため（例: `3823.duckdb` は
  `38230` 50.8 万行と `3823` 2 行）、`stocks_board_metadata` の
  `record_count` が最大の行を正銘柄とみなす。ファイル名からは決めない。
- `*_Conflict.duckdb`（同期の衝突ファイル）は銘柄一覧から除外する。
- `Timestamp` は文字列なので `TRY_CAST` し、変換できない行と価格 NULL の行は捨てる。
- 同一マイクロ秒の約定は捨てずに 1 マイクロ秒ずつ後ろへずらす
  （Lightweight Charts が単調増加の時間軸を要求するため）。
- `Type`（`'1'` / `'2'` など）に売買の意味づけはしていない。テープには
  生の値をそのまま出し、色は前約定比の up/down で塗る。
- 保存時刻はタイムゾーンなしの naive 値。これを UTC とみなして epoch 化し、
  Lightweight Charts に UTC のまま描かせるので、**保存された壁時計時刻が
  そのまま軸に出る**。タイムゾーンの読み替えは行っていない。

## 操作

| 操作 | 効果 |
| --- | --- |
| 銘柄 / 日付 → 読み込む | その日のセッションを読み込む。データが無い日は直近の過去営業日に寄せる |
| 前日 / 翌日 | データのある前後の営業日へ移動 |
| ▶ / ⏸（Space キー） | 再生・一時停止 |
| ⏮ | 寄り付き前に巻き戻す |
| x1〜x500 | 再生速度（x1 が実時間） |
| 空白時間をスキップ | 5 秒以上約定が無い区間を飛ばす（昼休みなど） |
| スクラバー | 任意の時刻へシーク。状態を作り直すので前後どちらへでも飛べる |

## チャートの配色

国内証券のリアルタイムチャートに合わせている。

- 始値 ≦ 最新値 → **陽線（赤）**
- 始値 > 最新値 → **陰線（青）**

形成中の足は始値が固定され、高値・安値・終値が約定のたびに伸縮し、
上の規則で色が反転する。バーごとに色を明示指定しているので、
ライブラリ既定の up/down 判定には依存しない。

## 構成

```
run.py                         起動スクリプト（uvicorn）
src/tickreplay/
  config.py                    BACKCAST_JQUANTS_DUCKDB_ROOT の解決
  repository.py                DuckDB への read-only アクセス
  server.py                    FastAPI（API + 静的配信）
  static/
    index.html / styles.css    UI（itayomikun.com 参考のライトテーマ）
    app.js                     再生エンジン・チャート・テープ
    vendor/                    lightweight-charts v4.2.0（同梱・CDN 不要）
```

再生はすべてブラウザ内で行う。サーバは 1 セッション分の約定を列指向 JSON
（`us` / `price` / `qty` / `type`）で一括返すだけなので、シークも速度変更も
サーバ往復なしで即座に効く。転送は gzip 圧縮している。

実測（この環境）: 7203 の 1 日 = 20,162 約定で約 0.8 秒 / 650 KB、
9984 の 1 日 = 104,054 約定で約 1.8 秒。

## API

| エンドポイント | 内容 |
| --- | --- |
| `GET /api/status` | データ root の解決結果と銘柄数 |
| `GET /api/symbols?q=&limit=` | 銘柄ファイル一覧（前方一致） |
| `GET /api/symbols/{stem}` | 正銘柄コードとデータ期間 |
| `GET /api/session?stem=&date=&direction=` | 1 日分の約定（列指向）。`direction` は `0`=その日のみ / `-1`=過去方向 / `1`=未来方向 |

## テスト

```bash
uv run --extra dev pytest tests/test_tickreplay_config.py tests/test_tickreplay_repository.py tests/test_tickreplay_server.py
```

本番データには触れず、同じ構造の一時 DuckDB を作って検証する。

# Tick Replay AI Agent — 実装計画

## 概要

歩み値リプレイ (`http://127.0.0.1:8765/`) 上で自律的に売買判断を行う
強化学習ベースの AI トレーディングエージェントを構築する。

### 前回提案のレビューと修正

前回のセッション（2026-11-01）で提示した AlphaGo 型アーキテクチャには
以下の**設計上の問題**があり、修正した：

| 前回の提案 | 問題点 | 本計画での修正 |
| :--- | :--- | :--- |
| AlphaGo 式 MCTS 探索 | 金融市場は確率的・部分観測で探索木がノイズで爆発する。対局相手のいない環境では「先読み」の定義が不明確 | MCTS は不採用。Actor-Critic (PPO) を基本とし、マルチモーダル行動が必要になった段階で Diffusion Policy へ拡張 |
| Mamba バックボーン推奨 | 研究は有望だが実装が複雑で、初期段階の検証コストが高い | フェーズ 1 は 1D-CNN + GRU。フェーズ 3 で Mamba / Transformer を比較検証 |
| Gym 環境 + PyTorch 一体提案 | 環境・モデル・UI 統合が巨大な一枚岩になる | 4 層分離: ① DuckDB → NumPy 変換層、② Gymnasium 環境、③ モデル＆学習、④ UI 統合 |
| 「JAX 超並列シミュレータ」推奨 | JAX-LOB は板のキュー再現に特化しており、本アプリのデータ（板情報なし・歩み値のみ）には過剰 | 純 NumPy ベクトル環境で十分。板は合成（アプリ同様ランダム演出） |
| 4 ステップの粒度が粗い | 各ステップの完了条件・テスト基準・判断ゲートが無い | 6 フェーズに細分化し、各フェーズに完了条件と Go/No-Go 判定を設置 |

### ゴール

1. **学習可能なバックテスト環境**: 1 日数万ティックの歩み値データ上で
   数百万ステップ/時の学習速度を持つ Gymnasium 互換環境。
2. **強化学習エージェント**: PPO ベースの Actor-Critic が、ランダム行動
   および Buy & Hold を統計的有意に上回る。
3. **リアルタイム推論 UI**: 学習済みモデルがブラウザ上のリプレイ画面で
   自動売買し、売買マーカーと損益がリアルタイムに表示される。

---

## 環境分析

### データ構造（`SessionTicks`）

サーバーの `/api/session` が返す 1 日分の列指向データ：

```
SessionTicks:
  stem: str          # 銘柄コード (例: "7203")
  code: str          # canonical code (例: "72030")
  date: str          # "YYYY-MM-DD"
  us: list[int]      # マイクロ秒タイムスタンプ (厳密単調増加)
  price: list[float] # 約定値
  qty: list[int]     # 約定数量
  trade_type: list[str] # 約定区分 ("1" 等)
```

- ソース: `src/tickreplay/repository.py` L152-173
  DuckDB の `stocks_trades` テーブルから
  `SELECT epoch_us(Timestamp), Price, Qty, Type ... ORDER BY Timestamp`
- 銘柄 7203 (トヨタ) の 2024-01-04: **19,770 ティック**、
  価格帯 2,600–2,605 円、総データ期間 2024-01-04 〜 2026-08-19
  （約 1,400 万レコード）

### 取引ルール（`paper-trading.mjs`）

```
ポジション制約: ネット 1 本のみ (両建て禁止)
  - qty > 0: 買い建玉
  - qty < 0: 売り建玉
  - qty == 0: ノーポジション

約定方式:
  - 成行 (Market): 現在の約定値で即時約定
  - 指値 (Limit): price を指定して板に配置
    → 以降のティックで isFilled(order, tradePrice) が true になれば約定
    → 売り指値: tradePrice >= order.price
    → 買い指値: tradePrice <= order.price
    → 約定値は指値どおり (不利な滑りなし)

損益計算:
  - 実現損益: (決済値 - 平均取得単価) × 決済数量 × sign(建玉方向)
  - 評価損益: (現在値 - 平均取得単価) × 保有数量
  - 合計損益: 実現損益 + 評価損益
```

- ソース: `src/tickreplay/static/paper-trading.mjs` L1-85
- ドテン対応: 反対方向の注文が保有を超えた場合、超過分は新規建玉

### 既存の戦略実行基盤

- `src/tickreplay/static/strategy/` に `StrategyRunner` の Python ABI
  ラッパーが存在（pycache のみ、ソースなし）。
  コマンドタイプ: `submit_market`, `submit_limit`, `cancel_strategy_orders`
- `python/engine/strategy_runtime/` にバックテスター・ポートフォリオ・
  ポジションサイジング等のモジュールが存在（同様に pycache のみ）。
- ブラウザ向け Playwright テストの設定が存在
  （`tests/browser/application-strategy.mjs` の参照あり）。

> [!NOTE]
> 既存の StrategyRunner ABI は Pyodide (ブラウザ内 Python) 向けの
> サンドボックスで、`handle_json` でリクエストを受けてコマンドを返す
> プロトコル。フェーズ 5 の UI 統合ではこの ABI に準拠するか、
> サーバーサイド WebSocket を新設するかを選択する。

---

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 0: Data Pipeline                                      │
│   DuckDB (stocks_trades/*.duckdb)                           │
│     → SessionLoader (Python)                                │
│       → NumPy 構造化配列 (us, price, qty, type)             │
│         → 正規化・特徴量変換 → FeatureBuffer                │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│ Layer 1: Gymnasium Environment (TickReplayEnv)              │
│                                                             │
│   observation_space:                                        │
│     market:  Box(shape=(window, n_features))                │
│       - price_return (対数収益率)                            │
│       - log_volume (対数出来高)                              │
│       - vwap_deviation (VWAP 乖離率)                        │
│       - buy_sell_imbalance (買い売り約定比率)                │
│       - time_of_day (セッション内経過割合)                   │
│       - volatility (直近ウィンドウの実現ボラティリティ)     │
│     agent: Box(shape=(n_agent_features,))                   │
│       - position (正規化保有数)                              │
│       - unrealized_pnl_pct (含み損益率)                     │
│       - time_remaining (残りセッション割合)                  │
│       - has_pending_order (指値注文有無)                     │
│                                                             │
│   action_space: Discrete(5)                                 │
│     0: Hold (何もしない)                                     │
│     1: Market Buy (成行買い, 固定数量)                       │
│     2: Market Sell (成行売り, 固定数量)                      │
│     3: Close Position (全建玉決済)                           │
│     4: (将来拡張: 指値注文)                                  │
│                                                             │
│   step_mode: "tick" (1 step = 1 tick) or                    │
│              "interval" (1 step = N ticks, 集約判断)        │
│                                                             │
│   reward: Δ(合計損益) + ペナルティ項                         │
│     - 合計損益の差分 (実現 + 評価)                           │
│     - 過剰取引ペナルティ: -λ per trade                      │
│     - 大引け建玉ペナルティ: -μ × |position|                 │
│     - ドローダウンペナルティ: -ν × max(0, peak_pnl - pnl)   │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│ Layer 2: Model & Training                                   │
│                                                             │
│   Phase 1: 1D-CNN + GRU → Policy Head + Value Head          │
│     encoder:                                                │
│       Conv1d(n_features, 32, k=3) → ReLU → Conv1d(32, 64)  │
│       → GRU(64, 128, 2 layers)                              │
│     agent_state: Linear(n_agent_features, 32)               │
│     fused: Concat(gru_out[-1], agent_state) → Linear(192)   │
│     policy: Linear(192, |A|) → Softmax                      │
│     value:  Linear(192, 1)                                  │
│                                                             │
│   Phase 3 (optional upgrade):                               │
│     Mamba-1D or causal Transformer backbone                 │
│                                                             │
│   Algorithm: PPO (Proximal Policy Optimization)             │
│     - CleanRL or Stable-Baselines3                          │
│     - Vectorized envs (SubprocVecEnv × 8-16)               │
│     - 報酬正規化、GAE(λ=0.95, γ=0.999)                     │
│     - エントロピーボーナス (探索促進)                         │
│     - 勾配クリッピング                                       │
└──────────────────────┬──────────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────────┐
│ Layer 3: Serving & UI Integration                           │
│                                                             │
│   Option A: サーバーサイド推論                                │
│     POST /api/strategy/action                                │
│       request:  { observation }                              │
│       response: { action, value_estimate, action_probs }     │
│                                                             │
│   Option B: StrategyRunner ABI 準拠 (Pyodide / ONNX.js)    │
│     ブラウザ内で ONNX Runtime Web が推論                     │
│                                                             │
│   UI: 「AI 自動売買」トグルボタン                             │
│     → 各フレームで observation 構築 → 推論 → placeOrder()    │
│     → チャートに売買マーカー + 損益パネル更新                │
└─────────────────────────────────────────────────────────────┘
```

---

## フェーズ別実装計画

### フェーズ 1: データパイプライン & Gymnasium 環境

**目的**: DuckDB → NumPy → Gymnasium 互換環境を構築し、ランダムエージェント
で動作確認する。

**成果物**:

| ファイル | 内容 |
| :--- | :--- |
| `src/ai/data/session_loader.py` | DuckDB からセッションデータを NumPy 配列としてロード。キャッシュ対応 |
| `src/ai/data/feature_builder.py` | 価格系列 → 特徴量変換 (対数収益率, VWAP, ボラティリティ等) |
| `src/ai/envs/tick_replay_env.py` | `gymnasium.Env` 実装。`reset()` でランダムな銘柄・日付を選択 |
| `src/ai/envs/portfolio.py` | `paper-trading.mjs` と同一ロジックの Python 実装 |
| `tests/test_ai_portfolio.py` | JS のテストケースと同一の入力で Python 実装の一致を検証 |
| `tests/test_ai_env.py` | 環境の smoke test (reset/step/render) |

**技術的決定事項**:

1. **ステップ粒度**: 初期は **1 step = 1 tick** とする。高速再生での
   学習効率が悪い場合、「1 step = 直近 N ticks を集約して判断」モードを
   追加する（`step_mode="interval"` パラメータ）。
2. **観測ウィンドウ**: 直近 **128 ティック** の特徴量行列。
   セッション開始直後はゼロパディング。
3. **行動空間**: 初期は **Discrete(4)**: Hold / Market Buy /
   Market Sell / Close。指値はフェーズ 4 以降。
4. **報酬設計**: 合計損益の差分 + 取引コストペナルティ
   (`-0.5 × tick_size per trade`)。
5. **エピソード**: 1 エピソード = 1 銘柄 × 1 営業日。
   大引け時に建玉が残っていれば強制決済＋ペナルティ。

**依存パッケージ** (新規追加):
```
gymnasium>=1.0
numpy>=2.0
torch>=2.4
```

**完了条件**:
- [ ] `TickReplayEnv` が `gymnasium.utils.env_checker.check_env()` を通過
- [ ] ランダムエージェントで 100 エピソード完走（クラッシュなし）
- [ ] `portfolio.py` のテストが `paper-trading.test.mjs` の全ケースと一致
- [ ] 1 エピソード (2 万ティック) の step 所要時間 < 0.5 秒

**Go/No-Go**: 環境が安定動作しなければ後続フェーズに進まない。

---

### フェーズ 2: ベースラインエージェント（ルールベース & 教師あり）

**目的**: 学習前に「何が良い取引か」の上界と下界を確立する。

**成果物**:

| ファイル | 内容 |
| :--- | :--- |
| `src/ai/baselines/random_agent.py` | ランダム行動。下界の設定 |
| `src/ai/baselines/buy_and_hold.py` | 寄り付き買い → 大引け売り |
| `src/ai/baselines/mean_reversion.py` | 短期移動平均乖離での逆張り |
| `src/ai/baselines/oracle_agent.py` | 未来のデータを覗くチート（上界の設定） |
| `src/ai/eval/backtest_runner.py` | 複数エージェントを複数日分一括評価 |
| `src/ai/eval/metrics.py` | 合計損益, シャープレシオ, 最大ドローダウン, 取引回数 |

**評価指標**:

| 指標 | 定義 | 用途 |
| :--- | :--- | :--- |
| Total PnL | セッション終了時の合計損益 (円) | 主目標 |
| Sharpe Ratio | mean(episode_returns) / std(episode_returns) | リスク調整済み性能 |
| Max Drawdown | 最大の含み損ピーク-トラフ幅 (円) | リスク制約 |
| Trade Count | 1 セッションの売買回数 | 過剰取引の監視 |
| Win Rate | 利益が出たトレードの割合 | 診断用 |

**完了条件**:
- [ ] 5 つのベースライン全てが 100 エピソード評価を完走
- [ ] Oracle が Random を明確に上回る（PnL 分布の差が統計的有意）
- [ ] 評価レポートが自動生成される

**Go/No-Go**: Oracle と Random の差が小さい場合、データ品質・報酬設計・
行動空間に問題がある。フェーズ 1 に戻って見直す。

---

### フェーズ 3: 強化学習エージェント（PPO）

**目的**: CNN-GRU Policy-Value ネットワークを PPO で学習し、
ルールベースのベースラインを上回る。

**成果物**:

| ファイル | 内容 |
| :--- | :--- |
| `src/ai/models/cnn_gru_actor_critic.py` | 1D-CNN + GRU → Policy Head + Value Head (PyTorch) |
| `src/ai/training/ppo_trainer.py` | PPO 学習ループ (CleanRL ベース) |
| `src/ai/training/config.py` | ハイパーパラメータ定義 (dataclass) |
| `src/ai/training/train.py` | エントリポイント (`python -m ai.training.train`) |
| `src/ai/training/wandb_logger.py` | 学習曲線の記録 (オプション) |

**ハイパーパラメータ初期値**:

```python
@dataclass
class PPOConfig:
    # Environment
    observation_window: int = 128
    step_mode: str = "tick"          # "tick" | "interval"
    interval_ticks: int = 50         # step_mode="interval" 時のみ
    order_quantity: int = 100        # 固定注文数量 (単元株)

    # Reward shaping
    trade_cost_penalty: float = 0.5  # 呼値幅 × この係数
    eod_position_penalty: float = 2.0
    drawdown_penalty: float = 0.01

    # PPO
    learning_rate: float = 3e-4
    gamma: float = 0.999             # 長いエピソード向け
    gae_lambda: float = 0.95
    clip_ratio: float = 0.2
    entropy_coeff: float = 0.01
    value_coeff: float = 0.5
    max_grad_norm: float = 0.5
    epochs_per_update: int = 4
    minibatch_size: int = 256
    rollout_steps: int = 2048

    # Training
    total_timesteps: int = 10_000_000
    num_envs: int = 8                # 並列環境数
    eval_interval: int = 50_000
    save_interval: int = 200_000
    seed: int = 42
```

**学習戦略**:

1. **カリキュラム学習**: 最初は大きな値動きのある日（ボラティリティ上位）
   から学習し、徐々に全日に拡張する。
2. **報酬正規化**: RunningMeanStd でオンライン正規化。
3. **アクション・マスキング**: ノーポジの時は Close を無効化、
   買い建玉がある時は Market Buy を無効化（ネットで増やさない制約）。

**完了条件**:
- [ ] 学習曲線が収束し、平均エピソード報酬が単調増加後に安定
- [ ] 評価セット（学習に使わなかった日付）で Mean Reversion ベースラインを
      合計 PnL で上回る
- [ ] 過剰取引していない (1 セッション平均 < 50 回)
- [ ] 最大ドローダウンが Oracle の 3 倍以内

**Go/No-Go**: 学習が収束しない場合の対処:
1. `step_mode` を `interval` (50 ticks ≈ 数秒) に変更
2. 報酬スケーリング調整
3. 行動空間を Discrete(3) に縮小 (Close を除外)
4. 学習データを 1 銘柄に限定してオーバーフィット確認

---

### フェーズ 4: モデル改良 & 行動空間拡張

**目的**: ネットワークアーキテクチャの改善と指値注文の追加。

**成果物**:

| ファイル | 内容 |
| :--- | :--- |
| `src/ai/models/mamba_actor_critic.py` | Mamba (S6) バックボーン版 |
| `src/ai/models/transformer_actor_critic.py` | Causal Transformer 版 |
| `src/ai/envs/tick_replay_env_v2.py` | 拡張行動空間 (指値 ±1〜5 呼値) |
| `src/ai/training/model_comparison.py` | A/B 比較スクリプト |

**行動空間拡張**:

```
Discrete(4) → MultiDiscrete([3, 11])
  axis 0: direction = {Hold, Buy, Sell}
  axis 1: order_type = {Market, Limit-1tick, ..., Limit-5tick} (direction≠Hold 時のみ)
  + Close は Hold + 明示的 Close フラグ (あるいは position が 0 でない時の Sell/Buy で自動判定)
```

**完了条件**:
- [ ] 3 アーキテクチャ (CNN-GRU / Mamba / Transformer) の評価比較表
- [ ] 最良モデルがフェーズ 3 の結果を再現または改善
- [ ] 指値注文モードで取引コストが削減される（平均スリッページ低下）

---

### フェーズ 5: UI 統合（リアルタイム推論）

**目的**: 学習済みモデルをリプレイ画面上でリアルタイムに動作させる。

**成果物**:

| ファイル | 内容 |
| :--- | :--- |
| `src/tickreplay/ai_endpoint.py` | FastAPI `/api/ai/action` エンドポイント |
| `src/tickreplay/static/ai-agent.mjs` | ブラウザ側の AI 制御モジュール |
| `src/tickreplay/static/app.js` (変更) | 「AI 自動売買」トグル UI の追加 |
| `src/tickreplay/static/index.html` (変更) | AI 関連 UI 要素 |

**UI 設計**:

```
┌─ トップバー ──────────────────────────────────────────────┐
│ [銘柄] [日付] [前日][翌日] [読込]  [🤖 AI 自動売買: OFF]  │
│                                     ↑ トグルボタン        │
│                                     ON にすると再生中に     │
│                                     AI が自動注文          │
└───────────────────────────────────────────────────────────┘
```

- AI の行動をチャートのマーカーに表示（人間の手動注文と色分け）
- AI の確信度（Policy の最大確率）をツールチップに表示
- Value Head の推定期待損益をサブチャートに折れ線表示（オプション）

**推論方式の選択**:

| 方式 | 利点 | 欠点 |
| :--- | :--- | :--- |
| サーバーサイド (FastAPI + PyTorch) | GPU 利用可、モデル更新が容易 | ネットワーク往復遅延 |
| ブラウザ内 (ONNX Runtime Web) | ゼロ遅延、サーバー負荷なし | モデル変換が必要、GPU 使えない場合あり |

→ **初期はサーバーサイド**。遅延が問題にならない再生速度 (x1–x10) では十分。

**完了条件**:
- [ ] AI トグル ON でリプレイ再生すると自動売買が動作
- [ ] 売買マーカーが正しくチャートに表示される
- [ ] AI ON/OFF でリプレイエンジン (step, seek, reset) が壊れない
- [ ] 手動注文と AI 注文が衝突しない

---

### フェーズ 6: 評価・改善サイクル

**目的**: 複数銘柄・期間でのロバスト性検証と継続的改善。

**成果物**:

| ファイル | 内容 |
| :--- | :--- |
| `src/ai/eval/cross_validation.py` | 時系列 Walk-Forward 検証 |
| `src/ai/eval/report_generator.py` | 評価レポート (Markdown) 自動生成 |
| `notebooks/ai_analysis.ipynb` | 探索的分析ノートブック (オプション) |

**検証項目**:
- 学習セット外の銘柄での汎化性能
- 学習セット外の期間 (2026 年データ) での性能
- 高ボラ日 vs 低ボラ日でのロバスト性
- 取引頻度と PnL の Pareto 最適フロンティア

---

## ディレクトリ構造

```
src/ai/
├── data/
│   ├── session_loader.py       # DuckDB → NumPy
│   └── feature_builder.py      # 特徴量エンジニアリング
├── envs/
│   ├── tick_replay_env.py      # Gymnasium 環境
│   ├── tick_replay_env_v2.py   # 拡張行動空間版
│   └── portfolio.py            # paper-trading.mjs の Python 移植
├── models/
│   ├── cnn_gru_actor_critic.py # Phase 3
│   ├── mamba_actor_critic.py   # Phase 4
│   └── transformer_actor_critic.py  # Phase 4
├── baselines/
│   ├── random_agent.py
│   ├── buy_and_hold.py
│   ├── mean_reversion.py
│   └── oracle_agent.py
├── training/
│   ├── config.py
│   ├── ppo_trainer.py
│   ├── train.py                # python -m ai.training.train
│   └── wandb_logger.py
└── eval/
    ├── backtest_runner.py
    ├── metrics.py
    ├── cross_validation.py
    └── report_generator.py

tests/
├── test_ai_portfolio.py
├── test_ai_env.py
├── test_ai_feature_builder.py
└── test_ai_model.py
```

## 依存関係管理

`pyproject.toml` に `[project.optional-dependencies]` として追加し、
既存の tickreplay ランタイムには影響を与えない：

```toml
[project.optional-dependencies]
ai = [
    "gymnasium>=1.0",
    "numpy>=2.0",
    "torch>=2.4",
    "stable-baselines3>=2.4",   # or cleanrl
]
```

`uv sync --extra ai` で AI 依存のみ追加インストール。

---

## リスクと対策

| リスク | 影響 | 対策 |
| :--- | :--- | :--- |
| 報酬設計の失敗（過剰取引、学習不安定） | フェーズ 3 が収束しない | フェーズ 2 の Oracle/Random 差で報酬設計を事前検証。報酬 ablation 実験 |
| ティック粒度が細かすぎて学習非効率 | 数千万ステップでも改善しない | `step_mode="interval"` で 10–100 ティック単位に集約 |
| リアリティギャップ（シミュレータと実市場の乖離） | AI が非現実的な戦略を学習 | 指値の約定は保守的（last price ではなく bid/ask を考慮）。板情報は合成であると明示 |
| データ量不足（1 銘柄では汎化しない） | オーバーフィット | 複数銘柄でのクロスバリデーション (フェーズ 6) |
| 既存 UI への副作用 | リグレッション | AI 機能は独立モジュール化し、トグル OFF で既存動作に影響なし |
| PyTorch 等の依存が重い | インストール・ビルド時間増大 | optional-dependencies で分離。CI は `--extra ai` を別ジョブ |

---

## 優先順位と推定工数

| フェーズ | 優先度 | 推定工数 | 依存 |
| :--- | :--- | :--- | :--- |
| 1: データ & Env | **必須** | 2–3 日 | なし |
| 2: ベースライン | **必須** | 1–2 日 | フェーズ 1 |
| 3: PPO 学習 | **必須** | 3–5 日 | フェーズ 2 |
| 4: モデル改良 | 推奨 | 3–5 日 | フェーズ 3 |
| 5: UI 統合 | 推奨 | 2–3 日 | フェーズ 3 |
| 6: 評価サイクル | 推奨 | 2–3 日 | フェーズ 3 |

> [!IMPORTANT]
> フェーズ 1–3 が MVP。フェーズ 1 の環境品質がすべての土台となるため、
> ここで十分なテストと検証を行うことが最も重要。

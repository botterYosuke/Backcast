# P3 開場スキャルピング v2 結果 (order_mode=market): 売買代金上位50銘柄 × 寄り付き10分

- 生成: 2026-11-07 04:49 / `scripts/train_scalp.py` (実測値のみ)

## 1. 設定
- 環境 `trading_rl.scalp_env.ScalpEnv`: 1銘柄×1日=1エピソード。判断は T_open+10s から1秒ごと (590回)、T_open+600s に全キャンセル+成行で強制決済して終了。
- 建玉: -1/0/+1 単位 (1単位=100株)。指値は買い・売り各1本まで。フラットは両建て注文可、ロング中は売り指値のみ、ショート中は買い指値のみ (違反は no-op として計数)。
- order_mode = **market**, 行動 (Discrete 4): 0=keep, 1=market_buy, 2=market_sell, 3=market_flatten。bid_kN = last-N呼値、ask_kN = last+N呼値 (その時点の価格帯の呼値)。market_buy = flat->long / short->flat、market_sell = flat->short / long->flat (last ± 0.5呼値、全成行は全指値をキャンセル)。
- 合法手マスク (action_mask): ロング中は買い指値/成行買い不可、ショート中は売り側不可、注文なしで cancel 不可、フラットで flatten 不可。PPO は学習・評価・再生ともマスク付き (非合法手は確率0、log-prob/エントロピー/argmax もマスク後)。マスク時の無効アクション数 invalid/sess は判断時点の違反のみ。判断後レイテンシ中に指値が約定して無効化した分は別カウント (n_latency_noops)。
- 約定ルール (主): **trade_through** = 買い指値 P は P より安い約定が出たとき、売り指値 P はP より高い約定が出たときに全量 P で約定 (P ちょうどの約定では約定しない)。参考: touch = P ちょうどでも約定。待ち行列順位は未モデル、marketable 指値の即時約定も未モデル。
- レイテンシ 50 ms: 全アクション (発注・訂正・取消・成行) は判断時刻+50ms に有効化。新規注文は有効化時刻より厳密に後の約定のみで約定。
- 成行: last ± 0.5 呼値 (買いは+、売りは-)。手数料 0.0 円/約定 (パラメータ)。窓外 (T_open+10s 前・T_open+600s 後) の約定はテストで禁止を確認。
- 報酬 = 1ステップの含み損益 (最終約定値で時価評価) の差分を、セッション基準呼値 (寄り付き価格の呼値) で割った値。ペナルティなしなら合計は最終損益 (呼値) に厳密に一致。PPO 学習時のみ割引リターンの走行標準偏差で報酬を割る (スケールのみ)。
- **学習報酬 (シェイピング) と生損益の区別**: 学習時のみ 在庫ペナルティ 0.0025 呼値/秒 × |建玉| × 1秒、アイドルペナルティ 0.0 呼値/秒 (フラットかつ注文なし; 取引自体を目的化するため非推奨、比較用)。このレポートの全ての PnL (学習曲線の val/train 列、結果表) は **生損益** (ペナルティ控除前)。ペナルティ込みの学習報酬は学習曲線の「train shaped」列のみ。検証・テストはペナルティなし・固定約定ルール。
- 観測 (38次元): gate_features の市場特徴量 28 個 (因果的) + 自己状態 10 個 (建玉, 含み損益[呼値], 買い注文の有無/last からの距離, 売り注文の有無/距離, エントリーからの秒/60, 各注文発注からの秒/60, 残り時間割合)。正規化は ActorCritic の obs_mean/obs_std に学習セッションで fit。
- 日次PnL = その日の全セッション (≤50銘柄) の PnL 合計。CI は日単位ブートストラップ (2000回)。yen/sess は1セッション平均。PF・勝率はラウンドトリップ単位。max DD は日次累積 PnL (円/100株) の最大ドローダウン。

## 2. データ
| split | 期間 | 既知銘柄 sessions | 学習外銘柄 sessions | 備考 |
|---|---|---|---|---|
| train | 2024-01-04 .. 2025-06-30 | 15,016 | 0 | 学習外銘柄は除外 |
| val | 2025-07-01 .. 2025-12-31 | 4,936 | 1,236 |  |
| test | 2026-01-01 .. 2026-08-19 | 5,955 | 1,616 |  |

`ok` 状態のキャッシュ済みセッションのみ。学習外銘柄 = md5(stem)[0] % 5 == 0 (約20%)。
除外セッション (`.cache\trading_rl\human\yaman_exclude_sessions.csv`, 行動模倣の教師が売買した日・銘柄、リーク防止): train 0, val 0, test 37 件を上表の件数から除外済み。

## 3. 学習
- PPO: hidden 128, lr 0.0003 (線形減衰), gamma 0.997, GAE λ 0.95, clip 0.2, entropy 0.03 -> 0.005 (線形), value_coef 0.5, 4 epochs, minibatch 1024, 32 envs × 256 steps/iter, seed 0。gamma=0.997 は約590ステップのエピソードに合わせた (有効ホライズン約330秒)。
- 約定ルール・カリキュラム (学習のみ): 新エピソードごとに trade_through の確率が 0.0 から 1.0 へ最初の 50% のステップで線形に上昇 (それ以外は touch)。 検証/テストは常に trade_through (主) と touch (参考)。
- 行動模倣ウォームスタート: csv:.cache/trading_rl/human/yaman_trades.csv / 2000 steps / 21 sessions / 12,382 samples, CE 0.511 → 0.310, argmax一致率 99.4%, label smoothing 0.1, critic あり, 教師 `{'csv': '.cache/trading_rl/human/yaman_trades.csv'}`。
- 実行: 10,002,432 env steps, 1221 iterations, 学習壁時計 20.2 分, **スループット 10,989 steps/s** (更新込み・評価除く、4 CPU スレッド、マスクあり; 評価込み 8,245 steps/s)。
- チェックポイント選択: 検証 (既知銘柄の固定 1000 セッション, trade_through, 決定論的) のセッション平均 yen/100sh 最大。選択 = 7,503,872 steps、保存先 `models\ppo_scalp_top50_open10m_market_v2_yaman.pt`。

### 学習曲線 (検証は固定サブセット・決定論的)
| steps | wall min | train ep ticks (raw) | train shaped | entropy | ent coef | p(trade_through) | val yen/sess (raw) | val ticks/sess | val trips/sess | val orders/sess | val invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 0.2 | +nan | +nan | nan | 0.0300 | 0.00 | +0.0 | +0.00 | 0.00 | 0.0 | 0.00 |
| 507,904 | 1.3 | -8.5 | -8.7 | 0.040 | 0.0288 | 0.10 | +15.2 | +0.12 | 0.01 | 0.0 | 0.00 |
| 1,007,616 | 2.3 | -1.3 | -1.3 | 0.011 | 0.0275 | 0.20 | -36.6 | -0.22 | 0.22 | 0.2 | 0.00 |
| 1,507,328 | 3.3 | -0.2 | -0.2 | 0.003 | 0.0263 | 0.30 | +0.0 | +0.00 | 0.00 | 0.0 | 0.00 |
| 2,007,040 | 4.2 | +0.2 | +0.2 | 0.001 | 0.0250 | 0.40 | +0.0 | +0.00 | 0.00 | 0.0 | 0.00 |
| 2,506,752 | 5.2 | -0.3 | -0.5 | 0.012 | 0.0238 | 0.50 | -8.1 | -0.59 | 0.74 | 0.7 | 0.00 |
| 3,006,464 | 6.2 | +1.0 | +0.6 | 0.014 | 0.0225 | 0.60 | -182.4 | -1.00 | 1.24 | 1.2 | 0.00 |
| 3,506,176 | 7.2 | -0.7 | -1.2 | 0.019 | 0.0213 | 0.70 | -290.5 | -1.11 | 1.28 | 1.3 | 0.00 |
| 4,005,888 | 8.2 | +0.5 | +0.2 | 0.012 | 0.0200 | 0.80 | -363.1 | -1.16 | 0.64 | 0.6 | 0.00 |
| 4,505,600 | 9.3 | +0.7 | +0.2 | 0.011 | 0.0188 | 0.90 | -175.7 | -1.21 | 1.06 | 1.1 | 0.00 |
| 5,005,312 | 10.3 | +0.9 | +0.5 | 0.025 | 0.0175 | 1.00 | -357.9 | -1.34 | 1.59 | 1.6 | 0.00 |
| 5,505,024 | 11.3 | -1.7 | -1.8 | 0.013 | 0.0163 | 1.00 | -91.8 | -0.51 | 1.13 | 1.1 | 0.00 |
| 6,004,736 | 12.2 | -0.4 | -0.4 | 0.006 | 0.0150 | 1.00 | -7.6 | -0.11 | 0.37 | 0.4 | 0.00 |
| 6,504,448 | 13.2 | +0.5 | +0.4 | 0.005 | 0.0138 | 1.00 | -18.0 | -0.28 | 0.54 | 0.5 | 0.00 |
| 7,004,160 | 14.3 | +0.6 | +0.5 | 0.013 | 0.0125 | 1.00 | -116.0 | -0.64 | 1.31 | 1.3 | 0.00 |
| 7,503,872 | 15.3 | -1.2 | -1.3 | 0.003 | 0.0113 | 1.00 | +80.5 | +0.24 | 0.46 | 0.5 | 0.00 |
| 8,003,584 | 16.3 | +0.5 | +0.4 | 0.003 | 0.0100 | 1.00 | +26.9 | +0.21 | 0.29 | 0.3 | 0.00 |
| 8,503,296 | 17.2 | -0.1 | -0.1 | 0.002 | 0.0088 | 1.00 | -85.7 | -0.31 | 0.45 | 0.4 | 0.00 |
| 9,003,008 | 18.2 | +0.3 | +0.2 | 0.002 | 0.0075 | 1.00 | -51.5 | -0.07 | 0.35 | 0.4 | 0.00 |
| 9,502,720 | 19.2 | +1.0 | +0.9 | 0.003 | 0.0063 | 1.00 | -85.3 | -0.21 | 0.40 | 0.4 | 0.00 |
| 10,002,432 | 20.2 | +2.5 | +2.4 | 0.003 | 0.0050 | 1.00 | -115.8 | -0.43 | 0.48 | 0.5 | 0.00 |

## 4. RuleScalper のチューニング (検証・既知銘柄・trade_through)
- グリッド 36 通り (mode, k_entry, tp, sl, timeout, requote)。第1段 300 セッションで全探索、上位 10 を 1500 セッションで再評価して凍結。第1段で yen/sess>0 の設定: 0/36、最良 -3712.8、中央値 -11055.6。
| chosen | params | stage1 yen/sess | stage2 yen/sess | stage2 ticks/sess | trips/sess |
|---|---|---|---|---|---|
| * | `{'mode': 'mm', 'k_entry': 0, 'tp': 3, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -4104.1 | -3553.4 | -10.98 | 13.02 |
|  | `{'mode': 'long', 'k_entry': 0, 'tp': 3, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -3712.8 | -3599.0 | -11.12 | 12.96 |
|  | `{'mode': 'long', 'k_entry': 0, 'tp': 2, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -4215.8 | -4263.3 | -13.37 | 15.25 |
|  | `{'mode': 'mm', 'k_entry': 0, 'tp': 2, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -4612.2 | -4360.9 | -13.75 | 15.37 |
|  | `{'mode': 'mm', 'k_entry': 0, 'tp': 1, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -5137.0 | -5108.4 | -15.92 | 19.95 |
|  | `{'mode': 'long', 'k_entry': 0, 'tp': 1, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -5304.6 | -5392.5 | -16.89 | 19.48 |
|  | `{'mode': 'long', 'k_entry': 0, 'tp': 3, 'sl': 'inf', 'timeout_s': 30, 'requote_s': 5, 'no_entry_last_s': 30}` | -7917.5 | -8002.0 | -23.93 | 25.85 |
|  | `{'mode': 'mm', 'k_entry': 0, 'tp': 3, 'sl': 6.0, 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -7356.2 | -8086.8 | -30.06 | 31.04 |
|  | `{'mode': 'long', 'k_entry': 0, 'tp': 3, 'sl': 6.0, 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -7698.7 | -8639.2 | -30.40 | 30.93 |
|  | `{'mode': 'mm', 'k_entry': 0, 'tp': 2, 'sl': 'inf', 'timeout_s': 30, 'requote_s': 5, 'no_entry_last_s': 30}` | -7848.5 | -8690.6 | -25.94 | 29.41 |

## 5. 結果
### 検証 (既知銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 4936/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| DoNothing | touch | 4936/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| Random | trade_through | 4936/124 | -2,115,652 [-2,155,683, -2,074,275] | -5,925.8 [-5,974.9, -5,879.8] | -53148.5 | 5909 | 148.44 | 12.8 | 0.19 | 262,340,815 | 0/732684 | 0/730206/2478 | 0.0% | 2.0 | 146.07 |
| Random | touch | 4936/124 | -2,115,652 [-2,155,683, -2,074,275] | -5,925.8 [-5,974.9, -5,879.8] | -53148.5 | 5909 | 148.44 | 12.8 | 0.19 | 262,340,815 | 0/732684 | 0/730206/2478 | 0.0% | 2.0 | 146.07 |
| RuleScalper(tuned) | trade_through | 4936/124 | -156,712 [-175,107, -137,766] | -470.7 [-520.9, -419.7] | -3936.8 | 507 | 12.74 | 65.5 | 0.71 | 19,432,265 | 0/62880 | 0/60055/2825 | 0.0% | 44.8 | 0.00 |
| RuleScalper(tuned) | touch | 4936/124 | -156,712 [-175,107, -137,766] | -470.7 [-520.9, -419.7] | -3936.8 | 507 | 12.74 | 65.5 | 0.71 | 19,432,265 | 0/62880 | 0/60055/2825 | 0.0% | 44.8 | 0.00 |
| PPO | trade_through | 4936/124 | -1,236 [-8,661, 5,526] | 9.4 [-7.4, 24.3] | -31.0 | 16 | 0.41 | 80.3 | 0.95 | 494,490 | 0/2012 | 0/1896/116 | 0.0% | 53.3 | 0.00 |
| PPO | touch | 4936/124 | -1,236 [-8,661, 5,526] | 9.4 [-7.4, 24.3] | -31.0 | 16 | 0.41 | 80.3 | 0.95 | 494,490 | 0/2012 | 0/1896/116 | 0.0% | 53.3 | 0.00 |

### 検証 (学習外銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 1236/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| DoNothing | touch | 1236/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| Random | trade_through | 1236/124 | -736,473 [-764,989, -711,393] | -1,483.2 [-1,519.7, -1,446.6] | -73885.7 | 1482 | 148.66 | 9.6 | 0.14 | 91,322,680 | 0/183747 | 0/183081/666 | 0.0% | 2.0 | 145.85 |
| Random | touch | 1236/124 | -736,473 [-764,989, -711,393] | -1,483.2 [-1,519.7, -1,446.6] | -73885.7 | 1482 | 148.66 | 9.6 | 0.14 | 91,322,680 | 0/183747 | 0/183081/666 | 0.0% | 2.0 | 145.85 |
| RuleScalper(tuned) | trade_through | 1236/124 | -53,696 [-61,706, -45,711] | -103.3 [-117.2, -90.5] | -5387.0 | 111 | 11.14 | 60.3 | 0.65 | 6,658,310 | 0/13774 | 0/13047/727 | 0.0% | 51.4 | 0.00 |
| RuleScalper(tuned) | touch | 1236/124 | -53,696 [-61,706, -45,711] | -103.3 [-117.2, -90.5] | -5387.0 | 111 | 11.14 | 60.3 | 0.65 | 6,658,310 | 0/13774 | 0/13047/727 | 0.0% | 51.4 | 0.00 |
| PPO | trade_through | 1236/124 | 1,192 [-58, 2,560] | 3.8 [0.8, 7.0] | +119.6 | 2 | 0.17 | 79.8 | 1.87 | 55,800 | 0/213 | 0/204/9 | 0.0% | 57.9 | 0.00 |
| PPO | touch | 1236/124 | 1,192 [-58, 2,560] | 3.8 [0.8, 7.0] | +119.6 | 2 | 0.17 | 79.8 | 1.87 | 55,800 | 0/213 | 0/204/9 | 0.0% | 57.9 | 0.00 |

### テスト (既知銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 5955/153 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| DoNothing | touch | 5955/153 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| Random | trade_through | 5955/153 | -3,287,903 [-3,437,732, -3,155,170] | -5,842.5 [-5,897.5, -5,785.1] | -84475.1 | 5782 | 148.55 | 17.5 | 0.25 | 503,049,170 | 0/884626 | 0/881498/3128 | 0.0% | 2.0 | 146.33 |
| Random | touch | 5955/153 | -3,287,903 [-3,437,732, -3,155,170] | -5,842.5 [-5,897.5, -5,785.1] | -84475.1 | 5782 | 148.55 | 17.5 | 0.25 | 503,049,170 | 0/884626 | 0/881498/3128 | 0.0% | 2.0 | 146.33 |
| RuleScalper(tuned) | trade_through | 5955/153 | -241,792 [-278,880, -206,489] | -549.8 [-621.2, -482.6] | -6212.3 | 598 | 15.37 | 72.5 | 0.77 | 36,994,200 | 0/91546 | 0/88281/3265 | 0.0% | 36.9 | 0.00 |
| RuleScalper(tuned) | touch | 5955/153 | -241,792 [-278,880, -206,489] | -549.8 [-621.2, -482.6] | -6212.3 | 598 | 15.37 | 72.5 | 0.77 | 36,994,200 | 0/91546 | 0/88281/3265 | 0.0% | 36.9 | 0.00 |
| PPO | trade_through | 5955/153 | -3,787 [-18,482, 9,995] | -7.8 [-37.2, 17.9] | -97.3 | 39 | 0.99 | 82.2 | 0.95 | 1,536,125 | 0/5914 | 0/5533/381 | 0.0% | 56.0 | 0.00 |
| PPO | touch | 5955/153 | -3,787 [-18,482, 9,995] | -7.8 [-37.2, 17.9] | -97.3 | 39 | 0.99 | 82.2 | 0.95 | 1,536,125 | 0/5914 | 0/5533/381 | 0.0% | 56.0 | 0.00 |

### テスト (学習外銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 1616/153 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| DoNothing | touch | 1616/153 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| Random | trade_through | 1616/153 | -1,189,986 [-1,296,310, -1,082,991] | -1,605.6 [-1,652.3, -1,562.7] | -112665.8 | 1574 | 149.00 | 19.5 | 0.21 | 182,067,890 | 0/240784 | 0/239824/960 | 0.0% | 2.0 | 146.56 |
| Random | touch | 1616/153 | -1,189,986 [-1,296,310, -1,082,991] | -1,605.6 [-1,652.3, -1,562.7] | -112665.8 | 1574 | 149.00 | 19.5 | 0.21 | 182,067,890 | 0/240784 | 0/239824/960 | 0.0% | 2.0 | 146.56 |
| RuleScalper(tuned) | trade_through | 1616/153 | -108,540 [-132,569, -84,896] | -175.6 [-207.9, -143.8] | -10276.4 | 174 | 16.51 | 74.5 | 0.69 | 16,742,515 | 0/26679 | 0/25789/890 | 0.0% | 34.3 | 0.00 |
| RuleScalper(tuned) | touch | 1616/153 | -108,540 [-132,569, -84,896] | -175.6 [-207.9, -143.8] | -10276.4 | 174 | 16.51 | 74.5 | 0.69 | 16,742,515 | 0/26679 | 0/25789/890 | 0.0% | 34.3 | 0.00 |
| PPO | trade_through | 1616/153 | 2,798 [-2,949, 8,462] | 8.3 [-8.3, 25.1] | +264.9 | 15 | 1.46 | 81.0 | 1.16 | 446,300 | 0/2361 | 0/2241/120 | 0.0% | 45.9 | 0.00 |
| PPO | touch | 1616/153 | 2,798 [-2,949, 8,462] | 8.3 [-8.3, 25.1] | +264.9 | 15 | 1.46 | 81.0 | 1.16 | 446,300 | 0/2361 | 0/2241/120 | 0.0% | 45.9 | 0.00 |

### trade_through と touch の差 (touch - trade_through, 日次 yen/100sh 平均)
| group | DoNothing | Random | RuleScalper(tuned) | PPO |
|---|---|---|---|---|
| 検証 (既知銘柄) | +0 | +0 | +0 | +0 |
| 検証 (学習外銘柄) | +0 | +0 | +0 | +0 |
| テスト (既知銘柄) | +0 | +0 | +0 | +0 |
| テスト (学習外銘柄) | +0 | +0 | +0 | +0 |

## 6. PPO の挙動 (テスト・既知銘柄・trade_through)
- 1セッションあたり: 発注 1.0 回, ラウンドトリップ 0.99, 平均保有 56.0 秒, 取引ゼロのセッション 65.5%, 無効アクション 0.00 (レイテンシ中の失効 0.00)。
| action | share of decisions |
|---|---|
| keep | 99.67% |
| market_buy | 0.17% |
| market_sell | 0.00% |
| market_flatten | 0.16% |

## 7. 自動所見 (事実のみ)
- [val_seen] PPO makes 0.41 round trips per session: it essentially does not trade.
- [val_seen] PPO enters mostly with market orders.
- [val_seen] PPO exits mostly by market/forced close (limit-exit share 0%).
- [val_seen] PPO plays 'keep' on 99.9% of decisions.
- [val_seen] PPO trade-through daily PnL is not distinguishable from 0 (-1,236 [-8,661, 5,526] yen/100sh per day).
- [val_seen] PPO touch-minus-trade-through gap: +0 yen/100sh per day.
- [val_held] PPO makes 0.17 round trips per session: it essentially does not trade.
- [val_held] PPO enters mostly with market orders.
- [val_held] PPO exits mostly by market/forced close (limit-exit share 0%).
- [val_held] PPO plays 'keep' on 99.9% of decisions.
- [val_held] PPO trade-through daily PnL is not distinguishable from 0 (1,192 [-58, 2,560] yen/100sh per day).
- [val_held] PPO touch-minus-trade-through gap: +0 yen/100sh per day.
- [test_seen] PPO enters mostly with market orders.
- [test_seen] PPO exits mostly by market/forced close (limit-exit share 0%).
- [test_seen] PPO plays 'keep' on 99.7% of decisions.
- [test_seen] PPO trade-through daily PnL is not distinguishable from 0 (-3,787 [-18,482, 9,995] yen/100sh per day).
- [test_seen] PPO touch-minus-trade-through gap: +0 yen/100sh per day.
- [test_held] PPO enters mostly with market orders.
- [test_held] PPO exits mostly by market/forced close (limit-exit share 0%).
- [test_held] PPO plays 'keep' on 99.5% of decisions.
- [test_held] PPO trade-through daily PnL is not distinguishable from 0 (2,798 [-2,949, 8,462] yen/100sh per day).
- [test_held] PPO touch-minus-trade-through gap: +0 yen/100sh per day.

## 8. 再現コマンド
`uv run python scripts/train_scalp.py --order-mode market --train-steps 10000000 --inventory-penalty 0.0025 --fill-curriculum 0.0,0.5 --ent-start 0.03 --ent-end 0.005 --bc-from csv:.cache/trading_rl/human/yaman_trades.csv --bc-steps 2000 --bc-critic --exclude-sessions .cache/trading_rl/human/yaman_exclude_sessions.csv --reuse-baselines .agents/docs/research/rl-top50-scalp-v2-baselines.json --model models/ppo_scalp_top50_open10m_market_v2_yaman.pt --report .agents/docs/research/rl-top50-scalp-v2-market-yaman.md`

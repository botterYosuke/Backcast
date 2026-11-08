# P3 開場スキャルピング v2 結果 (order_mode=market): 売買代金上位50銘柄 × 寄り付き10分

- 生成: 2026-11-07 21:30 / `scripts/train_scalp.py` (実測値のみ)

## 1. 設定
- 環境 `trading_rl.scalp_env.ScalpEnv`: 1銘柄×1日=1エピソード。判断は T_open+10s から1秒ごと (590回)、T_open+600s に全キャンセル+成行で強制決済して終了。
- 建玉: -1/0/+1 単位 (1単位=100株)。指値は買い・売り各1本まで。フラットは両建て注文可、ロング中は売り指値のみ、ショート中は買い指値のみ (違反は no-op として計数)。
- order_mode = **market**, 行動 (Discrete 4): 0=keep, 1=market_buy, 2=market_sell, 3=market_flatten。bid_kN = last-N呼値、ask_kN = last+N呼値 (その時点の価格帯の呼値)。market_buy = flat->long / short->flat、market_sell = flat->short / long->flat (last ± 0.5呼値、全成行は全指値をキャンセル)。
- 合法手マスク (action_mask): ロング中は買い指値/成行買い不可、ショート中は売り側不可、注文なしで cancel 不可、フラットで flatten 不可。PPO は学習・評価・再生ともマスク付き (非合法手は確率0、log-prob/エントロピー/argmax もマスク後)。マスク時の無効アクション数 invalid/sess は判断時点の違反のみ。判断後レイテンシ中に指値が約定して無効化した分は別カウント (n_latency_noops)。
- 約定ルール (主): **trade_through** = 買い指値 P は P より安い約定が出たとき、売り指値 P はP より高い約定が出たときに全量 P で約定 (P ちょうどの約定では約定しない)。参考: touch = P ちょうどでも約定。待ち行列順位は未モデル、marketable 指値の即時約定も未モデル。
- レイテンシ 50 ms: 全アクション (発注・訂正・取消・成行) は判断時刻+50ms に有効化。新規注文は有効化時刻より厳密に後の約定のみで約定。
- 成行: last ± 0.5 呼値 (買いは+、売りは-)。手数料 0.0 円/約定 (パラメータ)。窓外 (T_open+10s 前・T_open+600s 後) の約定はテストで禁止を確認。
- 報酬 = 1ステップの含み損益 (最終約定値で時価評価) の差分を、セッション基準呼値 (寄り付き価格の呼値) で割った値。ペナルティなしなら合計は最終損益 (呼値) に厳密に一致。PPO 学習時のみ割引リターンの走行標準偏差で報酬を割る (スケールのみ)。
- **学習報酬 (シェイピング) と生損益の区別**: 学習時のみ 在庫ペナルティ 0.0 呼値/秒 × |建玉| × 1秒、アイドルペナルティ 0.0 呼値/秒 (フラットかつ注文なし; 取引自体を目的化するため非推奨、比較用)、行動ベース (建玉・注文の状態は見ない): keep 判断ごとの罰 0.0 呼値、実際に効いた keep 以外の行動ごとの報酬 0.25 呼値。報酬の土台 reward_mode=realized (mtm = 含み損益込みの毎秒の損益変化 / realized = 決済で確定した損益のみ、決済した秒に支払い)。含み損ペナルティ 0.01 × 含み損(呼値) / 秒 (建玉中のみ、含み益には報酬なし)。このレポートの全ての PnL (学習曲線の val/train 列、結果表) は **生損益** (ペナルティ控除前)。ペナルティ込みの学習報酬は学習曲線の「train shaped」列のみ。検証・テストはペナルティなし・固定約定ルール。
- 観測 (55次元): 特徴量スペック **`base+C+D+G+H`** = 市場特徴量 45 個 (gate_features 28個から -J で削った残り + 追加グループ A-I、全て因果的) + 自己状態 10 個 (建玉, 含み損益[呼値], 買い注文の有無/last からの距離, 売り注文の有無/距離, エントリーからの秒/60, 各注文発注からの秒/60, 残り時間割合)。正規化は ActorCritic の obs_mean/obs_std に学習セッションで fit (学習セッションのみ。検証/テストの統計は使わない)。**`--no-test`: テスト分割は一切読み込まず、評価もしていない (スクリーニング用)。**
- 日次PnL = その日の全セッション (≤50銘柄) の PnL 合計。CI は日単位ブートストラップ (2000回)。yen/sess は1セッション平均。PF・勝率はラウンドトリップ単位。max DD は日次累積 PnL (円/100株) の最大ドローダウン。

## 2. データ
| split | 期間 | 既知銘柄 sessions | 学習外銘柄 sessions | 備考 |
|---|---|---|---|---|
| train | 2024-01-04 .. 2025-06-30 | 15,016 | 0 | 学習外銘柄は除外 |
| val | 2025-07-01 .. 2025-12-31 | 4,936 | 1,236 |  |

`ok` 状態のキャッシュ済みセッションのみ。学習外銘柄 = md5(stem)[0] % 5 == 0 (約20%)。

## 3. 学習
- PPO: hidden 128, lr 0.0003 (線形減衰), gamma 1.0, GAE λ 1.0, clip 0.2, entropy 0.03 -> 0.005 (線形), value_coef 0.5, 4 epochs, minibatch 1024, 32 envs × 256 steps/iter, seed 1。gamma=0.997 は約590ステップのエピソードに合わせた (有効ホライズン約330秒)。
- 約定ルール・カリキュラム (学習のみ): 新エピソードごとに trade_through の確率が 0.0 から 1.0 へ最初の 50% のステップで線形に上昇 (それ以外は touch)。 検証/テストは常に trade_through (主) と touch (参考)。
- 行動模倣ウォームスタート: rule / 2000 steps / 300 sessions / 177,000 samples, CE 0.703 → 0.373, argmax一致率 98.3%, label smoothing 0.1, critic あり, 教師 `{'mode': 'mm', 'k_entry': 0, 'tp': 3, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}`。
- 実行: 10,002,432 env steps, 1221 iterations, 学習壁時計 21.9 分, **スループット 9,941 steps/s** (更新込み・評価除く、4 CPU スレッド、マスクあり; 評価込み 7,619 steps/s)。
- チェックポイント選択: 検証 (既知銘柄の固定 1000 セッション, trade_through, 決定論的) のセッション平均 yen/100sh 最大。選択 = 6,004,736 steps、保存先 `C:\Users\sasai\Documents\Backcast\models\screen\s2s1_base_C_D_G_H.pt`。

### 学習曲線 (検証は固定サブセット・決定論的)
| steps | wall min | train ep ticks (raw) | train shaped | entropy | ent coef | p(trade_through) | val yen/sess (raw) | val ticks/sess | val trips/sess | val orders/sess | val invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 0.3 | +nan | +nan | nan | 0.0300 | 0.00 | -2145.7 | -6.73 | 6.37 | 6.4 | 0.00 |
| 507,904 | 1.4 | -161.6 | -84.3 | 0.938 | 0.0288 | 0.10 | -6424.7 | -20.27 | 20.99 | 26.9 | 0.00 |
| 1,007,616 | 2.5 | -170.1 | -88.1 | 0.959 | 0.0275 | 0.20 | -2741.9 | -8.68 | 11.43 | 16.0 | 0.00 |
| 1,507,328 | 3.7 | -153.9 | -78.6 | 0.949 | 0.0263 | 0.30 | -5043.7 | -12.21 | 13.47 | 14.2 | 0.00 |
| 2,007,040 | 4.8 | -141.9 | -74.3 | 0.985 | 0.0250 | 0.40 | -1293.8 | -5.47 | 7.00 | 8.0 | 0.00 |
| 2,506,752 | 5.9 | -142.1 | -71.7 | 0.997 | 0.0238 | 0.50 | -3733.0 | -10.65 | 13.83 | 18.9 | 0.00 |
| 3,006,464 | 7.0 | -127.2 | -66.4 | 0.899 | 0.0225 | 0.60 | -1073.3 | -2.84 | 3.04 | 4.5 | 0.00 |
| 3,506,176 | 8.1 | -109.5 | -57.3 | 0.900 | 0.0213 | 0.70 | -1355.8 | -3.30 | 1.96 | 2.0 | 0.00 |
| 4,005,888 | 9.2 | -124.5 | -64.2 | 0.856 | 0.0200 | 0.80 | -1787.3 | -4.40 | 3.21 | 3.6 | 0.00 |
| 4,505,600 | 10.3 | -93.7 | -51.3 | 0.770 | 0.0188 | 0.90 | -736.5 | -1.47 | 0.86 | 0.9 | 0.00 |
| 5,005,312 | 11.4 | -109.1 | -58.3 | 0.868 | 0.0175 | 1.00 | -562.5 | -2.25 | 1.94 | 2.3 | 0.00 |
| 5,505,024 | 12.5 | -124.2 | -63.0 | 0.829 | 0.0163 | 1.00 | -721.6 | -3.02 | 2.21 | 2.7 | 0.00 |
| 6,004,736 | 13.6 | -95.2 | -50.5 | 0.805 | 0.0150 | 1.00 | -40.1 | -1.01 | 0.91 | 0.9 | 0.00 |
| 6,504,448 | 14.7 | -103.2 | -53.2 | 0.905 | 0.0138 | 1.00 | -863.5 | -2.18 | 2.49 | 2.7 | 0.00 |
| 7,004,160 | 15.7 | -94.1 | -48.9 | 0.741 | 0.0125 | 1.00 | -426.1 | -1.15 | 1.05 | 1.2 | 0.00 |
| 7,503,872 | 16.8 | -87.9 | -46.3 | 0.760 | 0.0113 | 1.00 | -383.6 | -1.32 | 1.73 | 1.9 | 0.00 |
| 8,003,584 | 17.8 | -47.0 | -25.8 | 0.462 | 0.0100 | 1.00 | -105.0 | -0.66 | 0.50 | 0.6 | 0.00 |
| 8,503,296 | 18.9 | -34.7 | -19.1 | 0.408 | 0.0088 | 1.00 | -339.6 | -1.40 | 0.54 | 0.6 | 0.00 |
| 9,003,008 | 19.9 | -21.8 | -12.4 | 0.241 | 0.0075 | 1.00 | -101.4 | -0.64 | 0.29 | 0.3 | 0.00 |
| 9,502,720 | 20.9 | -13.9 | -7.0 | 0.184 | 0.0063 | 1.00 | -268.9 | -0.77 | 0.38 | 0.4 | 0.00 |
| 10,002,432 | 21.9 | -11.8 | -6.3 | 0.158 | 0.0050 | 1.00 | -159.8 | -0.55 | 0.27 | 0.3 | 0.00 |

## 4. RuleScalper のチューニング (検証・既知銘柄・trade_through)
- ベースライン (チューニング・評価) はキャッシュ `C:\Users\sasai\Documents\Backcast\.agents\docs\research\screen\s2s1_baselines.json` から再利用 (同一 order_mode/設定)。
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

### Momentum のチューニング (検証・既知銘柄・trade_through)
- グリッド 81 通り (lookback 30/60/120s × 閾値 0/1/2 呼値 × 保有 60/180/590s × 最早エントリー 10/30/60s)。第1段 300 セッション全探索、上位 10 を 1500 セッションで再評価して凍結。第1段で yen/sess>0: 0/81、最良 -1130.1、中央値 -2287.8。
| chosen | params | stage1 yen/sess | stage2 yen/sess | stage2 ticks/sess | trips/sess |
|---|---|---|---|---|---|
| * | `{'lookback_s': 30, 'thr_ticks': 0.0, 'hold_s': 590, 'entry_s': 60}` | -1134.0 | -46.8 | -1.33 | 1.00 |
|  | `{'lookback_s': 30, 'thr_ticks': 2.0, 'hold_s': 590, 'entry_s': 60}` | -1264.0 | -137.7 | -1.90 | 1.00 |
|  | `{'lookback_s': 30, 'thr_ticks': 1.0, 'hold_s': 590, 'entry_s': 60}` | -1130.1 | -229.6 | -1.66 | 1.00 |
|  | `{'lookback_s': 30, 'thr_ticks': 2.0, 'hold_s': 590, 'entry_s': 10}` | -1856.4 | -243.9 | -0.90 | 1.00 |
|  | `{'lookback_s': 60, 'thr_ticks': 2.0, 'hold_s': 590, 'entry_s': 60}` | -1864.6 | -493.8 | -1.06 | 1.00 |
|  | `{'lookback_s': 60, 'thr_ticks': 0.0, 'hold_s': 590, 'entry_s': 60}` | -1871.6 | -510.6 | -0.70 | 1.00 |
|  | `{'lookback_s': 60, 'thr_ticks': 1.0, 'hold_s': 590, 'entry_s': 60}` | -1829.1 | -525.0 | -1.05 | 1.00 |
|  | `{'lookback_s': 30, 'thr_ticks': 2.0, 'hold_s': 180, 'entry_s': 60}` | -1563.0 | -733.2 | -2.78 | 2.92 |
|  | `{'lookback_s': 120, 'thr_ticks': 2.0, 'hold_s': 180, 'entry_s': 10}` | -1764.3 | -1116.6 | -3.74 | 3.87 |
|  | `{'lookback_s': 120, 'thr_ticks': 0.0, 'hold_s': 180, 'entry_s': 30}` | -1633.7 | -1914.0 | -5.08 | 4.00 |

## 5. 結果
### 検証 (既知銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | gross ticks/trip | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 4936/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| DoNothing | touch | 4936/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| Random | trade_through | 4936/124 | -2,115,652 [-2,155,683, -2,074,275] | -5,925.8 [-5,974.9, -5,879.8] | -53148.5 | 5909 | 148.44 | 12.8 | 0.19 | 262,340,815 | 0/732684 | 0/730206/2478 | 0.0% | 2.0 | 0.003 | 146.07 |
| Random | touch | 4936/124 | -2,115,652 [-2,155,683, -2,074,275] | -5,925.8 [-5,974.9, -5,879.8] | -53148.5 | 5909 | 148.44 | 12.8 | 0.19 | 262,340,815 | 0/732684 | 0/730206/2478 | 0.0% | 2.0 | 0.003 | 146.07 |
| RuleScalper(tuned) | trade_through | 4936/124 | -156,712 [-175,107, -137,766] | -470.7 [-520.9, -419.7] | -3936.8 | 507 | 12.74 | 65.5 | 0.71 | 19,432,265 | 0/62880 | 0/60055/2825 | 0.0% | 44.8 | 0.079 | 0.00 |
| RuleScalper(tuned) | touch | 4936/124 | -156,712 [-175,107, -137,766] | -470.7 [-520.9, -419.7] | -3936.8 | 507 | 12.74 | 65.5 | 0.71 | 19,432,265 | 0/62880 | 0/60055/2825 | 0.0% | 44.8 | 0.079 | 0.00 |
| Momentum(tuned) | trade_through | 4936/124 | -17,624 [-34,380, -107] | -39.0 [-79.5, 2.2] | -442.7 | 40 | 1.00 | 46.6 | 0.87 | 2,719,880 | 0/4935 | 0/0/4935 | 0.0% | 539.8 | 0.025 | 0.00 |
| Momentum(tuned) | touch | 4936/124 | -17,624 [-34,380, -107] | -39.0 [-79.5, 2.2] | -442.7 | 40 | 1.00 | 46.6 | 0.87 | 2,719,880 | 0/4935 | 0/0/4935 | 0.0% | 539.8 | 0.025 | 0.00 |
| PPO | trade_through | 4936/124 | 2,058 [-4,437, 8,076] | -2.1 [-24.6, 18.1] | +51.7 | 37 | 0.92 | 61.4 | 1.04 | 312,790 | 0/4564 | 0/4291/273 | 0.0% | 52.7 | 0.946 | 0.00 |
| PPO | touch | 4936/124 | 2,058 [-4,437, 8,076] | -2.1 [-24.6, 18.1] | +51.7 | 37 | 0.92 | 61.4 | 1.04 | 312,790 | 0/4564 | 0/4291/273 | 0.0% | 52.7 | 0.946 | 0.00 |

### 検証 (学習外銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | gross ticks/trip | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 1236/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| DoNothing | touch | 1236/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| Random | trade_through | 1236/124 | -736,473 [-764,989, -711,393] | -1,483.2 [-1,519.7, -1,446.6] | -73885.7 | 1482 | 148.66 | 9.6 | 0.14 | 91,322,680 | 0/183747 | 0/183081/666 | 0.0% | 2.0 | 0.001 | 145.85 |
| Random | touch | 1236/124 | -736,473 [-764,989, -711,393] | -1,483.2 [-1,519.7, -1,446.6] | -73885.7 | 1482 | 148.66 | 9.6 | 0.14 | 91,322,680 | 0/183747 | 0/183081/666 | 0.0% | 2.0 | 0.001 | 145.85 |
| RuleScalper(tuned) | trade_through | 1236/124 | -53,696 [-61,706, -45,711] | -103.3 [-117.2, -90.5] | -5387.0 | 111 | 11.14 | 60.3 | 0.65 | 6,658,310 | 0/13774 | 0/13047/727 | 0.0% | 51.4 | 0.072 | 0.00 |
| RuleScalper(tuned) | touch | 1236/124 | -53,696 [-61,706, -45,711] | -103.3 [-117.2, -90.5] | -5387.0 | 111 | 11.14 | 60.3 | 0.65 | 6,658,310 | 0/13774 | 0/13047/727 | 0.0% | 51.4 | 0.072 | 0.00 |
| Momentum(tuned) | trade_through | 1236/124 | -9,542 [-18,090, -2,033] | -16.1 [-28.7, -3.1] | -957.3 | 10 | 1.00 | 46.2 | 0.77 | 1,375,650 | 0/1235 | 0/0/1235 | 0.0% | 539.9 | -0.619 | 0.00 |
| Momentum(tuned) | touch | 1236/124 | -9,542 [-18,090, -2,033] | -16.1 [-28.7, -3.1] | -957.3 | 10 | 1.00 | 46.2 | 0.77 | 1,375,650 | 0/1235 | 0/0/1235 | 0.0% | 539.9 | -0.619 | 0.00 |
| PPO | trade_through | 1236/124 | 72 [-3,301, 3,197] | 1.5 [-3.8, 6.4] | +7.2 | 5 | 0.49 | 60.3 | 1.01 | 227,460 | 0/602 | 0/555/47 | 0.0% | 61.6 | 1.323 | 0.00 |
| PPO | touch | 1236/124 | 72 [-3,301, 3,197] | 1.5 [-3.8, 6.4] | +7.2 | 5 | 0.49 | 60.3 | 1.01 | 227,460 | 0/602 | 0/555/47 | 0.0% | 61.6 | 1.323 | 0.00 |

### trade_through と touch の差 (touch - trade_through, 日次 yen/100sh 平均)
| group | DoNothing | Random | RuleScalper(tuned) | Momentum(tuned) | PPO |
|---|---|---|---|---|---|
| 検証 (既知銘柄) | +0 | +0 | +0 | +0 | +0 |
| 検証 (学習外銘柄) | +0 | +0 | +0 | +0 | +0 |

## 6. PPO の挙動 (検証・既知銘柄・trade_through)
- 1セッションあたり: 発注 0.9 回, ラウンドトリップ 0.92, 平均保有 52.7 秒, 取引ゼロのセッション 70.7%, 無効アクション 0.00 (レイテンシ中の失効 0.00)。
| action | share of decisions |
|---|---|
| keep | 99.70% |
| market_buy | 0.13% |
| market_sell | 0.03% |
| market_flatten | 0.14% |

## 7. 自動所見 (事実のみ)
- [val_seen] PPO enters mostly with market orders.
- [val_seen] PPO exits mostly by market/forced close (limit-exit share 0%).
- [val_seen] PPO plays 'keep' on 99.7% of decisions.
- [val_seen] PPO trade-through daily PnL is not distinguishable from 0 (2,058 [-4,437, 8,076] yen/100sh per day).
- [val_seen] PPO touch-minus-trade-through gap: +0 yen/100sh per day.
- [val_held] PPO makes 0.49 round trips per session: it essentially does not trade.
- [val_held] PPO enters mostly with market orders.
- [val_held] PPO exits mostly by market/forced close (limit-exit share 0%).
- [val_held] PPO plays 'keep' on 99.8% of decisions.
- [val_held] PPO trade-through daily PnL is not distinguishable from 0 (72 [-3,301, 3,197] yen/100sh per day).
- [val_held] PPO touch-minus-trade-through gap: +0 yen/100sh per day.

## 8. 再現コマンド
`uv run python scripts/train_scalp.py --features base+C+D+G+H --no-test --model C:\Users\sasai\Documents\Backcast\models\screen\s2s1_base_C_D_G_H.pt --report C:\Users\sasai\Documents\Backcast\.agents\docs\research\screen\s2s1_base_C_D_G_H.md --train-steps 10000000 --reuse-baselines C:\Users\sasai\Documents\Backcast\.agents\docs\research\screen\s2s1_baselines.json --order-mode market --reward-mode realized --gamma 1.0 --gae-lambda 1.0 --unrealized-loss-penalty 0.01 --inventory-penalty 0 --idle-penalty 0 --keep-penalty 0 --action-bonus 0.25 --fill-curriculum 0.0,0.5 --ent-start 0.03 --ent-end 0.005 --bc-from rule --bc-steps 2000 --bc-sessions 300 --bc-critic --seed 1`

# P3 開場スキャルピング v2 結果 (order_mode=market): 売買代金上位50銘柄 × 寄り付き10分

- 生成: 2026-11-07 10:46 / `scripts/train_scalp.py` (実測値のみ)

## 1. 設定
- 環境 `trading_rl.scalp_env.ScalpEnv`: 1銘柄×1日=1エピソード。判断は T_open+10s から1秒ごと (590回)、T_open+600s に全キャンセル+成行で強制決済して終了。
- 建玉: -1/0/+1 単位 (1単位=100株)。指値は買い・売り各1本まで。フラットは両建て注文可、ロング中は売り指値のみ、ショート中は買い指値のみ (違反は no-op として計数)。
- order_mode = **market**, 行動 (Discrete 4): 0=keep, 1=market_buy, 2=market_sell, 3=market_flatten。bid_kN = last-N呼値、ask_kN = last+N呼値 (その時点の価格帯の呼値)。market_buy = flat->long / short->flat、market_sell = flat->short / long->flat (last ± 0.5呼値、全成行は全指値をキャンセル)。
- 合法手マスク (action_mask): ロング中は買い指値/成行買い不可、ショート中は売り側不可、注文なしで cancel 不可、フラットで flatten 不可。PPO は学習・評価・再生ともマスク付き (非合法手は確率0、log-prob/エントロピー/argmax もマスク後)。マスク時の無効アクション数 invalid/sess は判断時点の違反のみ。判断後レイテンシ中に指値が約定して無効化した分は別カウント (n_latency_noops)。
- 約定ルール (主): **trade_through** = 買い指値 P は P より安い約定が出たとき、売り指値 P はP より高い約定が出たときに全量 P で約定 (P ちょうどの約定では約定しない)。参考: touch = P ちょうどでも約定。待ち行列順位は未モデル、marketable 指値の即時約定も未モデル。
- レイテンシ 50 ms: 全アクション (発注・訂正・取消・成行) は判断時刻+50ms に有効化。新規注文は有効化時刻より厳密に後の約定のみで約定。
- 成行: last ± 0.5 呼値 (買いは+、売りは-)。手数料 0.0 円/約定 (パラメータ)。窓外 (T_open+10s 前・T_open+600s 後) の約定はテストで禁止を確認。
- 報酬 = 1ステップの含み損益 (最終約定値で時価評価) の差分を、セッション基準呼値 (寄り付き価格の呼値) で割った値。ペナルティなしなら合計は最終損益 (呼値) に厳密に一致。PPO 学習時のみ割引リターンの走行標準偏差で報酬を割る (スケールのみ)。
- **学習報酬 (シェイピング) と生損益の区別**: 学習時のみ 在庫ペナルティ 0.0 呼値/秒 × |建玉| × 1秒、アイドルペナルティ 0.0 呼値/秒 (フラットかつ注文なし; 取引自体を目的化するため非推奨、比較用)、行動ベース (建玉・注文の状態は見ない): keep 判断ごとの罰 0.0 呼値、実際に効いた keep 以外の行動ごとの報酬 0.25 呼値。報酬の土台 reward_mode=realized (mtm = 含み損益込みの毎秒の損益変化 / realized = 決済で確定した損益のみ、決済した秒に支払い)。このレポートの全ての PnL (学習曲線の val/train 列、結果表) は **生損益** (ペナルティ控除前)。ペナルティ込みの学習報酬は学習曲線の「train shaped」列のみ。検証・テストはペナルティなし・固定約定ルール。
- 観測 (38次元): gate_features の市場特徴量 28 個 (因果的) + 自己状態 10 個 (建玉, 含み損益[呼値], 買い注文の有無/last からの距離, 売り注文の有無/距離, エントリーからの秒/60, 各注文発注からの秒/60, 残り時間割合)。正規化は ActorCritic の obs_mean/obs_std に学習セッションで fit。
- 日次PnL = その日の全セッション (≤50銘柄) の PnL 合計。CI は日単位ブートストラップ (2000回)。yen/sess は1セッション平均。PF・勝率はラウンドトリップ単位。max DD は日次累積 PnL (円/100株) の最大ドローダウン。

## 2. データ
| split | 期間 | 既知銘柄 sessions | 学習外銘柄 sessions | 備考 |
|---|---|---|---|---|
| train | 2024-01-04 .. 2025-06-30 | 15,016 | 0 | 学習外銘柄は除外 |
| val | 2025-07-01 .. 2025-12-31 | 4,936 | 1,236 |  |
| test | 2026-01-01 .. 2026-08-19 | 5,970 | 1,638 |  |

`ok` 状態のキャッシュ済みセッションのみ。学習外銘柄 = md5(stem)[0] % 5 == 0 (約20%)。

## 3. 学習
- PPO: hidden 128, lr 0.0003 (線形減衰), gamma 1.0, GAE λ 1.0, clip 0.2, entropy 0.03 -> 0.005 (線形), value_coef 0.5, 4 epochs, minibatch 1024, 32 envs × 256 steps/iter, seed 0。gamma=0.997 は約590ステップのエピソードに合わせた (有効ホライズン約330秒)。
- 約定ルール・カリキュラム (学習のみ): 新エピソードごとに trade_through の確率が 0.0 から 1.0 へ最初の 50% のステップで線形に上昇 (それ以外は touch)。 検証/テストは常に trade_through (主) と touch (参考)。
- 行動模倣ウォームスタート: rule / 2000 steps / 300 sessions / 177,000 samples, CE 0.812 → 0.511, argmax一致率 98.2%, label smoothing 0.1, critic あり, 教師 `{'mode': 'mm', 'k_entry': 0, 'tp': 3, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}`。
- 実行: 10,002,432 env steps, 1221 iterations, 学習壁時計 22.8 分, **スループット 9,581 steps/s** (更新込み・評価除く、4 CPU スレッド、マスクあり; 評価込み 7,305 steps/s)。
- チェックポイント選択: 検証 (既知銘柄の固定 1000 セッション, trade_through, 決定論的) のセッション平均 yen/100sh 最大。選択 = 9,003,008 steps、保存先 `models\ppo_scalp_top50_open10m_market_v2_realized_g1.pt`。

### 学習曲線 (検証は固定サブセット・決定論的)
| steps | wall min | train ep ticks (raw) | train shaped | entropy | ent coef | p(trade_through) | val yen/sess (raw) | val ticks/sess | val trips/sess | val orders/sess | val invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 0.3 | +nan | +nan | nan | 0.0300 | 0.00 | -2504.4 | -7.71 | 5.67 | 5.7 | 0.00 |
| 507,904 | 1.4 | -145.6 | -69.5 | 0.935 | 0.0288 | 0.10 | -2718.5 | -9.39 | 11.58 | 12.2 | 0.00 |
| 1,007,616 | 2.6 | -136.8 | -66.8 | 0.956 | 0.0275 | 0.20 | -1150.6 | -3.85 | 6.83 | 9.0 | 0.00 |
| 1,507,328 | 3.8 | -137.7 | -66.3 | 0.956 | 0.0263 | 0.30 | -2760.4 | -7.04 | 11.52 | 19.6 | 0.00 |
| 2,007,040 | 4.9 | -128.4 | -63.6 | 0.898 | 0.0250 | 0.40 | -2685.7 | -6.69 | 7.36 | 12.3 | 0.00 |
| 2,506,752 | 6.1 | -115.8 | -55.5 | 0.966 | 0.0238 | 0.50 | -2139.8 | -5.90 | 7.77 | 12.1 | 0.00 |
| 3,006,464 | 7.2 | -138.5 | -67.1 | 0.987 | 0.0225 | 0.60 | -2013.5 | -5.06 | 4.95 | 7.1 | 0.00 |
| 3,506,176 | 8.4 | -140.5 | -66.1 | 0.911 | 0.0213 | 0.70 | -361.6 | -2.69 | 6.75 | 11.5 | 0.00 |
| 4,005,888 | 9.5 | -116.7 | -53.6 | 0.915 | 0.0200 | 0.80 | -1500.4 | -4.46 | 6.83 | 10.8 | 0.00 |
| 4,505,600 | 10.7 | -124.1 | -58.9 | 0.829 | 0.0188 | 0.90 | -1884.3 | -3.82 | 3.77 | 4.9 | 0.00 |
| 5,005,312 | 11.8 | -113.8 | -52.6 | 0.942 | 0.0175 | 1.00 | -2508.1 | -6.48 | 9.17 | 12.6 | 0.00 |
| 5,505,024 | 13.0 | -126.8 | -60.8 | 0.977 | 0.0163 | 1.00 | -2626.1 | -6.66 | 8.83 | 13.8 | 0.00 |
| 6,004,736 | 14.1 | -98.9 | -47.3 | 0.898 | 0.0150 | 1.00 | -1811.0 | -5.94 | 7.82 | 8.6 | 0.00 |
| 6,504,448 | 15.2 | -86.7 | -40.8 | 0.843 | 0.0138 | 1.00 | -1205.4 | -2.92 | 2.45 | 2.6 | 0.00 |
| 7,004,160 | 16.3 | -87.8 | -42.4 | 0.723 | 0.0125 | 1.00 | -849.7 | -1.95 | 1.29 | 1.6 | 0.00 |
| 7,503,872 | 17.4 | -73.1 | -34.2 | 0.618 | 0.0113 | 1.00 | -340.3 | -1.56 | 1.64 | 2.4 | 0.00 |
| 8,003,584 | 18.5 | -57.2 | -26.4 | 0.620 | 0.0100 | 1.00 | +445.3 | -0.25 | 2.66 | 4.2 | 0.00 |
| 8,503,296 | 19.6 | -52.0 | -24.0 | 0.535 | 0.0088 | 1.00 | +706.9 | -0.21 | 2.07 | 3.0 | 0.00 |
| 9,003,008 | 20.7 | -38.1 | -16.5 | 0.459 | 0.0075 | 1.00 | +745.8 | +0.52 | 1.79 | 2.5 | 0.00 |
| 9,502,720 | 21.8 | -41.1 | -19.2 | 0.441 | 0.0063 | 1.00 | +430.7 | -1.15 | 2.04 | 3.0 | 0.00 |
| 10,002,432 | 22.8 | -33.2 | -12.6 | 0.436 | 0.0050 | 1.00 | +525.4 | -1.42 | 2.05 | 3.1 | 0.00 |

## 4. RuleScalper のチューニング (検証・既知銘柄・trade_through)
- ベースライン (チューニング・評価) はキャッシュ `.agents\docs\research\rl-top50-scalp-v2-baselines.json` から再利用 (同一 order_mode/設定)。
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
| PPO | trade_through | 4936/124 | -4,674 [-30,181, 21,380] | -45.6 [-115.3, 16.8] | -117.4 | 74 | 1.86 | 57.5 | 0.98 | 2,206,800 | 0/9189 | 0/4669/4520 | 0.0% | 278.8 | 0.00 |
| PPO | touch | 4936/124 | -4,674 [-30,181, 21,380] | -45.6 [-115.3, 16.8] | -117.4 | 74 | 1.86 | 57.5 | 0.98 | 2,206,800 | 0/9189 | 0/4669/4520 | 0.0% | 278.8 | 0.00 |

### 検証 (学習外銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 1236/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| DoNothing | touch | 1236/124 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| Random | trade_through | 1236/124 | -736,473 [-764,989, -711,393] | -1,483.2 [-1,519.7, -1,446.6] | -73885.7 | 1482 | 148.66 | 9.6 | 0.14 | 91,322,680 | 0/183747 | 0/183081/666 | 0.0% | 2.0 | 145.85 |
| Random | touch | 1236/124 | -736,473 [-764,989, -711,393] | -1,483.2 [-1,519.7, -1,446.6] | -73885.7 | 1482 | 148.66 | 9.6 | 0.14 | 91,322,680 | 0/183747 | 0/183081/666 | 0.0% | 2.0 | 145.85 |
| RuleScalper(tuned) | trade_through | 1236/124 | -53,696 [-61,706, -45,711] | -103.3 [-117.2, -90.5] | -5387.0 | 111 | 11.14 | 60.3 | 0.65 | 6,658,310 | 0/13774 | 0/13047/727 | 0.0% | 51.4 | 0.00 |
| RuleScalper(tuned) | touch | 1236/124 | -53,696 [-61,706, -45,711] | -103.3 [-117.2, -90.5] | -5387.0 | 111 | 11.14 | 60.3 | 0.65 | 6,658,310 | 0/13774 | 0/13047/727 | 0.0% | 51.4 | 0.00 |
| PPO | trade_through | 1236/124 | -1,369 [-10,169, 7,063] | -1.5 [-19.4, 13.8] | -137.4 | 14 | 1.43 | 55.0 | 0.97 | 491,640 | 0/1767 | 0/629/1138 | 0.0% | 365.0 | 0.00 |
| PPO | touch | 1236/124 | -1,369 [-10,169, 7,063] | -1.5 [-19.4, 13.8] | -137.4 | 14 | 1.43 | 55.0 | 0.97 | 491,640 | 0/1767 | 0/629/1138 | 0.0% | 365.0 | 0.00 |

### テスト (既知銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 5970/153 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| DoNothing | touch | 5970/153 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| Random | trade_through | 5970/153 | -3,311,853 [-3,455,579, -3,180,651] | -5,849.3 [-5,899.2, -5,802.4] | -84876.6 | 5796 | 148.54 | 17.5 | 0.25 | 506,713,450 | 0/886756 | 0/883613/3143 | 0.0% | 2.0 | 146.32 |
| Random | touch | 5970/153 | -3,311,853 [-3,455,579, -3,180,651] | -5,849.3 [-5,899.2, -5,802.4] | -84876.6 | 5796 | 148.54 | 17.5 | 0.25 | 506,713,450 | 0/886756 | 0/883613/3143 | 0.0% | 2.0 | 146.32 |
| RuleScalper(tuned) | trade_through | 5970/153 | -241,765 [-278,818, -205,642] | -550.1 [-622.4, -482.0] | -6196.0 | 600 | 15.38 | 72.5 | 0.77 | 36,990,000 | 0/91823 | 0/88549/3274 | 0.0% | 36.9 | 0.00 |
| RuleScalper(tuned) | touch | 5970/153 | -241,765 [-278,818, -205,642] | -550.1 [-622.4, -482.0] | -6196.0 | 600 | 15.38 | 72.5 | 0.77 | 36,990,000 | 0/91823 | 0/88549/3274 | 0.0% | 36.9 | 0.00 |
| PPO | trade_through | 5970/153 | -18,540 [-60,092, 22,929] | -60.7 [-144.5, 15.9] | -475.1 | 120 | 3.07 | 62.4 | 0.96 | 7,289,625 | 0/18324 | 0/12646/5678 | 0.0% | 176.9 | 0.00 |
| PPO | touch | 5970/153 | -18,540 [-60,092, 22,929] | -60.7 [-144.5, 15.9] | -475.1 | 120 | 3.07 | 62.4 | 0.96 | 7,289,625 | 0/18324 | 0/12646/5678 | 0.0% | 176.9 | 0.00 |

### テスト (学習外銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 1638/153 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| DoNothing | touch | 1638/153 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | 0.00 |
| Random | trade_through | 1638/153 | -1,320,254 [-1,426,928, -1,217,176] | -1,614.6 [-1,654.6, -1,575.5] | -123320.5 | 1594 | 148.89 | 19.5 | 0.21 | 201,998,935 | 0/243889 | 0/242920/969 | 0.0% | 2.0 | 146.62 |
| Random | touch | 1638/153 | -1,320,254 [-1,426,928, -1,217,176] | -1,614.6 [-1,654.6, -1,575.5] | -123320.5 | 1594 | 148.89 | 19.5 | 0.21 | 201,998,935 | 0/243889 | 0/242920/969 | 0.0% | 2.0 | 146.62 |
| RuleScalper(tuned) | trade_through | 1638/153 | -121,615 [-147,317, -96,250] | -176.7 [-209.1, -145.5] | -11359.7 | 176 | 16.48 | 74.5 | 0.68 | 18,615,015 | 0/26996 | 0/26090/906 | 0.0% | 34.4 | 0.00 |
| RuleScalper(tuned) | touch | 1638/153 | -121,615 [-147,317, -96,250] | -176.7 [-209.1, -145.5] | -11359.7 | 176 | 16.48 | 74.5 | 0.68 | 18,615,015 | 0/26996 | 0/26090/906 | 0.0% | 34.4 | 0.00 |
| PPO | trade_through | 1638/153 | 2,556 [-18,483, 23,599] | -9.4 [-39.1, 20.8] | +238.8 | 45 | 4.18 | 63.4 | 1.02 | 1,785,270 | 0/6842 | 0/5294/1548 | 0.0% | 129.2 | 0.00 |
| PPO | touch | 1638/153 | 2,556 [-18,483, 23,599] | -9.4 [-39.1, 20.8] | +238.8 | 45 | 4.18 | 63.4 | 1.02 | 1,785,270 | 0/6842 | 0/5294/1548 | 0.0% | 129.2 | 0.00 |

### trade_through と touch の差 (touch - trade_through, 日次 yen/100sh 平均)
| group | DoNothing | Random | RuleScalper(tuned) | PPO |
|---|---|---|---|---|
| 検証 (既知銘柄) | +0 | +0 | +0 | +0 |
| 検証 (学習外銘柄) | +0 | +0 | +0 | +0 |
| テスト (既知銘柄) | +0 | +0 | +0 | +0 |
| テスト (学習外銘柄) | +0 | +0 | +0 | +0 |

## 6. PPO の挙動 (テスト・既知銘柄・trade_through)
- 1セッションあたり: 発注 4.8 回, ラウンドトリップ 3.07, 平均保有 176.9 秒, 取引ゼロのセッション 0.9%, 無効アクション 0.00 (レイテンシ中の失効 0.00)。
| action | share of decisions |
|---|---|
| keep | 99.12% |
| market_buy | 0.49% |
| market_sell | 0.33% |
| market_flatten | 0.06% |

## 7. 自動所見 (事実のみ)
- [val_seen] PPO enters mostly with market orders.
- [val_seen] PPO exits mostly by market/forced close (limit-exit share 0%).
- [val_seen] 49% of PPO exits are the forced 600 s close (it holds to the end).
- [val_seen] PPO plays 'keep' on 99.5% of decisions.
- [val_seen] PPO trade-through daily PnL is not distinguishable from 0 (-4,674 [-30,181, 21,380] yen/100sh per day).
- [val_seen] PPO touch-minus-trade-through gap: +0 yen/100sh per day.
- [val_held] PPO enters mostly with market orders.
- [val_held] PPO exits mostly by market/forced close (limit-exit share 0%).
- [val_held] 64% of PPO exits are the forced 600 s close (it holds to the end).
- [val_held] PPO plays 'keep' on 99.7% of decisions.
- [val_held] PPO trade-through daily PnL is not distinguishable from 0 (-1,369 [-10,169, 7,063] yen/100sh per day).
- [val_held] PPO touch-minus-trade-through gap: +0 yen/100sh per day.
- [test_seen] PPO enters mostly with market orders.
- [test_seen] PPO exits mostly by market/forced close (limit-exit share 0%).
- [test_seen] 31% of PPO exits are the forced 600 s close (it holds to the end).
- [test_seen] PPO plays 'keep' on 99.1% of decisions.
- [test_seen] PPO trade-through daily PnL is not distinguishable from 0 (-18,540 [-60,092, 22,929] yen/100sh per day).
- [test_seen] PPO touch-minus-trade-through gap: +0 yen/100sh per day.
- [test_held] PPO enters mostly with market orders.
- [test_held] PPO exits mostly by market/forced close (limit-exit share 0%).
- [test_held] PPO plays 'keep' on 98.7% of decisions.
- [test_held] PPO trade-through daily PnL is not distinguishable from 0 (2,556 [-18,483, 23,599] yen/100sh per day).
- [test_held] PPO touch-minus-trade-through gap: +0 yen/100sh per day.

## 8. 再現コマンド
`uv run python scripts/train_scalp.py --order-mode market --train-steps 10000000 --reward-mode realized --gamma 1.0 --gae-lambda 1.0 --inventory-penalty 0 --idle-penalty 0 --keep-penalty 0 --action-bonus 0.25 --fill-curriculum 0.0,0.5 --ent-start 0.03 --ent-end 0.005 --bc-from rule --bc-steps 2000 --bc-sessions 300 --bc-critic --reuse-baselines .agents/docs/research/rl-top50-scalp-v2-baselines.json --model models/ppo_scalp_top50_open10m_market_v2_realized_g1.pt --report .agents/docs/research/rl-top50-scalp-v2-market-realized-g1.md`

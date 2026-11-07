# P3 開場スキャルピング v2 結果 (order_mode=market): 売買代金上位50銘柄 × 寄り付き10分

- 生成: 2026-11-07 12:54 / `scripts/train_scalp.py` (実測値のみ)

## 1. 設定
- 環境 `trading_rl.scalp_env.ScalpEnv`: 1銘柄×1日=1エピソード。判断は T_open+10s から1秒ごと (590回)、T_open+600s に全キャンセル+成行で強制決済して終了。
- 建玉: -1/0/+1 単位 (1単位=100株)。指値は買い・売り各1本まで。フラットは両建て注文可、ロング中は売り指値のみ、ショート中は買い指値のみ (違反は no-op として計数)。
- order_mode = **market**, 行動 (Discrete 4): 0=keep, 1=market_buy, 2=market_sell, 3=market_flatten。bid_kN = last-N呼値、ask_kN = last+N呼値 (その時点の価格帯の呼値)。market_buy = flat->long / short->flat、market_sell = flat->short / long->flat (last ± 0.5呼値、全成行は全指値をキャンセル)。
- 合法手マスク (action_mask): ロング中は買い指値/成行買い不可、ショート中は売り側不可、注文なしで cancel 不可、フラットで flatten 不可。PPO は学習・評価・再生ともマスク付き (非合法手は確率0、log-prob/エントロピー/argmax もマスク後)。マスク時の無効アクション数 invalid/sess は判断時点の違反のみ。判断後レイテンシ中に指値が約定して無効化した分は別カウント (n_latency_noops)。
- 約定ルール (主): **trade_through** = 買い指値 P は P より安い約定が出たとき、売り指値 P はP より高い約定が出たときに全量 P で約定 (P ちょうどの約定では約定しない)。参考: touch = P ちょうどでも約定。待ち行列順位は未モデル、marketable 指値の即時約定も未モデル。
- レイテンシ 50 ms: 全アクション (発注・訂正・取消・成行) は判断時刻+50ms に有効化。新規注文は有効化時刻より厳密に後の約定のみで約定。
- 成行: last ± 0.5 呼値 (買いは+、売りは-)。手数料 0.0 円/約定 (パラメータ)。窓外 (T_open+10s 前・T_open+600s 後) の約定はテストで禁止を確認。
- 報酬 = 1ステップの含み損益 (最終約定値で時価評価) の差分を、セッション基準呼値 (寄り付き価格の呼値) で割った値。ペナルティなしなら合計は最終損益 (呼値) に厳密に一致。PPO 学習時のみ割引リターンの走行標準偏差で報酬を割る (スケールのみ)。
- **学習報酬 (シェイピング) と生損益の区別**: 学習時のみ 在庫ペナルティ 0.0 呼値/秒 × |建玉| × 1秒、アイドルペナルティ 0.0 呼値/秒 (フラットかつ注文なし; 取引自体を目的化するため非推奨、比較用)、行動ベース (建玉・注文の状態は見ない): keep 判断ごとの罰 0.0 呼値、実際に効いた keep 以外の行動ごとの報酬 0.0 呼値。報酬の土台 reward_mode=mtm (mtm = 含み損益込みの毎秒の損益変化 / realized = 決済で確定した損益のみ、決済した秒に支払い)。含み損ペナルティ 0.0 × 含み損(呼値) / 秒 (建玉中のみ、含み益には報酬なし)。このレポートの全ての PnL (学習曲線の val/train 列、結果表) は **生損益** (ペナルティ控除前)。ペナルティ込みの学習報酬は学習曲線の「train shaped」列のみ。検証・テストはペナルティなし・固定約定ルール。
- 観測 (45次元): 特徴量スペック **`base+A`** = 市場特徴量 35 個 (gate_features 28個から -J で削った残り + 追加グループ A-I、全て因果的) + 自己状態 10 個 (建玉, 含み損益[呼値], 買い注文の有無/last からの距離, 売り注文の有無/距離, エントリーからの秒/60, 各注文発注からの秒/60, 残り時間割合)。正規化は ActorCritic の obs_mean/obs_std に学習セッションで fit (学習セッションのみ。検証/テストの統計は使わない)。**`--no-test`: テスト分割は一切読み込まず、評価もしていない (スクリーニング用)。**
- 日次PnL = その日の全セッション (≤50銘柄) の PnL 合計。CI は日単位ブートストラップ (2000回)。yen/sess は1セッション平均。PF・勝率はラウンドトリップ単位。max DD は日次累積 PnL (円/100株) の最大ドローダウン。

## 2. データ
| split | 期間 | 既知銘柄 sessions | 学習外銘柄 sessions | 備考 |
|---|---|---|---|---|
| train | 2024-01-04 .. 2025-06-30 | 520 | 0 | 学習外銘柄は除外 |
| val | 2025-07-01 .. 2025-12-31 | 490 | 106 |  |

`ok` 状態のキャッシュ済みセッションのみ。学習外銘柄 = md5(stem)[0] % 5 == 0 (約20%)。

## 3. 学習
- PPO: hidden 128, lr 0.0003 (線形減衰), gamma 0.997, GAE λ 0.95, clip 0.2, entropy 0.02 -> 0.02 (線形), value_coef 0.5, 4 epochs, minibatch 1024, 32 envs × 256 steps/iter, seed 0。gamma=0.997 は約590ステップのエピソードに合わせた (有効ホライズン約330秒)。
- 約定ルール・カリキュラム (学習のみ): なし (常に trade_through)。 検証/テストは常に trade_through (主) と touch (参考)。
- 行動模倣ウォームスタート: なし。
- 実行: 106,496 env steps, 13 iterations, 学習壁時計 0.2 分, **スループット 10,981 steps/s** (更新込み・評価除く、2 CPU スレッド、マスクあり; 評価込み 9,787 steps/s)。
- チェックポイント選択: 検証 (既知銘柄の固定 100 セッション, trade_through, 決定論的) のセッション平均 yen/100sh 最大。選択 = 106,496 steps、保存先 `C:\Users\sasai\Documents\Backcast\models\screen\smoke1_base_A.pt`。

### 学習曲線 (検証は固定サブセット・決定論的)
| steps | wall min | train ep ticks (raw) | train shaped | entropy | ent coef | p(trade_through) | val yen/sess (raw) | val ticks/sess | val trips/sess | val orders/sess | val invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 106,496 | 0.2 | -116.8 | -116.8 | 0.722 | 0.0200 | n/a | +0.0 | +0.00 | 0.00 | 0.0 | 0.00 |

## 4. RuleScalper のチューニング (検証・既知銘柄・trade_through)
- ベースライン (チューニング・評価) はキャッシュ `C:\Users\sasai\Documents\Backcast\.agents\docs\research\screen\smoke1_baselines.json` から再利用 (同一 order_mode/設定)。
- グリッド 36 通り (mode, k_entry, tp, sl, timeout, requote)。第1段 20 セッションで全探索、上位 3 を 40 セッションで再評価して凍結。第1段で yen/sess>0 の設定: 0/36、最良 -2020.0、中央値 -9365.0。
| chosen | params | stage1 yen/sess | stage2 yen/sess | stage2 ticks/sess | trips/sess |
|---|---|---|---|---|---|
| * | `{'mode': 'mm', 'k_entry': 0, 'tp': 2, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -2530.0 | -1213.8 | -11.11 | 13.68 |
|  | `{'mode': 'long', 'k_entry': 0, 'tp': 2, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -2502.5 | -4853.3 | -14.52 | 12.78 |
|  | `{'mode': 'long', 'k_entry': 0, 'tp': 3, 'sl': 'inf', 'timeout_s': 90, 'requote_s': 5, 'no_entry_last_s': 30}` | -2020.0 | -5072.8 | -14.50 | 11.00 |

### Momentum のチューニング (検証・既知銘柄・trade_through)
- グリッド 81 通り (lookback 30/60/120s × 閾値 0/1/2 呼値 × 保有 60/180/590s × 最早エントリー 10/30/60s)。第1段 20 セッション全探索、上位 3 を 40 セッションで再評価して凍結。第1段で yen/sess>0: 3/81、最良 +612.5、中央値 -3545.0。
| chosen | params | stage1 yen/sess | stage2 yen/sess | stage2 ticks/sess | trips/sess |
|---|---|---|---|---|---|
| * | `{'lookback_s': 30, 'thr_ticks': 2.0, 'hold_s': 590, 'entry_s': 60}` | +440.0 | +1306.5 | -1.46 | 0.97 |
|  | `{'lookback_s': 30, 'thr_ticks': 0.0, 'hold_s': 590, 'entry_s': 60}` | +612.5 | +527.0 | +0.98 | 1.00 |
|  | `{'lookback_s': 30, 'thr_ticks': 1.0, 'hold_s': 590, 'entry_s': 60}` | +535.0 | +325.5 | -0.59 | 1.00 |

## 5. 結果
### 検証 (既知銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | gross ticks/trip | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 490/12 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| DoNothing | touch | 490/12 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| Random | trade_through | 490/12 | -2,153,364 [-2,287,046, -2,002,700] | -6,164.8 [-6,287.4, -6,034.6] | -52735.4 | 6092 | 149.19 | 10.7 | 0.15 | 25,840,370 | 0/73101 | 0/72846/255 | 0.0% | 2.0 | -0.002 | 145.37 |
| Random | touch | 490/12 | -2,153,364 [-2,287,046, -2,002,700] | -6,164.8 [-6,287.4, -6,034.6] | -52735.4 | 6092 | 149.19 | 10.7 | 0.15 | 25,840,370 | 0/73101 | 0/72846/255 | 0.0% | 2.0 | -0.002 | 145.37 |
| RuleScalper(tuned) | trade_through | 490/12 | -145,564 [-178,727, -118,800] | -467.5 [-543.2, -378.1] | -3564.8 | 583 | 14.28 | 68.1 | 0.68 | 1,746,770 | 0/6996 | 0/6734/262 | 0.0% | 39.8 | 0.218 | 0.00 |
| RuleScalper(tuned) | touch | 490/12 | -145,564 [-178,727, -118,800] | -467.5 [-543.2, -378.1] | -3564.8 | 583 | 14.28 | 68.1 | 0.68 | 1,746,770 | 0/6996 | 0/6734/262 | 0.0% | 39.8 | 0.218 | 0.00 |
| Momentum(tuned) | trade_through | 490/12 | 12,272 [-30,447, 65,617] | 67.3 [-43.4, 193.2] | +300.5 | 40 | 0.98 | 48.5 | 1.11 | 182,040 | 0/482 | 0/0/482 | 0.0% | 537.3 | 2.682 | 0.00 |
| Momentum(tuned) | touch | 490/12 | 12,272 [-30,447, 65,617] | 67.3 [-43.4, 193.2] | +300.5 | 40 | 0.98 | 48.5 | 1.11 | 182,040 | 0/482 | 0/0/482 | 0.0% | 537.3 | 2.682 | 0.00 |
| PPO | trade_through | 490/12 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| PPO | touch | 490/12 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |

### 検証 (学習外銘柄)
| policy | fill rule | sessions/days | yen/100sh per day [95% CI] | ticks per day [95% CI] | yen/sess | trips/day | trips/sess | win % | PF | max DD (yen) | entries L/M | exits L/M/forced | limit-exit share | hold s | gross ticks/trip | invalid/sess |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DoNothing | trade_through | 106/12 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| DoNothing | touch | 106/12 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| Random | trade_through | 106/12 | -604,174 [-630,339, -578,825] | -1,327.1 [-1,451.1, -1,223.3] | -68397.1 | 1334 | 151.05 | 8.6 | 0.12 | 7,250,090 | 0/16011 | 0/15958/53 | 0.0% | 1.9 | 0.005 | 143.36 |
| Random | touch | 106/12 | -604,174 [-630,339, -578,825] | -1,327.1 [-1,451.1, -1,223.3] | -68397.1 | 1334 | 151.05 | 8.6 | 0.12 | 7,250,090 | 0/16011 | 0/15958/53 | 0.0% | 1.9 | 0.005 | 143.36 |
| RuleScalper(tuned) | trade_through | 106/12 | -58,799 [-86,109, -40,508] | -127.0 [-161.1, -97.1] | -6656.5 | 108 | 12.23 | 61.2 | 0.50 | 705,590 | 0/1296 | 0/1239/57 | 0.0% | 46.7 | -0.176 | 0.00 |
| RuleScalper(tuned) | touch | 106/12 | -58,799 [-86,109, -40,508] | -127.0 [-161.1, -97.1] | -6656.5 | 108 | 12.23 | 61.2 | 0.50 | 705,590 | 0/1296 | 0/1239/57 | 0.0% | 46.7 | -0.176 | 0.00 |
| Momentum(tuned) | trade_through | 106/12 | 17,750 [1,337, 34,881] | 42.3 [-3.1, 81.6] | +2009.4 | 9 | 1.00 | 62.3 | 2.14 | 28,150 | 0/106 | 0/0/106 | 0.0% | 534.2 | 5.786 | 0.00 |
| Momentum(tuned) | touch | 106/12 | 17,750 [1,337, 34,881] | 42.3 [-3.1, 81.6] | +2009.4 | 9 | 1.00 | 62.3 | 2.14 | 28,150 | 0/106 | 0/0/106 | 0.0% | 534.2 | 5.786 | 0.00 |
| PPO | trade_through | 106/12 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |
| PPO | touch | 106/12 | 0 [0, 0] | 0.0 [0.0, 0.0] | +0.0 | 0 | 0.00 | nan | n/a | 0 | 0/0 | 0/0/0 | n/a% | n/a | n/a | 0.00 |

### trade_through と touch の差 (touch - trade_through, 日次 yen/100sh 平均)
| group | DoNothing | Random | RuleScalper(tuned) | Momentum(tuned) | PPO |
|---|---|---|---|---|---|
| 検証 (既知銘柄) | +0 | +0 | +0 | +0 | +0 |
| 検証 (学習外銘柄) | +0 | +0 | +0 | +0 | +0 |

## 6. PPO の挙動 (検証・既知銘柄・trade_through)
- 1セッションあたり: 発注 0.0 回, ラウンドトリップ 0.00, 平均保有 n/a 秒, 取引ゼロのセッション 100.0%, 無効アクション 0.00 (レイテンシ中の失効 0.00)。
| action | share of decisions |
|---|---|
| keep | 100.00% |
| market_buy | 0.00% |
| market_sell | 0.00% |
| market_flatten | 0.00% |

## 7. 自動所見 (事実のみ)
- [val_seen] PPO makes 0.00 round trips per session: it essentially does not trade.
- [val_seen] PPO plays 'keep' on 100.0% of decisions.
- [val_seen] PPO result is identical to DoNothing.
- [val_seen] PPO trade-through daily PnL is not distinguishable from 0 (0 [0, 0] yen/100sh per day).
- [val_seen] PPO touch-minus-trade-through gap: +0 yen/100sh per day.
- [val_held] PPO makes 0.00 round trips per session: it essentially does not trade.
- [val_held] PPO plays 'keep' on 100.0% of decisions.
- [val_held] PPO result is identical to DoNothing.
- [val_held] PPO trade-through daily PnL is not distinguishable from 0 (0 [0, 0] yen/100sh per day).
- [val_held] PPO touch-minus-trade-through gap: +0 yen/100sh per day.

## 8. 再現コマンド
`uv run python scripts/train_scalp.py --features base+A --no-test --model C:\Users\sasai\Documents\Backcast\models\screen\smoke1_base_A.pt --report C:\Users\sasai\Documents\Backcast\.agents\docs\research\screen\smoke1_base_A.md --train-steps 100000 --reuse-baselines C:\Users\sasai\Documents\Backcast\.agents\docs\research\screen\smoke1_baselines.json --smoke --order-mode market --workers 2 --threads 2`

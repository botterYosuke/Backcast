/*
 * 仮想発注（ペーパートレード）の純ロジック。
 *
 * DOM にも板の描画にも依存しないので、node:test から直接叩ける。
 * 前提は「一日信用取引・両建て禁止」:
 *   - 建玉はネットで 1 つだけ。買い建玉があるときの売り約定は返済になる。
 *   - 返済数量を超えたぶんはドテン（反対方向の新規建玉）として扱う。
 *   - 返済しきれば何回でも新規建玉を建て直せる（回数制限なし）。
 */

'use strict';

/** 建玉ゼロの状態を作る。qty は符号付き（正＝買建 / 負＝売建）。 */
export function createPortfolio() {
  return { qty: 0, avgPrice: 0, realized: 0, entryCount: 0, exitCount: 0 };
}

/** 現在の建玉方向。'buy' / 'sell' / null（ノーポジ）。 */
export function positionSide(portfolio) {
  if (portfolio.qty > 0) return 'buy';
  if (portfolio.qty < 0) return 'sell';
  return null;
}

/**
 * 約定 1 件を建玉に反映し、発生したイベントを時系列順で返す。
 *
 * 返り値は 'exit'（返済）→ 'entry'（新規）の順。ドテンのときだけ 2 件になる。
 * 呼び出し側はこれをそのままチャートのマーカーにできる。
 */
export function applyFill(portfolio, fill) {
  const { side, price, time } = fill;
  const signed = side === 'buy' ? 1 : -1;
  const events = [];
  let remaining = fill.qty;

  // 反対建玉があるなら、まず返済に充てる（両建て禁止）。
  if (portfolio.qty !== 0 && Math.sign(portfolio.qty) !== signed) {
    const closeQty = Math.min(remaining, Math.abs(portfolio.qty));
    const pnl = (price - portfolio.avgPrice) * closeQty * Math.sign(portfolio.qty);
    portfolio.realized += pnl;
    portfolio.qty += signed * closeQty;
    portfolio.exitCount += 1;
    remaining -= closeQty;
    events.push({ kind: 'exit', side, qty: closeQty, price, pnl, time });
    if (portfolio.qty === 0) portfolio.avgPrice = 0;
  }

  if (remaining > 0) {
    const held = Math.abs(portfolio.qty);
    portfolio.avgPrice =
      held === 0 ? price : (portfolio.avgPrice * held + price * remaining) / (held + remaining);
    portfolio.qty += signed * remaining;
    portfolio.entryCount += 1;
    events.push({ kind: 'entry', side, qty: remaining, price, time });
  }

  return events;
}

/** 評価損益。建玉が無い / 現在値が未確定なら 0。 */
export function unrealizedPnl(portfolio, lastPrice) {
  if (!portfolio.qty || !Number.isFinite(lastPrice)) return 0;
  return (lastPrice - portfolio.avgPrice) * portfolio.qty;
}

/** 合計損益 ＝ 実現損益 ＋ 評価損益。 */
export function totalPnl(portfolio, lastPrice) {
  return portfolio.realized + unrealizedPnl(portfolio, lastPrice);
}

/**
 * 板のクリックを成行 / 指値に振り分ける（呼値レベルで比較する）。
 *   売り: 現在値より上 = 指値、現在値以下 = 成行
 *   買い: 現在値より下 = 指値、現在値以上 = 成行
 */
export function classifyClick(side, level, lastLevel) {
  if (side === 'sell') return level > lastLevel ? 'limit' : 'market';
  return level < lastLevel ? 'limit' : 'market';
}

/** 指値が約定するか。売りは指値以上、買いは指値以下の約定で成立する。 */
export function isFilled(order, tradePrice) {
  return order.side === 'sell' ? tradePrice >= order.price : tradePrice <= order.price;
}

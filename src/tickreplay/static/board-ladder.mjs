export const BOARD_MIN_LEVEL = 1;

export function boardRowCount(
  viewportHeight,
  { rowHeight = 20, minRows = 41, overscanRows = 12 } = {},
) {
  const visibleRows = Math.max(1, Math.ceil(Math.max(0, viewportHeight) / rowHeight));
  let count = Math.max(minRows, visibleRows + overscanRows * 2);
  if (count % 2 === 0) count += 1;
  return count;
}

export function levelAtRow(topLevel, rowIndex) {
  return topLevel - rowIndex;
}

export function rowIndexForLevel(topLevel, level, rowCount) {
  const index = topLevel - level;
  return index >= 0 && index < rowCount ? index : -1;
}

export function levelsInWindow(topLevel, rowCount) {
  return Array.from({ length: rowCount }, (_, index) => levelAtRow(topLevel, index));
}

export function centeredTopLevel(centerLevel, rowCount, minLevel = BOARD_MIN_LEVEL) {
  const middleIndex = Math.floor(rowCount / 2);
  return Math.max(minLevel + rowCount - 1, centerLevel + middleIndex);
}

export function centeredScrollTop({ rowOffsetTop, rowHeight, viewportHeight, maxScrollTop }) {
  const desired = rowOffsetTop - (viewportHeight - rowHeight) / 2;
  return Math.max(0, Math.min(maxScrollTop, desired));
}

export function hasProgrammaticScrollDrift(actualScrollTop, targetScrollTop, tolerance = 0.5) {
  if (!Number.isFinite(actualScrollTop) || !Number.isFinite(targetScrollTop)) return false;
  return Math.abs(actualScrollTop - targetScrollTop) > tolerance;
}

export function planBoardRebase({
  scrollTop,
  viewportHeight,
  scrollHeight,
  rowHeight,
  topLevel,
  rowCount,
  edgeRows = 4,
  shiftRows = 8,
  minLevel = BOARD_MIN_LEVEL,
}) {
  const maxScrollTop = Math.max(0, scrollHeight - viewportHeight);
  const edgeDistance = edgeRows * rowHeight;
  let levelDelta = 0;

  if (scrollTop <= edgeDistance) {
    const availableRows = Math.floor((maxScrollTop - scrollTop) / rowHeight);
    levelDelta = Math.min(shiftRows, availableRows);
  } else if (scrollTop >= maxScrollTop - edgeDistance) {
    const minimumTopLevel = minLevel + rowCount - 1;
    const availableLevels = Math.max(0, topLevel - minimumTopLevel);
    const availableRows = Math.floor(scrollTop / rowHeight);
    levelDelta = -Math.min(shiftRows, availableLevels, availableRows);
  }

  if (levelDelta === 0) return null;
  return {
    levelDelta,
    topLevel: topLevel + levelDelta,
    scrollTop: scrollTop + levelDelta * rowHeight,
  };
}

export function boardFractionDigits(tick) {
  if (!Number.isFinite(tick) || tick <= 0 || Number.isInteger(tick)) return 0;
  for (let digits = 1; digits <= 6; digits += 1) {
    const scaled = tick * (10 ** digits);
    if (Math.abs(scaled - Math.round(scaled)) < 1e-8) return digits;
  }
  return 2;
}

export function createBoardPriceFormatter(tick, locale = 'ja-JP') {
  const fractionDigits = boardFractionDigits(tick);
  return new Intl.NumberFormat(locale, {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

export function pendingQuantityAtRow(pendingByLevel, topLevel, rowIndex, rowCount) {
  if (rowIndex < 0 || rowIndex >= rowCount) return undefined;
  return pendingByLevel.get(levelAtRow(topLevel, rowIndex));
}

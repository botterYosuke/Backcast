import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  BOARD_MIN_LEVEL,
  boardFractionDigits,
  boardRowCount,
  centeredScrollTop,
  centeredTopLevel,
  createBoardPriceFormatter,
  hasProgrammaticScrollDrift,
  levelAtRow,
  levelsInWindow,
  pendingQuantityAtRow,
  planBoardRebase,
  rowIndexForLevel,
} from './board-ladder.mjs';

const APP_SOURCE = readFileSync(new URL('./app.js', import.meta.url), 'utf8');

function assertContiguous(levels) {
  for (let index = 1; index < levels.length; index += 1) {
    assert.equal(levels[index], levels[index - 1] - 1);
  }
}

function functionSource(name) {
  const start = APP_SOURCE.indexOf('function ' + name + '(');
  assert.notEqual(start, -1, 'missing function ' + name);
  const bodyStart = APP_SOURCE.indexOf('{', start);
  let depth = 0;
  for (let index = bodyStart; index < APP_SOURCE.length; index += 1) {
    if (APP_SOURCE[index] === '{') depth += 1;
    else if (APP_SOURCE[index] === '}') {
      depth -= 1;
      if (depth === 0) return APP_SOURCE.slice(start, index + 1);
    }
  }
  assert.fail('unterminated function ' + name);
}

test('row pool is odd, bounded, and includes runway for tall viewports', () => {
  assert.equal(boardRowCount(200), 41);
  assert.equal(boardRowCount(800), 65);
  assert.equal(boardRowCount(1200), 85);
});

test('row and level mapping is contiguous and reversible', () => {
  const levels = levelsInWindow(240, 41);
  assert.equal(levels.length, 41);
  assertContiguous(levels);
  assert.equal(levelAtRow(240, 17), 223);
  assert.equal(rowIndexForLevel(240, 223, 41), 17);
  assert.equal(rowIndexForLevel(240, 199, 41), -1);
});

test('repeated upward and downward rebases keep a constant contiguous window', () => {
  const rowCount = 65;
  let topLevel = 1000;

  for (let iteration = 0; iteration < 200; iteration += 1) {
    const result = planBoardRebase({
      scrollTop: 40,
      viewportHeight: 800,
      scrollHeight: 1300,
      rowHeight: 20,
      topLevel,
      rowCount,
    });
    assert.ok(result);
    topLevel = result.topLevel;
    const levels = levelsInWindow(topLevel, rowCount);
    assert.equal(levels.length, rowCount);
    assertContiguous(levels);
  }

  for (let iteration = 0; iteration < 400; iteration += 1) {
    const result = planBoardRebase({
      scrollTop: 460,
      viewportHeight: 800,
      scrollHeight: 1300,
      rowHeight: 20,
      topLevel,
      rowCount,
    });
    if (!result) break;
    topLevel = result.topLevel;
    const levels = levelsInWindow(topLevel, rowCount);
    assert.equal(levels.length, rowCount);
    assertContiguous(levels);
  }

  assert.equal(topLevel, BOARD_MIN_LEVEL + rowCount - 1);
  assert.equal(levelsInWindow(topLevel, rowCount).at(-1), BOARD_MIN_LEVEL);
});

test('rebase scroll compensation preserves a logical level pixel position', () => {
  const before = { topLevel: 500, scrollTop: 40 };
  const result = planBoardRebase({
    ...before,
    viewportHeight: 800,
    scrollHeight: 1300,
    rowHeight: 20,
    rowCount: 65,
  });
  assert.ok(result);

  const logicalLevel = 480;
  const beforePixel = rowIndexForLevel(before.topLevel, logicalLevel, 65) * 20 - before.scrollTop;
  const afterPixel = rowIndexForLevel(result.topLevel, logicalLevel, 65) * 20 - result.scrollTop;
  assert.equal(afterPixel, beforePixel);
  assert.equal(result.scrollTop - before.scrollTop, result.levelDelta * 20);
});

test('coalesced downward momentum is repeatedly rebased away from the physical end', () => {
  const metrics = {
    viewportHeight: 625,
    scrollHeight: 1148,
    rowHeight: 20,
    rowCount: 57,
  };
  const physicalMaximum = metrics.scrollHeight - metrics.viewportHeight;
  let topLevel = 1000;

  for (let iteration = 0; iteration < 20; iteration += 1) {
    const result = planBoardRebase({ ...metrics, scrollTop: physicalMaximum, topLevel });
    assert.ok(result);
    assert.ok(hasProgrammaticScrollDrift(physicalMaximum, result.scrollTop));
    assert.ok(result.scrollTop < physicalMaximum - metrics.rowHeight * 4);
    topLevel = result.topLevel;
  }
  assert.equal(topLevel, 840);
});

test('coalesced upward momentum is repeatedly rebased away from the physical start', () => {
  const metrics = {
    viewportHeight: 625,
    scrollHeight: 1148,
    rowHeight: 20,
    rowCount: 57,
  };
  let topLevel = 1000;

  for (let iteration = 0; iteration < 20; iteration += 1) {
    const result = planBoardRebase({ ...metrics, scrollTop: 0, topLevel });
    assert.ok(result);
    assert.ok(hasProgrammaticScrollDrift(0, result.scrollTop));
    assert.ok(result.scrollTop > metrics.rowHeight * 4);
    topLevel = result.topLevel;
  }
  assert.equal(topLevel, 1160);
});

test('downward momentum stops at the positive level floor', () => {
  const metrics = {
    viewportHeight: 625,
    scrollHeight: 1148,
    rowHeight: 20,
    rowCount: 57,
  };
  const physicalMaximum = metrics.scrollHeight - metrics.viewportHeight;
  const result = planBoardRebase({ ...metrics, scrollTop: physicalMaximum, topLevel: 60 });
  assert.ok(result);
  assert.equal(result.topLevel, 57);
  assert.equal(levelsInWindow(result.topLevel, metrics.rowCount).at(-1), BOARD_MIN_LEVEL);
  assert.equal(
    planBoardRebase({ ...metrics, scrollTop: physicalMaximum, topLevel: result.topLevel }),
    null,
  );
});

test('matching programmatic targets do not look like user scroll drift', () => {
  assert.equal(hasProgrammaticScrollDrift(264, 264), false);
  assert.equal(hasProgrammaticScrollDrift(264.25, 264, 0.5), false);
  assert.equal(hasProgrammaticScrollDrift(523, 363), true);
});

test('centering calculations respect the positive floor and viewport bounds', () => {
  assert.equal(centeredTopLevel(1000, 41), 1020);
  assert.equal(centeredTopLevel(5, 41), 41);
  assert.equal(centeredScrollTop({
    rowOffsetTop: 404,
    rowHeight: 20,
    viewportHeight: 300,
    maxScrollTop: 528,
  }), 264);
  assert.equal(centeredScrollTop({
    rowOffsetTop: 4,
    rowHeight: 20,
    viewportHeight: 300,
    maxScrollTop: 528,
  }), 0);
});

test('board price formatter uses uniform tick-derived decimals', () => {
  assert.equal(boardFractionDigits(0.1), 1);
  assert.equal(boardFractionDigits(0.5), 1);
  assert.equal(boardFractionDigits(1), 0);
  assert.equal(boardFractionDigits(5), 0);
  assert.equal(createBoardPriceFormatter(0.5).format(1000), '1,000.0');
  assert.equal(createBoardPriceFormatter(0.5).format(1000.5), '1,000.5');
  assert.equal(createBoardPriceFormatter(1).format(1000), '1,000');
});

test('pending quantities remain keyed to their price level after scrolling away and back', () => {
  const pendingSell = new Map([[1000, 300]]);
  const rowCount = 41;
  const originalTop = centeredTopLevel(1000, rowCount);
  const originalRow = rowIndexForLevel(originalTop, 1000, rowCount);
  assert.equal(pendingQuantityAtRow(pendingSell, originalTop, originalRow, rowCount), 300);

  const awayTop = originalTop + 200;
  assert.equal(rowIndexForLevel(awayTop, 1000, rowCount), -1);

  const returnedRow = rowIndexForLevel(originalTop, 1000, rowCount);
  assert.equal(pendingQuantityAtRow(pendingSell, originalTop, returnedRow, rowCount), 300);
});

test('app wiring shares current-price centering and keeps order clicks level-derived', () => {
  assert.match(APP_SOURCE, /function centerBoardOnCurrent\(\)/);
  assert.match(APP_SOURCE, /boardCenter\.addEventListener\('click', centerBoardOnCurrent\)/);
  assert.match(
    APP_SOURCE,
    /addEventListener\('dblclick',[\s\S]*?closest\('\.board-price'\)[\s\S]*?centerBoardOnCurrent\(\)/,
  );
  assert.match(
    APP_SOURCE,
    /const level = levelAtRow\(board\.topLevel, Number\(cell\.parentElement\.dataset\.index\)\)/,
  );
});

test('manual navigation and lifecycle wiring preserve the board state contract', () => {
  assert.match(functionSource('scheduleBoardScrollReconcile'), /board\.manual = true/);
  assert.match(functionSource('updateBoard'), /!board\.manual &&/);
  assert.match(functionSource('resetBoardNavigation'), /cancelBoardScrollWork\(\)/);
  assert.match(functionSource('redrawAll'), /resetBoardNavigation\(\)/);
  assert.match(functionSource('loadSession'), /setPlaying\(false\);\s*resetBoardNavigation\(\)/);
  assert.match(functionSource('updateBoard'), /syncRowFlash\(row, level\)/);
});

test('suppressed scroll drift is deferred and reconciled as manual after release', () => {
  const setter = functionSource('setBoardScrollTop');
  const listener = functionSource('onBoardScroll');
  assert.match(setter, /board\.programmaticScrollTarget = scrollTop/);
  assert.match(setter, /hasProgrammaticScrollDrift\(actualScrollTop, targetScrollTop\)/);
  assert.match(setter, /scheduleBoardScrollReconcile\(actualScrollTop, true\)/);
  assert.match(listener, /if \(board\.programmaticScroll\)/);
  assert.match(listener, /board\.deferredScrollTop = els\.board\.scrollTop/);
  assert.match(
    listener,
    /if \(!hasProgrammaticScrollDrift\(els\.board\.scrollTop, targetScrollTop\)\) return/,
  );
});

test('an empty session clears the previously rendered board before returning', () => {
  assert.match(
    functionSource('loadSession'),
    /state\.last = null;[\s\S]*?tickSeries\.setData\(\[\]\);\s*clearBoard\(\);[\s\S]*?return;/,
  );
});

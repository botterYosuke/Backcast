import assert from 'node:assert/strict';
import test from 'node:test';

import {
  BUNDLED_RL_STRATEGY,
  CUSTOM_PYTHON_STRATEGY,
  STRATEGY_SELECTION_STORAGE_KEY,
  createStrategySelectionStore,
} from './strategy-selection-store.mjs';

function memoryStorage(initial = null) {
  const values = new Map();
  if (initial !== null) values.set(STRATEGY_SELECTION_STORAGE_KEY, initial);
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
    values,
  };
}

test('missing, obsolete, and malformed records all reset logically to Bundled RL', () => {
  for (const raw of [
    null,
    '{',
    JSON.stringify({ schemaVersion: 0, selection: CUSTOM_PYTHON_STRATEGY }),
    JSON.stringify({ schemaVersion: 1, selection: 'native_trace_v1' }),
    JSON.stringify({ schemaVersion: 1, selection: CUSTOM_PYTHON_STRATEGY, extra: true }),
  ]) {
    assert.equal(createStrategySelectionStore({ storage: memoryStorage(raw) }).read(), BUNDLED_RL_STRATEGY);
  }
});

test('only the two current choices can be persisted', () => {
  const storage = memoryStorage();
  const store = createStrategySelectionStore({ storage });
  assert.equal(store.write(CUSTOM_PYTHON_STRATEGY), CUSTOM_PYTHON_STRATEGY);
  assert.equal(store.read(), CUSTOM_PYTHON_STRATEGY);
  assert.equal(store.write(BUNDLED_RL_STRATEGY), BUNDLED_RL_STRATEGY);
  assert.equal(store.read(), BUNDLED_RL_STRATEGY);
  assert.throws(() => store.write('pyodide'), /invalid strategy selection/);
});

test('reset removes the record and returns Bundled RL', () => {
  const storage = memoryStorage();
  const store = createStrategySelectionStore({ storage });
  store.write(CUSTOM_PYTHON_STRATEGY);
  assert.equal(store.reset(), BUNDLED_RL_STRATEGY);
  assert.equal(storage.values.has(STRATEGY_SELECTION_STORAGE_KEY), false);
});

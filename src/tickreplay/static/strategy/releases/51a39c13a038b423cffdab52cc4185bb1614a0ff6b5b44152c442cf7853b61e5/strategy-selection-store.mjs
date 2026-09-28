'use strict';

export const STRATEGY_SELECTION_STORAGE_KEY = 'tickreplay.strategy.selection.v1';
export const BUNDLED_RL_STRATEGY = 'bundled_rl';
export const CUSTOM_PYTHON_STRATEGY = 'custom_python';
export const STRATEGY_SELECTIONS = Object.freeze([
  BUNDLED_RL_STRATEGY,
  CUSTOM_PYTHON_STRATEGY,
]);

const SCHEMA_VERSION = 1;

function isSelection(value) {
  return STRATEGY_SELECTIONS.includes(value);
}

export function createStrategySelectionStore({ storage = globalThis.localStorage } = {}) {
  if (!storage || typeof storage.getItem !== 'function' || typeof storage.setItem !== 'function') {
    throw new TypeError('strategy selection storage is required');
  }

  function read() {
    let parsed;
    try {
      const raw = storage.getItem(STRATEGY_SELECTION_STORAGE_KEY);
      if (raw === null) return BUNDLED_RL_STRATEGY;
      parsed = JSON.parse(raw);
    } catch {
      return BUNDLED_RL_STRATEGY;
    }
    if (
      parsed === null
      || typeof parsed !== 'object'
      || Array.isArray(parsed)
      || parsed.schemaVersion !== SCHEMA_VERSION
      || !isSelection(parsed.selection)
      || Object.keys(parsed).sort().join(',') !== 'schemaVersion,selection'
    ) return BUNDLED_RL_STRATEGY;
    return parsed.selection;
  }

  function write(selection) {
    if (!isSelection(selection)) throw new TypeError('invalid strategy selection');
    storage.setItem(STRATEGY_SELECTION_STORAGE_KEY, JSON.stringify({
      schemaVersion: SCHEMA_VERSION,
      selection,
    }));
    return selection;
  }

  function reset() {
    storage.removeItem?.(STRATEGY_SELECTION_STORAGE_KEY);
    return BUNDLED_RL_STRATEGY;
  }

  return Object.freeze({ read, write, reset });
}

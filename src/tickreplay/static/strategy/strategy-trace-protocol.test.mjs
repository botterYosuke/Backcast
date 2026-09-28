import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
  BUNDLED_RL_PRESET,
  BUNDLED_RL_TRACE_CONTRACT,
  BundledRlTraceProtocolError,
  createBundledRlSubmitRequest,
  validateBundledRlCapability,
  validateBundledRlJobSubmission,
  validateBundledRlTraceArtifact,
} from './strategy-trace-protocol.mjs';

const CONTRACT_FIXTURE = JSON.parse(readFileSync(
  new URL('../../../../tests/fixtures/bundled-rl/native-contract.json', import.meta.url),
  'utf8',
));

const HASH = 'a'.repeat(64);
const OTHER_HASH = 'b'.repeat(64);

function decision(overrides = {}) {
  return {
    observedSessionTickIndex: 0,
    eligibleFromSessionTick: 1,
    decisionTimeUs: 1_789_000_200_000_000,
    positionBranches: [
      { positionQuantity: 0, targetQuantity: 100 },
      { positionQuantity: 100, targetQuantity: 0 },
    ],
    ...overrides,
  };
}

function artifact(overrides = {}) {
  return {
    contractVersion: BUNDLED_RL_TRACE_CONTRACT,
    preset: { ...BUNDLED_RL_PRESET },
    session: { stem: '7203', code: '7203', date: '2026-09-28', dataHash: HASH, tickCount: 4 },
    inputId: OTHER_HASH,
    runtimeFingerprint: HASH,
    proposalContract: 'backcast_position_target_v1',
    decisions: [decision()],
    ...overrides,
  };
}

test('capability requires the exact bundled preset identity', () => {
  const value = validateBundledRlCapability(CONTRACT_FIXTURE.capability);
  assert.equal(Object.isFrozen(value.preset), true);
  assert.equal(validateBundledRlCapability(CONTRACT_FIXTURE.unavailableCapability).available, false);
  assert.throws(() => validateBundledRlCapability({ ...value, preset: { ...BUNDLED_RL_PRESET, revision: 'other' } }), (error) => error.code === 'preset_mismatch');
  assert.throws(() => validateBundledRlCapability({ ...value, source: 'print(1)' }), (error) => error.code === 'unknown_field');
  assert.throws(() => validateBundledRlCapability({ ...value, unavailableReason: 'wrong state' }), (error) => error.code === 'invalid_capability');
});

test('submit payload contains only contract, preset, and authoritative session identity', () => {
  const payload = createBundledRlSubmitRequest({ session: CONTRACT_FIXTURE.submit.session });
  assert.deepEqual(Object.keys(payload), ['contractVersion', 'preset', 'session']);
  assert.equal(JSON.stringify(payload).includes('source'), false);
  assert.deepEqual(payload, CONTRACT_FIXTURE.submit);
});

test('job submission and trace validators accept the frozen backend fixture exactly', () => {
  assert.deepEqual(validateBundledRlJobSubmission(CONTRACT_FIXTURE.jobResponse), CONTRACT_FIXTURE.jobResponse.job);
  assert.throws(
    () => validateBundledRlJobSubmission(CONTRACT_FIXTURE.jobResponse.job),
    (error) => error.code === 'unknown_field',
  );
  assert.deepEqual(validateBundledRlTraceArtifact(CONTRACT_FIXTURE.trace), CONTRACT_FIXTURE.trace);
});

test('artifact accepts exact ordered position branches and freezes them', () => {
  const value = validateBundledRlTraceArtifact(artifact());
  assert.deepEqual(value.decisions[0].positionBranches.map((branch) => branch.positionQuantity), [0, 100]);
  assert.equal(Object.isFrozen(value.decisions[0]), true);
});

test('artifact rejects source, commands, broker state, unknown fields, and wrong preset', () => {
  for (const forbidden of ['source', 'sourceHash', 'commands', 'portfolio', 'fills', 'executor']) {
    assert.throws(() => validateBundledRlTraceArtifact({ ...artifact(), [forbidden]: true }), (error) => error.code === 'unknown_field');
  }
  assert.throws(() => validateBundledRlTraceArtifact(artifact({ preset: { ...BUNDLED_RL_PRESET, digest: HASH } })), (error) => error.code === 'preset_mismatch');
});

test('artifact rejects missing, reversed, duplicate, or invalid position branches', () => {
  const invalidBranches = [
    [],
    [{ positionQuantity: 0, targetQuantity: 100 }],
    [{ positionQuantity: 100, targetQuantity: 0 }, { positionQuantity: 0, targetQuantity: 100 }],
    [{ positionQuantity: 0, targetQuantity: 50 }, { positionQuantity: 100, targetQuantity: 0 }],
  ];
  for (const positionBranches of invalidBranches) {
    assert.throws(() => validateBundledRlTraceArtifact(artifact({ decisions: [decision({ positionBranches })] })), BundledRlTraceProtocolError);
  }
});

test('artifact enforces next-tick causality, strict observed order, and no final-tick row', () => {
  for (const decisions of [
    [decision({ eligibleFromSessionTick: 2 })],
    [decision({ observedSessionTickIndex: 3, eligibleFromSessionTick: 4 })],
    [decision(), decision({ decisionTimeUs: 1_789_000_210_000_000 })],
    [decision({ sequence: 0 })],
  ]) {
    assert.throws(() => validateBundledRlTraceArtifact(artifact({ decisions })), BundledRlTraceProtocolError);
  }
});

test('artifact rejects non-finite and non-plain values', () => {
  assert.throws(() => validateBundledRlTraceArtifact(artifact({ session: { ...artifact().session, tickCount: Infinity } })), BundledRlTraceProtocolError);
  assert.throws(() => validateBundledRlTraceArtifact(Object.assign(Object.create(null), artifact(), { extra: undefined })), BundledRlTraceProtocolError);
});

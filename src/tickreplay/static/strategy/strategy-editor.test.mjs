import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ABI_COMPLETIONS,
  createAbiCompletionSource,
  createStrategyEditor,
  diagnosticToCodeMirror,
  sourcePositionToOffset,
} from './strategy-editor.mjs';

function element(attributes = {}) {
  const values = new Map(Object.entries(attributes));
  return {
    hidden: false,
    readOnly: false,
    tabIndex: 0,
    value: '',
    selectionStart: 0,
    selectionEnd: 0,
    scrollTop: 0,
    focused: false,
    setAttribute: (name, value) => values.set(name, String(value)),
    removeAttribute: (name) => values.delete(name),
    getAttribute: (name) => values.get(name) ?? null,
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    focus() { this.focused = true; },
    attributes: values,
  };
}

function fakeToolkit() {
  let listener = null;
  let destroyed = 0;
  let diagnosticDispatch = null;
  class EditorState {
    static create({ doc, selection, extensions }) {
      listener = extensions.flat().find((item) => item?.kind === 'listener')?.callback ?? null;
      return {
        doc: { toString: () => doc },
        selection: { main: selection },
      };
    }
  }
  class EditorView {
    static updateListener = { of: (callback) => ({ kind: 'listener', callback }) };
    constructor({ state, parent }) {
      this.state = state;
      this.contentDOM = element();
      this.scrollDOM = { scrollTop: 0 };
      this.hasFocus = false;
      parent.view = this;
    }
    dispatch(spec) {
      if (spec.kind === 'diagnostics') {
        diagnosticDispatch = spec;
        return;
      }
      if (!spec.changes) {
        if (spec.selection) this.state.selection.main = spec.selection;
        return;
      }
      const next = spec.changes.insert;
      this.state = {
        doc: { toString: () => next },
        selection: { main: spec.selection ?? this.state.selection.main },
      };
      listener?.({ docChanged: true, state: this.state });
    }
    focus() { this.hasFocus = true; }
    destroy() { destroyed += 1; }
  }
  return {
    EditorState,
    EditorView,
    createBaseExtensions: () => [],
    setDiagnostics: (_state, diagnostics) => ({ kind: 'diagnostics', diagnostics }),
    diagnosticDispatch: () => diagnosticDispatch,
    destroyed: () => destroyed,
  };
}

test('Unicode scalar source positions map exactly to UTF-16 offsets', () => {
  const source = 'a😀b\n日本';
  assert.equal(sourcePositionToOffset(source, { line: 1, column: 2 }), 3);
  assert.equal(sourcePositionToOffset(source, { line: 2, column: 1 }), 6);
  assert.equal(sourcePositionToOffset(source, { line: 3, column: 0 }), null);
  assert.equal(sourcePositionToOffset(source, { line: 1, column: 4 }), null);
});

test('diagnostics decorate only the exact visible source identity and valid range', () => {
  const diagnostic = {
    source_hash: 'a'.repeat(64),
    draft_revision: 4,
    severity: 'error',
    message: '<syntax>',
    range: { start: { line: 1, column: 1 }, end: { line: 1, column: 2 } },
  };
  assert.deepEqual(diagnosticToCodeMirror(diagnostic, {
    source: '😀x', sourceHash: 'a'.repeat(64), draftRevision: 4,
  }), { from: 2, to: 3, severity: 'error', message: '<syntax>', source: 'strategy' });
  assert.equal(diagnosticToCodeMirror(diagnostic, {
    source: '😀x', sourceHash: 'b'.repeat(64), draftRevision: 4,
  }), null);
});

test('completion source is a fixed ABI-only list and never inspects runtime state', () => {
  const complete = createAbiCompletionSource();
  const result = complete({
    pos: 3,
    explicit: false,
    matchBefore: () => ({ from: 0, to: 3, text: 'on_' }),
  });
  assert.ok(ABI_COMPLETIONS.includes('on_event'));
  assert.ok(result.options.some(({ label }) => label === 'submit_limit'));
  assert.equal(result.options.some(({ label }) => label === 'fetch'), false);
});

test('enhancement exposes one editor, mirrors changes synchronously, and restores fallback', () => {
  const textarea = element({ 'aria-labelledby': 'label', 'aria-describedby': 'help' });
  textarea.value = 'print(1)';
  textarea.selectionStart = 2;
  textarea.selectionEnd = 2;
  textarea.ownerDocument = { activeElement: textarea };
  const container = element();
  container.hidden = true;
  const toolkit = fakeToolkit();
  let canonical = textarea.value;
  const changes = [];
  const degraded = [];
  const editor = createStrategyEditor({
    container,
    textarea,
    toolkit,
    getCanonicalSource: () => canonical,
    onSourceChange(source) { canonical = source; changes.push(source); },
    onDegraded: (error) => degraded.push(error.message),
    sourceHash: 'a'.repeat(64),
    draftRevision: 0,
  });

  assert.equal(editor.mode(), 'enhanced');
  assert.equal(textarea.hidden, true);
  assert.equal(textarea.tabIndex, -1);
  assert.equal(container.view.contentDOM.getAttribute('role'), 'textbox');
  container.view.dispatch({ changes: { from: 0, to: 8, insert: 'print(2)' } });
  assert.deepEqual(changes, ['print(2)']);
  assert.equal(textarea.value, 'print(1)', 'workspace owns raw textarea synchronization');

  editor.fallback(new Error('extension failed'));
  assert.equal(editor.mode(), 'degraded');
  assert.equal(textarea.value, 'print(2)');
  assert.equal(textarea.hidden, false);
  assert.equal(textarea.readOnly, false);
  assert.deepEqual(degraded, ['extension failed']);
  assert.equal(toolkit.destroyed(), 1);
  editor.dispose();
  assert.equal(toolkit.destroyed(), 1, 'dispose is idempotent after fallback');
});

test('a rejected editor transaction restores the previous canonical source', () => {
  const textarea = element();
  textarea.value = 'safe';
  const container = element();
  const toolkit = fakeToolkit();
  const editor = createStrategyEditor({
    container,
    textarea,
    toolkit,
    getCanonicalSource: () => 'safe',
    onSourceChange() { throw new Error('host rejected change'); },
    sourceHash: null,
    draftRevision: 0,
  });
  container.view.dispatch({ changes: { from: 0, to: 4, insert: 'unsafe' } });
  assert.equal(editor.mode(), 'degraded');
  assert.equal(textarea.value, 'safe');
});

test('diagnostic navigation selects only a mapped current diagnostic', () => {
  const textarea = element();
  textarea.value = '😀x';
  const container = element();
  const toolkit = fakeToolkit();
  const diagnostic = {
    source_hash: 'a'.repeat(64),
    draft_revision: 2,
    severity: 'error',
    message: 'mapped',
    range: { start: { line: 1, column: 1 }, end: { line: 1, column: 2 } },
  };
  const editor = createStrategyEditor({
    container,
    textarea,
    toolkit,
    getCanonicalSource: () => '😀x',
    onSourceChange() {},
    sourceHash: 'a'.repeat(64),
    draftRevision: 2,
    diagnostics: [diagnostic, { ...diagnostic, source_hash: 'b'.repeat(64) }],
  });

  assert.equal(editor.navigateDiagnostic(1), true);
  assert.deepEqual(container.view.state.selection.main, { anchor: 2, head: 3 });
  assert.equal(container.view.hasFocus, true);
  editor.dispose();
});

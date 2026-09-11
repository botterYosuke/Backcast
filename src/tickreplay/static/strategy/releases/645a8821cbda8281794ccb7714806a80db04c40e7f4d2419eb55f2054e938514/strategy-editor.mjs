'use strict';

export const ABI_COMPLETIONS = Object.freeze([
  'on_start', 'on_event', 'on_end',
  'next_session_tick_index', 'last_committed_session_tick_index',
  'last_replay_time_us', 'last_price_level', 'tick', 'fills', 'fill_cursor',
  'portfolio', 'open_orders', 'open_orders_total', 'open_orders_truncated',
  'session_tick_index', 'replay_time_us', 'price_level', 'quantity', 'trade_type',
  'cash_minor', 'position_quantity', 'average_price_level', 'realized_pnl_minor',
  'unrealized_pnl_minor', 'equity_minor', 'submit_market', 'submit_limit',
  'cancel_strategy_orders', 'client_intent_id', 'side', 'buy', 'sell',
]);

function scalarColumnToUtf16(lineText, scalarColumn) {
  if (!Number.isSafeInteger(scalarColumn) || scalarColumn < 0) return null;
  const scalars = Array.from(lineText);
  if (scalarColumn > scalars.length) return null;
  return scalars.slice(0, scalarColumn).join('').length;
}

export function sourcePositionToOffset(source, position) {
  if (typeof source !== 'string' || !position || !Number.isSafeInteger(position.line)) return null;
  const lines = source.split('\n');
  if (position.line < 1 || position.line > lines.length) return null;
  const column = scalarColumnToUtf16(lines[position.line - 1], position.column);
  if (column === null) return null;
  let offset = column;
  for (let index = 0; index < position.line - 1; index += 1) {
    offset += lines[index].length + 1;
  }
  return offset;
}

export function diagnosticToCodeMirror(diagnostic, { source, sourceHash, draftRevision }) {
  if (
    !diagnostic
    || diagnostic.source_hash !== sourceHash
    || diagnostic.draft_revision !== draftRevision
    || diagnostic.range === null
  ) return null;
  const from = sourcePositionToOffset(source, diagnostic.range.start);
  const to = sourcePositionToOffset(source, diagnostic.range.end);
  if (from === null || to === null || to < from) return null;
  return Object.freeze({
    from,
    to,
    severity: diagnostic.severity === 'fatal' ? 'error' : diagnostic.severity,
    message: diagnostic.message,
    source: 'strategy',
  });
}

export function createAbiCompletionSource(candidates = ABI_COMPLETIONS) {
  const options = candidates.map((label) => Object.freeze({ label, type: 'keyword' }));
  return (context) => {
    const word = context.matchBefore(/[A-Za-z_][A-Za-z0-9_]*$/);
    if (!word && !context.explicit) return null;
    return { from: word?.from ?? context.pos, options, validFor: /^[A-Za-z_][A-Za-z0-9_]*$/ };
  };
}

function setAttribute(element, name, value) {
  if (value === null || value === undefined || value === '') element.removeAttribute?.(name);
  else element.setAttribute?.(name, String(value));
}

function setTextareaMode(textarea, { hidden, readonly }) {
  textarea.hidden = hidden;
  textarea.readOnly = readonly;
  textarea.tabIndex = hidden ? -1 : 0;
  setAttribute(textarea, 'aria-hidden', hidden ? 'true' : null);
}

export function createStrategyEditor({
  container,
  textarea,
  toolkit,
  getCanonicalSource,
  onSourceChange,
  onDegraded,
  sourceHash,
  draftRevision,
  diagnostics = [],
  lineWrapping = false,
}) {
  if (!container || !textarea || !toolkit) throw new TypeError('editor elements and toolkit are required');
  if (typeof getCanonicalSource !== 'function' || typeof onSourceChange !== 'function') {
    throw new TypeError('editor source callbacks are required');
  }
  const { EditorState, EditorView, createBaseExtensions, setDiagnostics } = toolkit;
  if (!EditorState || !EditorView || typeof createBaseExtensions !== 'function') {
    throw new TypeError('invalid CodeMirror toolkit');
  }

  let view = null;
  let disposed = false;
  let degraded = false;
  let suppressHostChange = false;
  let identity = { sourceHash, draftRevision };
  const hadFocus = textarea.ownerDocument?.activeElement === textarea;
  const initialSelection = {
    anchor: Math.max(0, textarea.selectionStart ?? 0),
    head: Math.max(0, textarea.selectionEnd ?? textarea.selectionStart ?? 0),
  };
  const initialScrollTop = textarea.scrollTop ?? 0;

  function restoreTextarea({ focus = false, selection = initialSelection, scrollTop = initialScrollTop } = {}) {
    textarea.value = getCanonicalSource();
    setTextareaMode(textarea, { hidden: false, readonly: false });
    container.hidden = true;
    const length = textarea.value.length;
    textarea.setSelectionRange?.(
      Math.min(selection.anchor, length),
      Math.min(selection.head, length),
    );
    textarea.scrollTop = scrollTop;
    if (focus) textarea.focus?.();
  }

  function degrade(error, { acceptEditorDocument = false } = {}) {
    if (disposed || degraded) return;
    degraded = true;
    const editorSource = view?.state?.doc?.toString?.();
    const selection = view?.state?.selection?.main ?? initialSelection;
    const scrollTop = view?.scrollDOM?.scrollTop ?? initialScrollTop;
    const editorFocused = Boolean(view?.hasFocus);
    if (acceptEditorDocument && typeof editorSource === 'string' && editorSource !== getCanonicalSource()) {
      try {
        onSourceChange(editorSource, { origin: 'editor-recovery' });
      } catch {
        // The host rejected the editor value; its canonical draft wins.
      }
    }
    try { view?.destroy?.(); } catch { /* recovery must remain available */ }
    view = null;
    restoreTextarea({ focus: editorFocused, selection, scrollTop });
    try { onDegraded?.(error); } catch { /* optional observer cannot break recovery */ }
  }

  const completionSource = createAbiCompletionSource();
  const updateExtension = EditorView.updateListener.of((update) => {
    if (!update.docChanged || disposed || degraded) return;
    if (suppressHostChange) {
      suppressHostChange = false;
      return;
    }
    const source = update.state.doc.toString();
    try {
      onSourceChange(source, { origin: 'codemirror' });
    } catch (error) {
      degrade(error, { acceptEditorDocument: false });
    }
  });
  const escapeTabKeymap = [{
    key: 'Escape',
    run(currentView) {
      currentView.contentDOM?.setAttribute?.('data-escape-tab', 'true');
      return false;
    },
  }];
  const state = EditorState.create({
    doc: getCanonicalSource(),
    selection: initialSelection,
    extensions: [
      ...createBaseExtensions({ completionSource, extraKeymap: escapeTabKeymap, lineWrapping }),
      updateExtension,
    ],
  });
  view = new EditorView({ state, parent: container });
  setAttribute(view.contentDOM, 'role', 'textbox');
  setAttribute(view.contentDOM, 'aria-multiline', 'true');
  setAttribute(view.contentDOM, 'aria-labelledby', textarea.getAttribute?.('aria-labelledby'));
  setAttribute(view.contentDOM, 'aria-describedby', textarea.getAttribute?.('aria-describedby'));
  container.hidden = false;
  setTextareaMode(textarea, { hidden: true, readonly: true });
  if (hadFocus) view.focus();

  function publishDiagnostics(nextDiagnostics, nextIdentity = identity) {
    identity = { ...nextIdentity };
    if (!view || disposed || degraded || typeof setDiagnostics !== 'function') return [];
    const source = getCanonicalSource();
    const mapped = nextDiagnostics
      .map((diagnostic) => diagnosticToCodeMirror(diagnostic, { source, ...identity }))
      .filter(Boolean);
    view.dispatch(setDiagnostics(view.state, mapped));
    return mapped;
  }
  publishDiagnostics(diagnostics);

  function replaceSource(source, selection = null) {
    if (!view || disposed || degraded) return false;
    const current = view.state.doc.toString();
    if (current === source) return true;
    suppressHostChange = true;
    view.dispatch({
      changes: { from: 0, to: current.length, insert: source },
      selection: selection ?? { anchor: Math.min(view.state.selection.main.anchor, source.length) },
    });
    return true;
  }

  function dispose() {
    if (disposed) return;
    disposed = true;
    try { view?.destroy?.(); } finally { view = null; }
    container.hidden = true;
  }

  return Object.freeze({
    mode: () => (degraded ? 'degraded' : disposed ? 'disposed' : 'enhanced'),
    publishDiagnostics,
    replaceSource,
    focus: () => view?.focus?.(),
    fallback: (error) => degrade(error, { acceptEditorDocument: true }),
    dispose,
  });
}

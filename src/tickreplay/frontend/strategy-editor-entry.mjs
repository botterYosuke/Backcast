import {
  autocompletion,
  closeBrackets,
  closeBracketsKeymap,
  completionKeymap,
  startCompletion,
} from "@codemirror/autocomplete";
import {
  defaultKeymap,
  history,
  historyKeymap,
  indentMore,
  indentLess,
} from "@codemirror/commands";
import { python } from "@codemirror/lang-python";
import {
  bracketMatching,
  defaultHighlightStyle,
  indentUnit,
  syntaxHighlighting,
} from "@codemirror/language";
import { lintKeymap, setDiagnostics } from "@codemirror/lint";
import { highlightSelectionMatches, searchKeymap } from "@codemirror/search";
import { EditorState, Prec } from "@codemirror/state";
import {
  drawSelection,
  EditorView,
  highlightActiveLine,
  highlightActiveLineGutter,
  keymap,
  lineNumbers,
} from "@codemirror/view";

const FOUR_SPACE_INDENT = "    ";

function createBaseExtensions({ completionSource, extraKeymap = [], lineWrapping = false } = {}) {
  const extensions = [
    lineNumbers(),
    highlightActiveLineGutter(),
    history(),
    drawSelection(),
    highlightActiveLine(),
    highlightSelectionMatches(),
    bracketMatching(),
    closeBrackets(),
    indentUnit.of(FOUR_SPACE_INDENT),
    python(),
    syntaxHighlighting(defaultHighlightStyle, { fallback: true }),
    EditorView.baseTheme({
      ".cm-lintRange": {
        backgroundImage: "none !important",
        textDecoration: "underline wavy orange",
      },
    }),
    Prec.highest(
      keymap.of([
        ...extraKeymap,
        { key: "Tab", run: indentMore, shift: indentLess },
      ]),
    ),
    keymap.of([
      ...closeBracketsKeymap,
      ...defaultKeymap,
      ...searchKeymap,
      ...historyKeymap,
      ...completionKeymap,
      ...lintKeymap,
    ]),
  ];
  if (completionSource) {
    extensions.push(autocompletion({ override: [completionSource] }));
  }
  if (lineWrapping) {
    extensions.push(EditorView.lineWrapping);
  }
  return extensions;
}

export {
  EditorState,
  EditorView,
  autocompletion,
  createBaseExtensions,
  setDiagnostics,
  startCompletion,
};

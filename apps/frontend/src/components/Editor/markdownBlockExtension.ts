import { type CompletionContext, type CompletionResult } from '@codemirror/autocomplete';
import { EditorSelection, Prec, RangeSetBuilder, type Extension } from '@codemirror/state';
import {
  Decoration,
  type DecorationSet,
  EditorView,
  keymap,
  ViewPlugin,
  type ViewUpdate,
  WidgetType,
} from '@codemirror/view';
import {
  applySlashCommandToLine,
  getEnterInsertionForLine,
  matchSlashCommand,
  moveMarkdownBlockByOffset,
  moveMarkdownBlockInText,
  SLASH_COMMANDS,
  toggleTaskLine,
  type SlashCommandId,
} from './slashCommands';

type MarkdownBlockKind =
  | 'blank'
  | 'paragraph'
  | 'heading-1'
  | 'heading-2'
  | 'heading-3'
  | 'heading-4'
  | 'heading-5'
  | 'heading-6'
  | 'unordered-list'
  | 'ordered-list'
  | 'task-open'
  | 'task-done'
  | 'quote'
  | 'code-fence'
  | 'thematic-break';

type MarkdownMarker = {
  from: number;
  to: number;
  label: string;
};

export type MarkdownLineBlock = {
  kind: MarkdownBlockKind;
  marker?: MarkdownMarker;
};

// Two halves of the same job: the `-marker` kinds are the delimiters, which get
// hidden, and the bare kinds are the text they wrap, which gets styled. Hiding
// the delimiters alone is what made bold text render as plain text — the syntax
// disappeared and nothing took its place.
type InlineMarkdownKind =
  | 'strong-marker'
  | 'emphasis-marker'
  | 'code-marker'
  | 'strong'
  | 'emphasis'
  | 'code';

export type InlineMarkdownMark = {
  from: number;
  to: number;
  kind: InlineMarkdownKind;
};

const HEADING_RE = /^(#{1,6})\s+/;
const TASK_RE = /^(\s*)([-*+]\s+\[([ xX])\]\s+)/;
const UNORDERED_RE = /^(\s*)([-*+]\s+)/;
const ORDERED_RE = /^(\s*)(\d+[.)]\s+)/;
const QUOTE_RE = /^(\s*>+\s?)/;
const CODE_FENCE_RE = /^(\s*`{3,}|~{3,})/;
const THEMATIC_BREAK_RE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;

export function classifyMarkdownLine(text: string): MarkdownLineBlock {
  if (text.trim().length === 0) return { kind: 'blank' };

  const heading = HEADING_RE.exec(text);
  if (heading) {
    const level = Math.min(heading[1].length, 6);
    return {
      kind: `heading-${level}` as MarkdownBlockKind,
      marker: { from: 0, to: heading[0].length, label: '' },
    };
  }

  const task = TASK_RE.exec(text);
  if (task) {
    return {
      kind: task[3].toLowerCase() === 'x' ? 'task-done' : 'task-open',
      marker: {
        from: task[1].length,
        to: task[1].length + task[2].length,
        label: task[3].toLowerCase() === 'x' ? '☑' : '☐',
      },
    };
  }

  const unordered = UNORDERED_RE.exec(text);
  if (unordered) {
    return {
      kind: 'unordered-list',
      marker: {
        from: unordered[1].length,
        to: unordered[1].length + unordered[2].length,
        label: '•',
      },
    };
  }

  const ordered = ORDERED_RE.exec(text);
  if (ordered) {
    return {
      kind: 'ordered-list',
      marker: {
        from: ordered[1].length,
        to: ordered[1].length + ordered[2].length,
        label: ordered[2].trim(),
      },
    };
  }

  const quote = QUOTE_RE.exec(text);
  if (quote) {
    return {
      kind: 'quote',
      marker: { from: 0, to: quote[1].length, label: '' },
    };
  }

  const fence = CODE_FENCE_RE.exec(text);
  if (fence) {
    return {
      kind: 'code-fence',
      marker: { from: fence[1].search(/[`~]/), to: fence[1].length, label: 'code' },
    };
  }

  if (THEMATIC_BREAK_RE.test(text)) {
    return {
      kind: 'thematic-break',
      marker: { from: 0, to: text.length, label: '' },
    };
  }

  return { kind: 'paragraph' };
}

export function findInlineMarkdownMarks(text: string): InlineMarkdownMark[] {
  const marks: InlineMarkdownMark[] = [];
  const occupied = new Set<number>();

  function addPair(
    openFrom: number,
    openTo: number,
    closeFrom: number,
    closeTo: number,
    kind: 'strong' | 'emphasis' | 'code',
  ) {
    for (let index = openFrom; index < closeTo; index += 1) occupied.add(index);
    marks.push(
      { from: openFrom, to: openTo, kind: `${kind}-marker` as InlineMarkdownKind },
      { from: openTo, to: closeFrom, kind },
      { from: closeFrom, to: closeTo, kind: `${kind}-marker` as InlineMarkdownKind },
    );
  }

  for (const match of text.matchAll(/`([^`\n]+)`/g)) {
    const start = match.index ?? 0;
    addPair(start, start + 1, start + match[0].length - 1, start + match[0].length, 'code');
  }

  for (const match of text.matchAll(/\*\*([^*\n]+)\*\*/g)) {
    const start = match.index ?? 0;
    if (occupied.has(start)) continue;
    addPair(start, start + 2, start + match[0].length - 2, start + match[0].length, 'strong');
  }

  for (const match of text.matchAll(/(?<!\*)\*([^*\n]+)\*(?!\*)/g)) {
    const start = match.index ?? 0;
    if (occupied.has(start)) continue;
    addPair(start, start + 1, start + match[0].length - 1, start + match[0].length, 'emphasis');
  }

  return marks.sort((a, b) => a.from - b.from);
}

class MarkdownMarkerWidget extends WidgetType {
  constructor(
    readonly label: string,
    readonly kind: MarkdownBlockKind | InlineMarkdownKind,
    readonly lineNumber?: number,
  ) {
    super();
  }

  eq(other: MarkdownMarkerWidget) {
    return this.label === other.label && this.kind === other.kind && this.lineNumber === other.lineNumber;
  }

  toDOM() {
    if (this.kind === 'task-open' || this.kind === 'task-done') {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `cm-markdown-marker cm-markdown-marker-${this.kind} cm-task-toggle`;
      button.textContent = this.label;
      button.title = 'Toggle task';
      button.setAttribute('aria-label', 'Toggle task');
      if (this.lineNumber != null) button.dataset.line = String(this.lineNumber);
      return button;
    }

    const span = document.createElement('span');
    span.className = `cm-markdown-marker cm-markdown-marker-${this.kind}`;
    span.textContent = this.label;
    return span;
  }

  ignoreEvent() {
    return this.kind !== 'task-open' && this.kind !== 'task-done';
  }
}

class BlockHandleWidget extends WidgetType {
  constructor(readonly lineNumber: number) {
    super();
  }

  eq(other: BlockHandleWidget) {
    return this.lineNumber === other.lineNumber;
  }

  toDOM() {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'cm-block-handle';
    button.draggable = true;
    button.tabIndex = -1;
    button.textContent = '⋮⋮';
    button.title = 'Drag to move block';
    button.setAttribute('aria-label', 'Drag to move block');
    button.dataset.line = String(this.lineNumber);
    button.addEventListener('mousedown', (event) => {
      event.stopPropagation();
    });
    button.addEventListener('dragstart', (event) => {
      event.stopPropagation();
      event.dataTransfer?.setData('application/x-archivum-line', String(this.lineNumber));
      event.dataTransfer?.setData('text/plain', String(this.lineNumber));
      if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
    });
    return button;
  }

  ignoreEvent() {
    return false;
  }
}

const INLINE_MARKS: Record<string, Decoration> = {
  strong: Decoration.mark({ class: 'cm-md-strong' }),
  emphasis: Decoration.mark({ class: 'cm-md-emphasis' }),
  code: Decoration.mark({ class: 'cm-md-code' }),
};

function buildBlockDecorations(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();

  // Lines the cursor is on keep their raw syntax. Formatting you cannot see is
  // formatting you cannot remove — with the delimiters hidden everywhere there
  // is no way to tell where bold starts, or to get back out of it.
  const editing = new Set(
    view.state.selection.ranges.map((range) => view.state.doc.lineAt(range.head).number),
  );

  for (const { from, to } of view.visibleRanges) {
    let position = from;
    while (position <= to) {
      const line = view.state.doc.lineAt(position);
      const block = classifyMarkdownLine(line.text);

      const onCursorLine = editing.has(line.number);
      builder.add(
        line.from,
        line.from,
        Decoration.line({
          // `cm-prompt` drives the empty-line hint. It cannot key off
          // `.cm-activeLine`, because `highlightActiveLine` is not one of this
          // editor's extensions and that class is never applied.
          class: [
            'cm-markdown-block',
            `cm-markdown-${block.kind}`,
            block.kind === 'blank' && onCursorLine ? 'cm-prompt' : '',
          ]
            .filter(Boolean)
            .join(' '),
        }),
      );

      builder.add(
        line.from,
        line.from,
        Decoration.widget({
          widget: new BlockHandleWidget(line.number),
          side: -1,
        }),
      );

      if (block.marker) {
        builder.add(
          line.from + block.marker.from,
          line.from + block.marker.to,
          Decoration.replace({
            widget: new MarkdownMarkerWidget(block.marker.label, block.kind, line.number),
            inclusive: false,
          }),
        );
      }

      const revealing = onCursorLine;
      for (const mark of findInlineMarkdownMarks(line.text)) {
        if (mark.from === mark.to) continue;
        const isDelimiter = mark.kind.endsWith('-marker');
        if (isDelimiter && revealing) continue;
        builder.add(
          line.from + mark.from,
          line.from + mark.to,
          isDelimiter
            ? Decoration.replace({
                widget: new MarkdownMarkerWidget('', mark.kind),
                inclusive: false,
              })
            : INLINE_MARKS[mark.kind],
        );
      }

      if (line.to >= to) break;
      position = line.to + 1;
    }
  }

  return builder.finish();
}

function getLineFromDomTarget(view: EditorView, target: HTMLElement | null) {
  const lineElement = target?.closest('.cm-line');
  if (!lineElement) return null;

  try {
    return view.state.doc.lineAt(view.posAtDOM(lineElement, 0));
  } catch {
    return null;
  }
}

function getNearestLineBlockAtCoords(view: EditorView, event: MouseEvent) {
  const documentY = Math.max(0, Math.min(view.contentHeight, event.clientY - view.documentTop));
  const visibleBlock = view.viewportLineBlocks.find((block) => documentY >= block.top && documentY <= block.bottom);
  if (visibleBlock) return visibleBlock;

  return view.viewportLineBlocks.reduce((nearest, block) => {
    const nearestDistance = Math.min(Math.abs(documentY - nearest.top), Math.abs(documentY - nearest.bottom));
    const blockDistance = Math.min(Math.abs(documentY - block.top), Math.abs(documentY - block.bottom));
    return blockDistance < nearestDistance ? block : nearest;
  }, view.lineBlockAtHeight(documentY));
}

function getLinePositionAtCoords(view: EditorView, event: MouseEvent, target: HTMLElement | null) {
  const block = getNearestLineBlockAtCoords(view, event);
  const line = getLineFromDomTarget(view, target) ?? view.state.doc.lineAt(block.from);

  if (event.clientY < view.documentTop) return view.state.doc.line(1).from;
  if (event.clientY > view.documentTop + view.contentHeight) return view.state.doc.line(view.state.doc.lines).to;

  if (line.length === 0) return line.from;

  const precisePosition = view.posAtCoords({ x: event.clientX, y: event.clientY });
  if (precisePosition != null) {
    const preciseLine = view.state.doc.lineAt(precisePosition);
    if (preciseLine.number === line.number) return precisePosition;
  }

  const estimatedPosition = view.posAtCoords({ x: event.clientX, y: event.clientY }, false);
  const estimatedLine = view.state.doc.lineAt(estimatedPosition);
  if (estimatedLine.number === line.number) {
    return Math.max(line.from, Math.min(line.to, estimatedPosition));
  }

  const leftEdge = view.coordsAtPos(line.from)?.left ?? view.contentDOM.getBoundingClientRect().left;
  const rightEdge = view.coordsAtPos(line.to)?.right ?? leftEdge;
  if (event.clientX <= leftEdge) return line.from;
  if (event.clientX >= rightEdge) return line.to;
  return line.from;
}

function shouldUseCustomPointerPlacement(view: EditorView, event: MouseEvent, target: HTMLElement | null) {
  const targetLine = getLineFromDomTarget(view, target);
  if (!targetLine) return true;
  if (targetLine.length === 0) return true;

  const precisePosition = view.posAtCoords({ x: event.clientX, y: event.clientY });
  if (precisePosition == null) return true;

  return view.state.doc.lineAt(precisePosition).number !== targetLine.number;
}

function notionMouseSelectionStyle(view: EditorView, event: MouseEvent) {
  const target = event.target as HTMLElement | null;
  if (target?.closest('.cm-block-handle, .cm-task-toggle, .cm-wikilink-existing, .cm-wikilink-missing')) {
    return null;
  }
  if (
    !target?.closest('.cm-line, .cm-content, .cm-scroller') ||
    !shouldUseCustomPointerPlacement(view, event, target)
  ) {
    return null;
  }

  const anchor = getLinePositionAtCoords(view, event, target);
  return {
    get(curEvent: MouseEvent, extend: boolean, multiple: boolean) {
      const head = getLinePositionAtCoords(view, curEvent, curEvent.target as HTMLElement | null);
      const range = anchor === head ? EditorSelection.cursor(head) : EditorSelection.range(anchor, head);

      if (extend) return view.state.selection.replaceRange(view.state.selection.main.extend(head));
      if (multiple) return view.state.selection.addRange(range);
      return EditorSelection.create([range]);
    },
    update() {
      return true;
    },
  };
}

const markdownBlockPlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;

    constructor(view: EditorView) {
      this.decorations = buildBlockDecorations(view);
    }

    update(update: ViewUpdate) {
      if (update.docChanged || update.viewportChanged || update.selectionSet) {
        this.decorations = buildBlockDecorations(update.view);
      }
    }
  },
  {
    decorations: (plugin) => plugin.decorations,
    eventHandlers: {
      mousedown(event, view) {
        const target = event.target as HTMLElement | null;
        if (target?.closest('.cm-block-handle')) return false;
        const toggle = target?.closest('.cm-task-toggle') as HTMLElement | null;
        if (!toggle?.dataset.line) return false;
        const line = view.state.doc.line(Number(toggle.dataset.line));
        const replacement = toggleTaskLine(line.text);
        if (replacement === line.text) return false;
        event.preventDefault();
        view.dispatch({
          changes: { from: line.from, to: line.to, insert: replacement },
          selection: { anchor: line.from + replacement.length },
        });
        view.focus();
        return true;
      },
      click(event, view) {
        const target = event.target as HTMLElement | null;
        if (target?.closest('.cm-wikilink-existing, .cm-wikilink-missing')) {
          view.focus();
        }
        return false;
      },
      dragover(event) {
        if (!event.dataTransfer?.types.includes('application/x-archivum-line')) return false;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'move';
        return true;
      },
      drop(event, view) {
        const source = Number(event.dataTransfer?.getData('application/x-archivum-line'));
        if (!source) return false;
        const position = view.posAtCoords({ x: event.clientX, y: event.clientY });
        if (position == null) return false;
        const target = view.state.doc.lineAt(position).number;
        const nextDoc = moveMarkdownBlockInText(view.state.doc.toString(), source, target);
        if (nextDoc === view.state.doc.toString()) return true;
        event.preventDefault();
        view.dispatch({
          changes: { from: 0, to: view.state.doc.length, insert: nextDoc },
          selection: { anchor: view.state.doc.line(Math.min(target, view.state.doc.lines)).from },
        });
        view.focus();
        return true;
      },
    },
  },
);

function moveCursorVertically(view: EditorView, forward: boolean) {
  const selection = view.state.selection.main;
  const range = view.moveVertically(selection, forward);
  view.dispatch({
    selection: EditorSelection.single(range.anchor, range.head),
    effects: EditorView.scrollIntoView(range.head, { y: 'nearest' }),
  });
  return true;
}

export function slashCommandCompletion(context: CompletionContext): CompletionResult | null {
  const before = context.matchBefore(/^\s*\/[^\n]*$/);
  if (!before) return null;
  const line = context.state.doc.lineAt(context.pos);
  if (before.from !== line.from) return null;

  const query = before.text.replace(/^\s*\//, '').split(/\s+/)[0] ?? '';
  const options = SLASH_COMMANDS.map((command) => ({
    command,
    score: matchSlashCommand(command, query),
  }))
    .filter(({ score }) => score > 0)
    .sort((a, b) => b.score - a.score || a.command.label.localeCompare(b.command.label))
    .map(({ command, score }) => ({
      label: command.label,
      detail: command.detail,
      boost: score,
      type: 'keyword',
      apply(view: EditorView) {
        const currentLine = view.state.doc.lineAt(view.state.selection.main.head);
        const replacement = applySlashCommandToLine(currentLine.text, command.id as SlashCommandId);
        view.dispatch({
          changes: { from: currentLine.from, to: currentLine.to, insert: replacement },
          selection: { anchor: currentLine.from + replacement.length },
        });
      },
    }));

  return {
    from: before.from,
    options,
    validFor: /^\s*\/[^\n]*$/,
  };
}

function handleSmartEnter(view: EditorView) {
  const selection = view.state.selection.main;
  if (!selection.empty) return false;
  const line = view.state.doc.lineAt(selection.head);
  if (selection.head !== line.to) return false;
  const next = getEnterInsertionForLine(line.text);
  if (!next) return false;

  const changes =
    next.replaceLine == null
      ? { from: selection.head, insert: next.insertion }
      : { from: line.from, to: line.to, insert: `${next.replaceLine}${next.insertion}` };
  const anchor =
    next.replaceLine == null
      ? selection.head + next.insertion.length
      : line.from + next.replaceLine.length + next.insertion.length;

  view.dispatch({ changes, selection: { anchor } });
  return true;
}

function moveCurrentBlock(view: EditorView, offset: -1 | 1) {
  const line = view.state.doc.lineAt(view.state.selection.main.head);
  const moved = moveMarkdownBlockByOffset(view.state.doc.toString(), line.number, offset);
  if (moved.text === view.state.doc.toString()) return true;
  view.dispatch({
    changes: { from: 0, to: view.state.doc.length, insert: moved.text },
    selection: { anchor: Math.min(view.state.doc.line(moved.lineNumber).from, moved.text.length) },
  });
  return true;
}

export function markdownBlockExtension(): Extension {
  return [
    Prec.high(EditorView.mouseSelectionStyle.of(notionMouseSelectionStyle)),
    Prec.high(keymap.of([
      { key: 'ArrowUp', run: (view) => moveCursorVertically(view, false) },
      { key: 'ArrowDown', run: (view) => moveCursorVertically(view, true) },
      { key: 'Enter', run: handleSmartEnter },
      { key: 'Mod-Shift-ArrowUp', run: (view) => moveCurrentBlock(view, -1) },
      { key: 'Mod-Shift-ArrowDown', run: (view) => moveCurrentBlock(view, 1) },
    ])),
    markdownBlockPlugin,
    EditorView.theme({
      '.cm-content': {
        fontFamily: "'Instrument Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      },
      '.cm-line.cm-markdown-block': {
        position: 'relative',
        padding: '3px 0',
        borderRadius: '5px',
        color: 'inherit',
      },
      '.cm-line.cm-markdown-block:hover': {
        backgroundColor: 'transparent',
      },
      '.cm-line.cm-activeLine': {
        backgroundColor: 'transparent',
      },
      '.cm-markdown-heading-1': {
        paddingTop: '22px',
        paddingBottom: '7px',
        fontSize: '1.875rem',
        lineHeight: '1.2',
        fontWeight: '700',
        color: 'inherit',
      },
      '.cm-markdown-heading-2': {
        paddingTop: '18px',
        paddingBottom: '6px',
        fontSize: '1.5rem',
        lineHeight: '1.25',
        fontWeight: '700',
        color: 'inherit',
      },
      '.cm-markdown-heading-3': {
        paddingTop: '14px',
        paddingBottom: '5px',
        fontSize: '1.1875rem',
        lineHeight: '1.3',
        fontWeight: '600',
        color: 'inherit',
      },
      '.cm-markdown-heading-4, .cm-markdown-heading-5, .cm-markdown-heading-6': {
        paddingTop: '10px',
        paddingBottom: '4px',
        fontSize: '1rem',
        lineHeight: '1.38',
        fontWeight: '600',
        color: 'inherit',
      },
      '.cm-markdown-paragraph': {
        fontSize: '1rem',
        lineHeight: '1.74',
      },
      '.cm-markdown-unordered-list, .cm-markdown-ordered-list, .cm-markdown-task-open, .cm-markdown-task-done': {
        fontSize: '1rem',
        lineHeight: '1.68',
      },
      '.cm-markdown-task-done': {
        color: 'hsl(var(--muted-foreground))',
      },
      '.cm-markdown-quote': {
        paddingTop: '6px',
        paddingBottom: '6px',
        paddingLeft: '14px',
        borderLeft: '3px solid hsl(var(--border) / 0.18)',
        color: 'hsl(var(--muted-foreground))',
        fontStyle: 'italic',
      },
      '.cm-markdown-code-fence': {
        color: 'hsl(var(--muted-foreground))',
        fontFamily: "'JetBrains Mono', 'Fira Code', Consolas, monospace",
        fontSize: '13px',
      },
      '.cm-markdown-thematic-break': {
        minHeight: '18px',
        paddingTop: '9px',
        borderTop: '0',
        color: 'transparent',
      },
      '.cm-markdown-marker': {
        display: 'inline-flex',
        minWidth: '22px',
        marginLeft: '-30px',
        marginRight: '8px',
        justifyContent: 'center',
        color: 'hsl(var(--muted-foreground))',
        fontWeight: '600',
        textDecoration: 'none',
        fontStyle: 'normal',
      },
      '.cm-task-toggle': {
        height: '20px',
        alignItems: 'center',
        border: '0',
        borderRadius: '4px',
        backgroundColor: 'transparent',
        cursor: 'pointer',
        padding: '0',
      },
      '.cm-task-toggle:hover': {
        backgroundColor: 'hsl(var(--foreground) / 0.07)',
        color: 'hsl(var(--foreground))',
      },
      '.cm-markdown-marker-heading-1, .cm-markdown-marker-heading-2, .cm-markdown-marker-heading-3, .cm-markdown-marker-heading-4, .cm-markdown-marker-heading-5, .cm-markdown-marker-heading-6, .cm-markdown-marker-quote, .cm-markdown-marker-thematic-break': {
        width: '0',
        minWidth: '0',
        margin: '0',
      },
      '.cm-markdown-marker-strong-marker, .cm-markdown-marker-emphasis-marker, .cm-markdown-marker-code-marker': {
        width: '0',
        minWidth: '0',
        margin: '0',
      },
      '.cm-markdown-marker-code-fence': {
        minWidth: '34px',
        marginLeft: '-44px',
        marginRight: '10px',
        borderRadius: '4px',
        backgroundColor: 'hsl(var(--foreground) / 0.06)',
        color: 'hsl(var(--muted-foreground))',
        fontSize: '10px',
        textTransform: 'uppercase',
      },
      '.cm-block-handle': {
        position: 'absolute',
        left: '-58px',
        top: '50%',
        display: 'inline-flex',
        height: '22px',
        width: '24px',
        transform: 'translateY(-50%)',
        alignItems: 'center',
        justifyContent: 'center',
        border: '0',
        borderRadius: '5px',
        background: 'transparent',
        color: 'hsl(var(--muted-foreground))',
        cursor: 'grab',
        fontSize: '13px',
        lineHeight: '1',
        opacity: '0',
        transition: 'opacity 120ms ease, background-color 120ms ease, color 120ms ease',
      },
      '.cm-line:hover .cm-block-handle, .cm-line.cm-activeLine .cm-block-handle': {
        opacity: '1',
      },
      '.cm-block-handle:hover': {
        backgroundColor: 'hsl(var(--foreground) / 0.06)',
        color: 'hsl(var(--foreground))',
      },
      '.cm-block-handle:active': {
        cursor: 'grabbing',
      },
    }),
  ];
}

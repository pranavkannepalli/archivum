import { describe, expect, it } from 'vitest';
import { classifyMarkdownLine, findInlineMarkdownMarks } from './markdownBlockExtension';

describe('classifyMarkdownLine', () => {
  it('identifies markdown blocks that should receive document-style editing treatments', () => {
    expect(classifyMarkdownLine('# Roadmap')).toMatchObject({
      kind: 'heading-1',
      marker: { from: 0, to: 2, label: '' },
    });
    expect(classifyMarkdownLine('- [x] Ship editor polish')).toMatchObject({
      kind: 'task-done',
      marker: { from: 0, to: 6, label: '☑' },
    });
    expect(classifyMarkdownLine('> Keep context visible')).toMatchObject({
      kind: 'quote',
      marker: { from: 0, to: 2, label: '' },
    });
    expect(classifyMarkdownLine('---')).toMatchObject({
      kind: 'thematic-break',
      marker: { from: 0, to: 3, label: '' },
    });
  });
});

describe('findInlineMarkdownMarks', () => {
  it('finds inline markdown delimiters that should be hidden in visual editing', () => {
    const markers = findInlineMarkdownMarks('This is **bold**, *emphasis*, and `code`.').filter(
      (mark) => mark.kind.endsWith('-marker'),
    );
    expect(markers).toEqual([
      { from: 8, to: 10, kind: 'strong-marker' },
      { from: 14, to: 16, kind: 'strong-marker' },
      { from: 18, to: 19, kind: 'emphasis-marker' },
      { from: 27, to: 28, kind: 'emphasis-marker' },
      { from: 34, to: 35, kind: 'code-marker' },
      { from: 39, to: 40, kind: 'code-marker' },
    ]);
  });

  it('spans the text between the delimiters so it can actually be styled', () => {
    // Hiding `**` without bolding what it wrapped is worse than showing the
    // raw markdown: the formatting becomes invisible rather than rendered.
    const content = findInlineMarkdownMarks('This is **bold**, *emphasis*, and `code`.').filter(
      (mark) => !mark.kind.endsWith('-marker'),
    );
    expect(content).toEqual([
      { from: 10, to: 14, kind: 'strong' },
      { from: 19, to: 27, kind: 'emphasis' },
      { from: 35, to: 39, kind: 'code' },
    ]);
  });

  it('keeps every range in document order for the decoration builder', () => {
    // CodeMirror's RangeSetBuilder rejects out-of-order ranges outright.
    const marks = findInlineMarkdownMarks('a **b** c *d* e `f`');
    const starts = marks.map((mark) => mark.from);
    expect(starts).toEqual([...starts].sort((left, right) => left - right));
  });
});

// @vitest-environment jsdom
// DOMPurify needs a real DOM; sanitising is part of what this module promises,
// so the test exercises it rather than stubbing it out.
import { describe, expect, it } from 'vitest';
import { renderMarkdown } from './markdown';

describe('renderMarkdown', () => {
  it('renders the block types three hand-rolled regex renderers all missed', () => {
    // Tables, nested lists and task lists are ordinary markdown that every
    // previous renderer dropped or mangled — a table came out as paragraphs of
    // pipes, and a nested list flattened to one level.
    const html = renderMarkdown('| a | b |\n| - | - |\n| 1 | 2 |');
    expect(html).toContain('<table');
    expect(html).toContain('<td>1</td>');
  });

  it('nests lists instead of flattening them', () => {
    const html = renderMarkdown('- one\n  - deeper\n- two');
    expect(html.match(/<ul/g)).toHaveLength(2);
  });

  it('wraps list items in a list', () => {
    // The old renderers emitted bare <li> with no <ul> around them, so every
    // list was invalid markup that browsers rendered without indentation.
    const html = renderMarkdown('- one\n- two');
    expect(html.startsWith('<ul')).toBe(true);
  });

  it('renders task lists as checkboxes', () => {
    const html = renderMarkdown('- [x] done\n- [ ] open');
    expect(html).toContain('type="checkbox"');
    expect(html).toContain('checked');
  });

  it('keeps the language on a fenced code block', () => {
    const html = renderMarkdown('```python\nx = 1\n```');
    expect(html).toContain('language-python');
  });

  it('links wikilinks into the vault by default', () => {
    const html = renderMarkdown('see [[projects/perceo/archivum]]');
    expect(html).toContain('href="/wiki/projects/perceo/archivum"');
  });

  it('supports the piped wikilink form, showing the label not the slug', () => {
    const html = renderMarkdown('see [[projects/perceo/archivum|Archivum]]');
    expect(html).toContain('>Archivum<');
    expect(html).toContain('href="/wiki/projects/perceo/archivum"');
  });

  it('renders wikilinks as plain text for readers who cannot open the vault', () => {
    // A share recipient following a /wiki/ link lands on a login wall.
    const html = renderMarkdown('see [[secrets/plan]]', { wikilinks: 'text' });
    expect(html).not.toContain('href');
    expect(html).toContain('secrets/plan');
  });

  it('strips script tags rather than trusting the source', () => {
    const html = renderMarkdown('<script>alert(1)</script>\n\nhello');
    expect(html).not.toContain('<script');
    expect(html).toContain('hello');
  });

  it('strips event handlers from inline html', () => {
    const html = renderMarkdown('<img src=x onerror="alert(1)">');
    expect(html).not.toContain('onerror');
  });

  it('leaves a javascript: url unusable', () => {
    const html = renderMarkdown('[click](javascript:alert(1))');
    expect(html).not.toContain('javascript:');
  });
});

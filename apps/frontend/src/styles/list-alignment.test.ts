import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * Two ways a bullet ends up in the wrong place, both of which happened.
 *
 * These are CSS assertions rather than rendered measurements, which is a real
 * limitation — they check the declarations that were wrong, not the layout. But
 * both failures were silent and neither is visible in a unit test otherwise.
 */
const css = fs.readFileSync(path.join(__dirname, 'shell.css'), 'utf8');

function block(selector: string): string {
  const index = css.indexOf(selector);
  expect(index, `${selector} is missing from shell.css`).toBeGreaterThan(-1);
  return css.slice(index, css.indexOf('}', index));
}

describe('editor list alignment', () => {
  it('does not pull the first line out with a negative text-indent', () => {
    // `text-indent: -26px` moves only the first line, so the bullet ended up
    // outside the document's left edge while every other block began at 2px.
    const rule = block('.cm-markdown-unordered-list,');
    expect(rule).not.toMatch(/text-indent:\s*-/);
    expect(rule).toMatch(/padding-left:/);
  });

  it('reserves the gutter with a negative margin on the marker itself', () => {
    expect(block('.cm-markdown-marker-unordered-list,')).toMatch(/margin-left:\s*-/);
  });
});

describe('rendered markdown lists', () => {
  it('asks for list markers back, because preflight removes them', () => {
    // Tailwind's preflight sets `list-style: none` on every ul and ol. Without
    // restoring it a rendered list is indented text with no bullets at all.
    expect(block('.md-body ul, .shared-doc .body ul {')).toMatch(/list-style:\s*disc/);
    expect(block('.md-body ol, .shared-doc .body ol {')).toMatch(/list-style:\s*decimal/);
  });

  it('does not shift task list items left of every other item', () => {
    const rule = block(".md-body li:has(> input[type='checkbox']),");
    expect(rule).not.toMatch(/margin-left:\s*-/);
  });
});

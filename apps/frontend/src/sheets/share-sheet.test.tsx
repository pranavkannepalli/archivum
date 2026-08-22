// @vitest-environment jsdom
// The markdown cases sanitise through DOMPurify, which needs a DOM.
import { describe, expect, it, vi } from 'vitest';
import { renderToString } from 'react-dom/server';
import { StaticRouter } from 'react-router-dom/server';
import ShareSheet from './ShareSheet';
import SharePage from '../pages/SharePage';
import { renderMarkdown } from '../pages/markdown';

/**
 * Rendered server-side, so effects do not run: these cover the shape a person
 * sees before any data arrives, plus the rules that must hold regardless of
 * data. The grant semantics themselves live in the backend suite.
 */

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    listShareAccess: vi.fn(async () => []),
    createSharePrincipal: vi.fn(),
    createShareGrant: vi.fn(),
    revokeShareGrant: vi.fn(),
    openShareLink: vi.fn(),
    getSharedResource: vi.fn(),
    commentOnShare: vi.fn(),
  };
});

function render(node: React.ReactNode, location = '/') {
  return renderToString(<StaticRouter location={location}>{node}</StaticRouter>);
}

describe('the share sheet', () => {
  const target = { resource_kind: 'entry' as const, resource_id: 'people/alice' };

  it('renders nothing while closed', () => {
    const html = render(
      <ShareSheet
        open={false}
        target={target}
        resourceTitle="Alice"
        onClose={() => {}}
      />,
    );
    expect(html).toBe('');
  });

  it('uses the same sheet chrome as capture and ask', () => {
    const html = render(
      <ShareSheet open target={target} resourceTitle="Alice" onClose={() => {}} />,
    );
    expect(html).toContain('class="overlay on"');
    expect(html).toContain('class="sheet"');
    expect(html).toContain('sheet-foot');
  });

  it('offers both roles, defaulting to read-only', () => {
    const html = render(
      <ShareSheet open target={target} resourceTitle="Alice" onClose={() => {}} />,
    );
    expect(html).toContain('Can read');
    expect(html).toContain('Can suggest');
  });

  it('says plainly that nobody has access yet', () => {
    const html = render(
      <ShareSheet open target={target} resourceTitle="Alice" onClose={() => {}} />,
    );
    expect(html).toContain('Only you can see this');
  });

  it('starts with the public link off', () => {
    const html = render(
      <ShareSheet open target={target} resourceTitle="Alice" onClose={() => {}} />,
    );
    expect(html).toContain('Anyone with the link');
    expect(html).toContain('off — link does nothing');
    // The toggle must not render in its "on" state before a link grant exists.
    expect(html).not.toContain('class="toggle on"');
  });

  it('names how many cited sources are being left behind', () => {
    const html = render(
      <ShareSheet
        open
        target={target}
        resourceTitle="Alice"
        citedCount={3}
        onClose={() => {}}
      />,
    );
    // SSR splits interpolated text with comment markers, so match on the
    // stripped text rather than the raw markup.
    const text = html.replace(/<!--[\s\S]*?-->/g, '').replace(/<[^>]+>/g, '');
    expect(text).toContain('3 sources cited · not shared');
  });
});

describe('the recipient viewer', () => {
  it('shows a loading state rather than an error before data arrives', () => {
    const html = render(<SharePage />, '/share/some-token');
    expect(html).toContain('skeleton');
  });
});

describe('shared markdown', () => {
  const shared = (source: string) => renderMarkdown(source, { wikilinks: 'text' });

  it('strips the dangerous part of inline html rather than the whole tag', () => {
    // The old renderer escaped every `<`, which also killed legitimate markup.
    // Sanitising keeps the image and drops what makes it an attack.
    const html = shared('<img src=x onerror=alert(1)>');
    expect(html).not.toContain('onerror');
  });

  it('renders wikilinks as plain text, since the reader has no vault to open', () => {
    const html = shared('See [[Deploy policy]] for details.');
    expect(html).toContain('wikilink-plain');
    expect(html).not.toContain('href');
    expect(html).toContain('Deploy policy');
  });

  it('uses theme classes instead of hard-coded colours', () => {
    const html = shared('> a quote\n\n## Heading');
    expect(html).toContain('<blockquote>');
    expect(html).toContain('<h2>Heading</h2>');
    expect(html).not.toContain('style=');
  });

  it('keeps external links but marks them safe', () => {
    const html = shared('[docs](https://example.com)');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(html).toContain('target="_blank"');
  });
});

import { describe, expect, it, vi } from 'vitest';
import { renderToString } from 'react-dom/server';
import { StaticRouter } from 'react-router-dom/server';
import fs from 'node:fs';
import path from 'node:path';
import { AppProvider } from '../store';
import { ToastProvider } from '../components/ui/Toast';
import SelfSurface from './SelfSurface';
import VisualizedSurface from './VisualizedSurface';

/**
 * Replaces the old memory-page tests. Memory is no longer a section: it lives
 * on `person:self` and on the entry it was distilled from.
 *
 * These render server-side, so effects do not run — they cover the pre-data
 * states, which are the ones a user actually waits on. The data contracts
 * themselves are covered in memory-api.test.ts.
 */

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    getOwner: vi.fn(async () => {
      throw new Error('not called during SSR');
    }),
  };
});

function render(node: React.ReactNode, location = '/me') {
  return renderToString(
    <StaticRouter location={location}>
      <AppProvider>
        <ToastProvider>{node}</ToastProvider>
      </AppProvider>
    </StaticRouter>,
  );
}

describe('the self surface', () => {
  it('waits on the profile rather than showing an empty shell', () => {
    expect(render(<SelfSurface />)).toContain('Loading your profile');
  });

  it('asks for memory by owner, not by scope', () => {
    // `person:self` is the owner of every asset and the scope of none, so
    // filtering on scope guaranteed an empty section under a non-zero count.
    const source = fs.readFileSync(path.resolve('src/surfaces/SelfSurface.tsx'), 'utf8');

    expect(source).toContain("listMemoryAssets({ owner: SELF_OWNER })");
    expect(source).not.toContain('scope: SELF_SCOPE');
  });
});

describe('the visualized surface', () => {
  it('renders its tabs and tiles before data arrives', () => {
    const html = render(<VisualizedSurface />, '/visualized');

    expect(html).toContain('Visualized');
    expect(html).toContain('Memory flow');
    expect(html).toContain('Waiting on you');
    // Unknown counts read as a dash, never as a plausible-looking zero.
    expect(html).toContain('—');
  });
});

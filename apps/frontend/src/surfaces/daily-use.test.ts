import { afterEach, describe, expect, it, vi } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

const fetchMock = vi.fn();
vi.stubGlobal('fetch', fetchMock);
vi.stubGlobal('document', { cookie: '' });

afterEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal('document', { cookie: '' });
});

function jsonResponse(body: unknown) {
  return { ok: true, json: async () => body } as Response;
}

describe('finding things', () => {
  it('searches the vault semantically rather than by title substring', async () => {
    // The box filtered titles client-side while Qdrant, embeddings and a
    // hybrid endpoint sat unused, so anything you could not name was lost.
    const { searchVault } = await import('../api');
    fetchMock.mockResolvedValueOnce(
      jsonResponse([{ slug: 'a', title: 'A', excerpt: '…', score: 0.9 }]),
    );

    const hits = await searchVault('retry backoff');

    expect(hits[0].slug).toBe('a');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/search?q=retry%20backoff&limit=20');
  });

  it('the everything surface uses it', () => {
    const source = fs.readFileSync(
      path.resolve('src/surfaces/EverythingSurface.tsx'),
      'utf8',
    );
    expect(source).toContain('searchVault');
  });
});

describe('today', () => {
  it('opens or creates the daily note', async () => {
    const { ensureDailyNote } = await import('../api');
    fetchMock.mockResolvedValueOnce(jsonResponse({ slug: 'daily/2026-08-20', title: 'Daily' }));

    const page = await ensureDailyNote();

    expect(page.slug).toBe('daily/2026-08-20');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/life/daily');
  });

  it('is reachable from the shell', () => {
    // The endpoint existed and nothing opened it, so there was no "today".
    const source = fs.readFileSync(path.resolve('src/shell/AppShell.tsx'), 'utf8');
    expect(source).toContain('ensureDailyNote');
  });
});

describe('tasks', () => {
  it('can be ticked off', async () => {
    const { toggleTask } = await import('../api');
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ text: 'Ship it', slug: 'daily/today', page_title: 'Today', line: 1 }),
    );

    await toggleTask({ slug: 'daily/today', line: 1, done: true });

    expect(fetchMock.mock.calls[0][0]).toBe('/api/tasks/toggle');
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
  });

  it('the stream shows what is still open', () => {
    const source = fs.readFileSync(path.resolve('src/surfaces/StreamSurface.tsx'), 'utf8');
    expect(source).toContain('open_tasks');
    expect(source).toContain('toggleTask');
  });
});

describe('the stream shows the work', () => {
  it('renders sessions and fixes', () => {
    // Capture is automatic; capture you cannot see reads as no capture.
    const source = fs.readFileSync(path.resolve('src/surfaces/StreamSurface.tsx'), 'utf8');
    expect(source).toContain("case 'fix'");
    expect(source).toContain("case 'session'");
  });
});

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

/**
 * Indexing and governance now happen on every write, so these two endpoints are
 * the backfill for a vault that predates that. They were unreachable from the
 * app, which meant an existing vault could never catch up.
 */
describe('vault repair client', () => {
  it('can reindex the whole vault from disk', async () => {
    const { reindexVault } = await import('../api');
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ pages: 3, actions: { indexed: 3 }, degraded: [] }),
    );

    const report = await reindexVault({ force: true });

    expect(report.pages).toBe(3);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/system/reindex?force=true');
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
  });

  it('defaults to a non-forced pass', async () => {
    const { reindexVault } = await import('../api');
    fetchMock.mockResolvedValueOnce(jsonResponse({ pages: 0, actions: {}, degraded: [] }));

    await reindexVault();

    expect(fetchMock.mock.calls[0][0]).toBe('/api/system/reindex');
  });
});

describe('the settings page', () => {
  it('offers the repair pass, so a pre-existing vault can catch up', () => {
    const source = fs.readFileSync(path.resolve('src/pages/SettingsPage.tsx'), 'utf8');

    expect(source).toContain('VaultRepair');
  });
});

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  bindMemoryAsset,
  catalogMemoryAssets,
  distillSource,
  getAgentLoadout,
  getGraphAudit,
  getGraphPath,
  listMemoryAssets,
  setMemoryAssetStatus,
  unbindMemoryAsset,
  type GraphReport,
  type LoadoutPackage,
  type MemoryAsset,
} from './api';

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

const asset: MemoryAsset = {
  id: 'memory:chat:src1',
  wiki_id: 'default',
  asset_type: 'chat',
  layer: 'L1',
  name: 'Session memory',
  owner: 'person:self',
  scope: 'wiki:default',
  status: 'active',
  visibility: 'private',
  version: 1,
  page_slug: 'memory/sessions/s1',
  summary: '2 atoms',
  body: '# Session',
  tags: ['chat', 'memory'],
  metadata: {},
  citations: [],
  approved_by: null,
  reviewed_at: null,
  supersedes: [],
  superseded_by: [],
  conflict_lineage: [],
  retired_at: null,
  created_at: '2026-08-12T00:00:00Z',
  updated_at: '2026-08-12T00:00:00Z',
};

describe('memory asset client', () => {
  it('passes filters through as query params', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([asset]));
    const result = await listMemoryAssets({ asset_type: 'chat', status: 'active' });
    expect(result[0].id).toBe('memory:chat:src1');
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/memory/assets?asset_type=chat&status=active',
    );
  });

  it('omits the query string when no filters are given', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([]));
    await listMemoryAssets();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/memory/assets');
  });

  it('can ask for memory by owner, which is how the profile page reads it', async () => {
    // Assets are owned by person:self but scoped to a wiki. Filtering on scope
    // matches nothing, which is why /me used to render permanently empty.
    fetchMock.mockResolvedValueOnce(jsonResponse([asset]));
    await listMemoryAssets({ owner: 'person:self' });
    expect(fetchMock.mock.calls[0][0]).toBe('/api/memory/assets?owner=person%3Aself');
  });

  it('encodes colon-bearing asset ids in status transitions', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...asset, status: 'archived' }));
    const updated = await setMemoryAssetStatus('memory:chat:src1', 'archived');
    expect(updated.status).toBe('archived');
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/memory/assets/memory%3Achat%3Asrc1/status',
    );
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
  });

  it('defaults a binding to an always-on priority', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        agent_key: 'coder',
        asset_id: 'memory:chat:src1',
        mode: 'always',
        priority: 100,
      }),
    );
    await bindMemoryAsset('coder', { asset_id: 'memory:chat:src1' });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      asset_id: 'memory:chat:src1',
      mode: 'always',
      priority: 100,
    });
  });

  it('deletes a binding by agent and asset', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ removed: true }));
    const result = await unbindMemoryAsset('coder', 'memory:chat:src1');
    expect(result.removed).toBe(true);
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/memory/agents/coder/bindings/memory%3Achat%3Asrc1',
    );
    expect(fetchMock.mock.calls[0][1].method).toBe('DELETE');
  });

  it('sends the session query when resolving a loadout', async () => {
    const loadout: LoadoutPackage = {
      agent_key: 'coder',
      query: 'deploy',
      entries: [{ asset, mode: 'on_demand', priority: 10, reason: 'Query match 0.50' }],
      citations: [],
      insufficient_evidence: true,
      reason: 'Loadout assets carry no citations; treat their content as unverified.',
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(loadout));
    const result = await getAgentLoadout('coder', 'deploy stack');
    expect(result.entries[0].mode).toBe('on_demand');
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/memory/agents/coder/loadout?query=deploy%20stack',
    );
  });

  it('posts an empty catalog request', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        wiki_assets: 3,
        source_assets: 1,
        codegraph_assets: 0,
        asset_ids: ['page:default:notes'],
      }),
    );
    const report = await catalogMemoryAssets();
    expect(report.wiki_assets).toBe(3);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/memory/catalog');
    expect(fetchMock.mock.calls[0][1].method).toBe('POST');
  });

  it('posts a distillation request', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        source_id: 'src1',
        session_id: 's1',
        scope: 'wiki:default',
        atoms_total: 2,
        atoms_accepted: 2,
        atoms_pending_review: 0,
        asset_ids: ['memory:chat:src1'],
        scenario_id: null,
        persona_updated: false,
        skill_id: null,
        skill_reason: 'No reusable procedure',
        pages_written: [],
      }),
    );
    const report = await distillSource({ source_id: 'src1', write_pages: false });
    expect(report.atoms_accepted).toBe(2);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      source_id: 'src1',
      write_pages: false,
    });
  });
});

describe('graph audit client', () => {
  const report: GraphReport = {
    scope: 'wiki:default',
    node_count: 6,
    edge_count: 7,
    by_kind: { page: 6 },
    by_extraction_method: { EXTRACTED: 6 },
    self_cited_ids: [],
    low_confidence_ids: [],
    orphan_ids: [],
    communities: [{ id: 'a1', label: 'Alpha', size: 3, member_ids: ['a1', 'a2', 'a3'] }],
    surprising_links: [],
    narrative: ['The graph holds 6 records.'],
    // The report names its own records, so whatever draws it needs no second call.
    edges: [{ source: 'a1', target: 'a2', relation: 'references', extraction_method: 'EXTRACTED' }],
    node_labels: { a1: 'Alpha', a2: 'Beta', a3: 'Gamma' },
    node_kinds: { a1: 'page', a2: 'page', a3: 'page' },
  };

  it('requests the audit with a surprise limit', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(report));
    const result = await getGraphAudit(5);
    expect(result.communities[0].size).toBe(3);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/graph/audit?surprise_limit=5');
  });

  it('encodes both endpoints when resolving a path', async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        source: 'person:self',
        target: 'page:default:a',
        found: true,
        length: 1,
        steps: [{ from_id: 'person:self', to_id: 'page:default:a', relation: 'remembers' }],
        reason: null,
      }),
    );
    const path = await getGraphPath('person:self', 'page:default:a');
    expect(path.found).toBe(true);
    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/graph/path?source=person%3Aself&target=page%3Adefault%3Aa',
    );
  });
});

describe('memory asset filters', () => {
  it('scopes assets to a single owner scope', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([asset]));

    await listMemoryAssets({ scope: 'person:self' });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/memory/assets?scope=person%3Aself',
      expect.anything(),
    );
  });

  it('scopes assets to the page they were distilled from', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([asset]));

    await listMemoryAssets({ page_slug: 'topics/retrieval/design' });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/memory/assets?page_slug=topics%2Fretrieval%2Fdesign',
      expect.anything(),
    );
  });
});

import type {
  Page,
  Folder,
  FolderMutationResult,
  SearchResult,
  GraphNode,
  GraphEdge,
  IngestLog,
  IngestSocketMessage,
} from './types';

const BASE = '';

function getCookie(name: string): string | undefined {
  const match = document.cookie
    .split(';')
    .map((c) => c.trim())
    .find((c) => c.startsWith(`${name}=`));
  if (!match) return undefined;
  return decodeURIComponent(match.substring(name.length + 1));
}

function csrfToken(): string | undefined {
  return getCookie('csrf_token');
}

function shouldSendCsrf(method?: string): boolean {
  const m = (method ?? 'GET').toUpperCase();
  return m === 'POST' || m === 'PUT' || m === 'PATCH' || m === 'DELETE';
}

function encodeSlugPath(slug: string): string {
  // Keep '/' as path separators but encode each segment safely.
  return slug
    .split('/')
    .map((seg) => encodeURIComponent(seg))
    .join('/');
}

function extractErrorMessage(body: unknown): string | null {
  if (typeof body === 'string') return body.trim() || null;
  if (!body || typeof body !== 'object') return null;

  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object') {
    const nested = detail as { detail?: unknown; message?: unknown; code?: unknown };
    if (typeof nested.detail === 'string') return nested.detail;
    if (typeof nested.message === 'string') return nested.message;
    if (typeof nested.code === 'string') return nested.code.replace(/_/g, ' ');
  }

  const message = (body as { message?: unknown; error?: unknown }).message
    ?? (body as { error?: unknown }).error;
  if (typeof message === 'string') return message;
  return null;
}

async function responseErrorMessage(res: Response): Promise<string> {
  const fallback = res.statusText || `HTTP ${res.status}`;
  const contentType = res.headers.get('content-type') ?? '';
  try {
    if (contentType.includes('application/json')) {
      const json = await res.json();
      return extractErrorMessage(json) ?? fallback;
    }
    const text = await res.text();
    return extractErrorMessage(text) ?? fallback;
  } catch {
    return fallback;
  }
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const method = (init?.method ?? 'GET').toUpperCase();
  const res = await fetch(`${BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
      ...(shouldSendCsrf(method)
        ? csrfToken()
          ? { 'X-CSRF-Token': csrfToken() }
          : {}
        : {}),
    },
  });
  if (!res.ok) {
    throw new Error(await responseErrorMessage(res));
  }
  return res;
}

export type CreatePageInput = {
  title: string;
  content?: string;
  tags?: string[];
  slug?: string;
};

export type UpdatePageInput = {
  title?: string | null;
  content?: string | null;
  tags?: string[] | null;
};

export type AuthSession = {
  username: string;
  role: string;
  wiki_id: string;
};

export async function login(password: string): Promise<void> {
  await apiFetch('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  });
}

export async function refreshSession(): Promise<AuthSession> {
  const res = await apiFetch('/api/auth/refresh', { method: 'POST' });
  return res.json();
}

export async function logout(): Promise<void> {
  await apiFetch('/api/auth/logout', { method: 'POST' });
}

export async function listPages(): Promise<Page[]> {
  const res = await apiFetch('/api/pages');
  return res.json();
}

export async function getPage(slug: string): Promise<Page> {
  const res = await apiFetch(`/api/pages/${encodeSlugPath(slug)}`);
  return res.json();
}

export async function updatePage(slug: string, input: UpdatePageInput): Promise<Page> {
  const res = await apiFetch(`/api/pages/${encodeSlugPath(slug)}`, {
    method: 'PUT',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function createPage(input: CreatePageInput): Promise<Page> {
  const res = await apiFetch('/api/pages', {
    method: 'POST',
    body: JSON.stringify({
      title: input.title,
      content: input.content ?? '',
      tags: input.tags ?? [],
      slug: input.slug,
    }),
  });
  return res.json();
}

export interface ReindexResult {
  slug: string;
  action: 'indexed' | 'unchanged' | 'removed' | 'missing';
  degraded: string[];
}

/**
 * Re-read this page from disk and rebuild everything derived from it.
 *
 * The vault is plain markdown you can edit with any tool, so this is the
 * manual counterpart to the watcher: the file is the truth, and the indexes
 * are being told to catch up.
 */
export async function reindexPage(slug: string): Promise<ReindexResult> {
  const res = await apiFetch(`/api/pages/${encodeSlugPath(slug)}/reindex`, { method: 'POST' });
  return res.json();
}

export interface VaultReindexReport {
  pages: number;
  actions: Record<string, number>;
  degraded: string[];
}

/**
 * Re-read the whole vault from disk.
 *
 * Every write already indexes itself and the watcher catches external edits, so
 * this is the repair pass: `force` rebuilds projections that were lost rather
 * than reacting to content that changed.
 */
export async function reindexVault(
  options: { force?: boolean } = {},
): Promise<VaultReindexReport> {
  const suffix = options.force ? '?force=true' : '';
  const res = await apiFetch(`/api/system/reindex${suffix}`, { method: 'POST' });
  return res.json();
}

export interface DerivedRecord {
  id: string;
  kind: string;
  label: string;
  slug: string | null;
  confidence: number;
}

export interface DerivedResponse {
  source_id: string;
  records: DerivedRecord[];
  pages: number;
}

/** What a source actually produced, walked through its provenance citations. */
export async function listSourceDerived(sourceId: string): Promise<DerivedResponse> {
  const res = await apiFetch(`/api/sources/${encodeSlugPath(sourceId)}/derived`);
  return res.json();
}

export async function deletePage(slug: string): Promise<void> {
  await apiFetch(`/api/pages/${encodeSlugPath(slug)}`, { method: 'DELETE' });
}

export async function movePage(slug: string, input: { new_slug: string }): Promise<Page> {
  const res = await apiFetch(`/api/pages/${encodeSlugPath(slug)}/move`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function duplicatePage(
  slug: string,
  input: { new_slug: string; title?: string },
): Promise<Page> {
  const res = await apiFetch(`/api/pages/${encodeSlugPath(slug)}/duplicate`, {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function listFolders(): Promise<Folder[]> {
  const res = await apiFetch('/api/folders');
  return res.json();
}

export async function createFolder(input: { path: string }): Promise<Folder> {
  const res = await apiFetch('/api/folders', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function renameFolder(
  path: string,
  input: { name: string; recursive?: boolean },
): Promise<FolderMutationResult> {
  const res = await apiFetch(`/api/folders/${encodeSlugPath(path)}`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function moveFolder(
  path: string,
  input: { new_path: string; recursive?: boolean },
): Promise<FolderMutationResult> {
  const res = await apiFetch(`/api/folders/${encodeSlugPath(path)}`, {
    method: 'PATCH',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function deleteFolder(
  path: string,
  input: { recursive?: boolean } = {},
): Promise<FolderMutationResult> {
  const recursive = input.recursive ? 'true' : 'false';
  const res = await apiFetch(`/api/folders/${encodeSlugPath(path)}?recursive=${recursive}`, {
    method: 'DELETE',
  });
  return res.json();
}

export async function getBacklinks(slug: string): Promise<Page[]> {
  const res = await apiFetch(`/api/pages/${encodeSlugPath(slug)}/backlinks`);
  return res.json();
}

export type SharePage = {
  type: string;
  token: string;
  wiki_id: string;
  slug: string | null;
  title: string | null;
  content: string | null;
  tags: string[];
  question: string | null;
  answer: string | null;
  citations: Array<{ slug: string; title: string }>;
  expires_at: string | null;
};

export async function getShare(token: string): Promise<SharePage> {
  // Share tokens are url-safe base64-ish; still encode defensively.
  const res = await apiFetch(`/api/share/${encodeURIComponent(token)}`);
  return res.json();
}

export type ShareLinkInfo = {
  id: number;
  token: string;
  type: string;
  target_id: string | null;
  created_at: string;
  expires_at: string | null;
  revoked: number;
};

export type CreateShareLinkInput = {
  type: 'page' | 'query';
  target_id?: string | null;
  question?: string;
  answer?: string;
  citations?: Array<{ slug: string; title: string }>;
  expires_in_days?: number | null;
};

export async function createShareLink(
  input: CreateShareLinkInput,
): Promise<{ token: string; url: string; expires_at: string | null }> {
  const res = await apiFetch('/api/share-links', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function listShareLinks(): Promise<ShareLinkInfo[]> {
  const res = await apiFetch('/api/share-links');
  return res.json();
}

export async function revokeShareLink(token: string): Promise<void> {
  await apiFetch(`/api/share-links/${token}`, { method: 'DELETE' });
}

// ── Sharing ──────────────────────────────────────────────────────────────────
//
// A grant is the primitive: sharing with a person and sharing by link are the
// same record with a different subject. `/api/sharing/*` is owner-side
// management; `/api/shared/*` is what a recipient reads.

export type ShareRole = 'viewer' | 'commenter';

export type SharePrincipal = {
  id: string;
  wiki_id: string;
  display_name: string;
  claimed_at: string | null;
  revoked: boolean;
  created_at: string;
};

export type ShareGrant = {
  id: string;
  wiki_id: string;
  subject_kind: 'principal' | 'link';
  subject_id: string;
  resource_urn: string;
  role: ShareRole;
  include_cited: boolean;
  created_by: string;
  created_at: string;
  expires_at: string | null;
  revoked: boolean;
};

export type ShareAccessRow = {
  id: string;
  resource_urn: string;
  role: ShareRole;
  subject_kind: 'principal' | 'link';
  display_name: string | null;
  created_at: string;
  expires_at: string | null;
};

export type ShareHold = {
  grant_id: string;
  resource_urn: string;
  grant_urn: string;
  reason: string;
  role: ShareRole;
  display_name: string | null;
  created_at: string;
};

/**
 * A resource is named by kind and id, never by urn.
 *
 * The wiki segment of a urn is filled in server-side from the session, so the
 * browser never has to know its own tenant id to share the page it is showing.
 */
export type ShareTarget = {
  resource_kind: 'entry' | 'folder' | 'asset' | 'scope' | 'view' | 'source';
  resource_id: string;
};

export function entryTarget(slug: string): ShareTarget {
  return { resource_kind: 'entry', resource_id: slug };
}

export function folderTarget(path: string): ShareTarget {
  return { resource_kind: 'folder', resource_id: path };
}

export async function createSharePrincipal(
  displayName: string,
): Promise<{ principal: SharePrincipal; claim_token: string; claim_url: string }> {
  const res = await apiFetch('/api/sharing/principals', {
    method: 'POST',
    body: JSON.stringify({ display_name: displayName }),
  });
  return res.json();
}

export async function listSharePrincipals(): Promise<SharePrincipal[]> {
  const res = await apiFetch('/api/sharing/principals');
  return res.json();
}

export async function revokeSharePrincipal(principalId: string): Promise<void> {
  await apiFetch(`/api/sharing/principals/${encodeURIComponent(principalId)}`, {
    method: 'DELETE',
  });
}

export type CreateGrantInput = ShareTarget & {
  principal_id?: string;
  subject_kind?: 'principal' | 'link';
  role?: ShareRole;
  expires_in_days?: number | null;
  include_cited?: boolean;
};

export async function createShareGrant(input: CreateGrantInput): Promise<{
  grant: ShareGrant;
  share_token: string | null;
  share_url: string | null;
}> {
  const res = await apiFetch('/api/sharing/grants', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function listShareAccess(target: ShareTarget): Promise<ShareAccessRow[]> {
  const query = new URLSearchParams({
    resource_kind: target.resource_kind,
    resource_id: target.resource_id,
  });
  const res = await apiFetch(`/api/sharing/grants?${query.toString()}`);
  return res.json();
}

export async function revokeShareGrant(grantId: string): Promise<void> {
  await apiFetch(`/api/sharing/grants/${encodeURIComponent(grantId)}`, {
    method: 'DELETE',
  });
}

export async function listShareHolds(): Promise<ShareHold[]> {
  const res = await apiFetch('/api/sharing/holds');
  return res.json();
}

export async function approveShareHold(
  grantId: string,
  resourceUrn: string,
): Promise<void> {
  await apiFetch(`/api/sharing/holds/${encodeURIComponent(grantId)}/approve`, {
    method: 'POST',
    body: JSON.stringify({ resource_urn: resourceUrn }),
  });
}

// ── Recipient side ───────────────────────────────────────────────────────────

export type SharedCitation = { title: string; urn: string | null };

export type SharedListing = {
  urn: string;
  kind: string;
  title: string;
  role: ShareRole;
};

export type SharedResource = {
  urn: string;
  kind: string;
  role: ShareRole;
  title: string;
  body: string;
  tags: string[];
  citations: SharedCitation[];
  children: SharedListing[];
  may_comment: boolean;
  shared_by_inheritance: string | null;
};

export async function claimShare(
  claimToken: string,
): Promise<{ principal_id: string; display_name: string; wiki_id: string }> {
  const res = await apiFetch('/api/shared/claim', {
    method: 'POST',
    body: JSON.stringify({ claim_token: claimToken }),
  });
  return res.json();
}

export async function openShareLink(token: string): Promise<SharedResource> {
  const res = await apiFetch(`/api/shared/by-token/${encodeURIComponent(token)}`);
  return res.json();
}

export async function getSharedResource(
  urn: string,
  token?: string,
): Promise<SharedResource> {
  const query = new URLSearchParams({ urn });
  if (token) query.set('token', token);
  const res = await apiFetch(`/api/shared/resource?${query.toString()}`);
  return res.json();
}

export async function listSharedWithMe(): Promise<SharedListing[]> {
  const res = await apiFetch('/api/shared');
  return res.json();
}

export async function commentOnShare(urn: string, text: string): Promise<void> {
  await apiFetch('/api/shared/comment', {
    method: 'POST',
    body: JSON.stringify({ urn, text }),
  });
}

export type PublicPageSummary = {
  slug: string;
  title: string;
  tags: string[];
  updated_at: string;
};

export type PublicPage = PublicPageSummary & {
  content: string;
};

export async function listPublicPages(): Promise<PublicPageSummary[]> {
  const res = await apiFetch('/api/public/pages');
  return res.json();
}

export async function getPublicPage(slug: string): Promise<PublicPage> {
  const res = await apiFetch(`/api/public/pages/${encodeSlugPath(slug)}`);
  return res.json();
}

export async function search(query: string): Promise<SearchResult[]> {
  const res = await apiFetch(`/api/search?q=${encodeURIComponent(query)}`);
  return res.json();
}

export async function getGraph(): Promise<{
  nodes: GraphNode[];
  edges: GraphEdge[];
  // 'demo' means the graph store was unreachable and this is fixture data.
  // Surface it: an unlabelled fake graph is indistinguishable from a real one.
  source?: 'live' | 'demo';
}> {
  const res = await apiFetch('/api/graph');
  return res.json();
}

export type Citation = {
  source_id: string;
  chunk_id: string;
  span_start: number | null;
  span_end: number | null;
  quote: string | null;
};

export type ContextNode = {
  id: string;
  label: string;
  node_type: string;
  scope: string;
  extraction_method: 'EXTRACTED' | 'INFERRED' | 'AMBIGUOUS' | 'USER_AUTHORED';
  confidence: number;
  citations: Citation[];
};

export type ContextEdge = {
  from_id: string;
  to_id: string;
  relation: string;
  scope: string;
  extraction_method: 'EXTRACTED' | 'INFERRED' | 'AMBIGUOUS' | 'USER_AUTHORED';
  confidence: number;
  citations: Citation[];
};

export type ContextPackage = {
  query: string;
  seeds: string[];
  nodes: ContextNode[];
  edges: ContextEdge[];
  citations: Citation[];
  insufficient_evidence: boolean;
  reason: string | null;
  inclusion_explanations: Record<string, string>;
  exclusion_explanations: Record<string, string>;
  staleness_warnings: Record<string, string>;
};

export type ContextPackageRequest = {
  query?: string;
  scope?: string;
  source_type?: string;
  depth?: number;
  max_nodes?: number;
  relations?: string[];
  seed_ids?: string[];
};

export type RetrievalHit = {
  id: string;
  label: string;
  score: number;
  source: string;
  citation: Citation;
  citations: Citation[];
  extraction_method: ContextNode['extraction_method'] | 'DERIVED' | null;
  confidence: number | null;
  provenance: 'canonical' | 'derived';
};

export type RetrieveResponse = {
  query: string;
  hits: RetrievalHit[];
  citations: Citation[];
  insufficient_evidence: boolean;
  reason: string | null;
};

export type MemorySuggestion = {
  id: string;
  target_id: string;
  suggestion_type: string;
  proposed_markdown: string;
  proposed_objects: unknown[];
  citations: Citation[];
  proposed_scopes: string[];
  scores: Record<string, number>;
  duplicates: string[];
  conflicts: string[];
  retention_tier: string;
  agent_visibility: string;
  rationale: string;
  estimated_durability: string;
  expires_at: string | null;
  status: SuggestionStatus;
};

export type SuggestionStatus =
  | 'pending'
  | 'accepted'
  | 'edited'
  | 'rejected'
  | 'merged'
  | 'replaced'
  | 'kept'
  | 'retired'
  | 'scope_changed'
  | 'visibility_changed'
  | 'expired';

export type SuggestionReviewAction =
  | 'accept'
  | 'edit'
  | 'reject'
  | 'merge'
  | 'replace'
  | 'keep_both'
  | 'retire'
  | 'change_scope'
  | 'change_visibility'
  | 'expire';

export type CreateSuggestionInput = {
  target_id?: string;
  page_slug?: string;
  suggestion_type: string;
  proposed_markdown?: string;
  proposed_objects?: unknown[];
  citations?: Citation[];
  proposed_scopes?: string[];
  scores?: Record<string, number>;
  duplicates?: string[];
  conflicts?: string[];
  retention_tier?: string;
  agent_visibility?: string;
  rationale?: string;
  estimated_durability?: string;
  expires_at?: string | null;
};

export async function listSuggestions(): Promise<MemorySuggestion[]> {
  const res = await apiFetch('/api/suggestions');
  return res.json();
}

export async function listPageSuggestions(slug: string): Promise<MemorySuggestion[]> {
  const res = await apiFetch(`/api/suggestions?page_slug=${encodeURIComponent(slug)}`);
  return res.json();
}

export async function createSuggestion(input: CreateSuggestionInput): Promise<MemorySuggestion> {
  const res = await apiFetch('/api/suggestions', {
    method: 'POST',
    body: JSON.stringify({
      target_id: input.target_id,
      page_slug: input.page_slug,
      suggestion_type: input.suggestion_type,
      proposed_markdown: input.proposed_markdown ?? '',
      proposed_objects: input.proposed_objects ?? [],
      citations: input.citations ?? [],
      proposed_scopes: input.proposed_scopes ?? [],
      scores: input.scores ?? {},
      duplicates: input.duplicates ?? [],
      conflicts: input.conflicts ?? [],
      retention_tier: input.retention_tier ?? 'candidate',
      agent_visibility: input.agent_visibility ?? 'review_required',
      rationale: input.rationale ?? '',
      estimated_durability: input.estimated_durability ?? '',
      expires_at: input.expires_at ?? null,
    }),
  });
  return res.json();
}

export async function acceptSuggestion(suggestionId: string): Promise<MemorySuggestion> {
  const res = await apiFetch(`/api/suggestions/${encodeURIComponent(suggestionId)}/accept`, {
    method: 'POST',
  });
  return res.json();
}

export async function rejectSuggestion(suggestionId: string): Promise<MemorySuggestion> {
  const res = await apiFetch(`/api/suggestions/${encodeURIComponent(suggestionId)}/reject`, {
    method: 'POST',
  });
  return res.json();
}

export async function reviewSuggestion(
  suggestionId: string,
  action: SuggestionReviewAction,
  options: {
    asset_id?: string;
    scope?: string;
    visibility?: string;
    edited_markdown?: string;
  } = {},
): Promise<MemorySuggestion> {
  const res = await apiFetch(`/api/suggestions/${encodeURIComponent(suggestionId)}/review`, {
    method: 'POST',
    body: JSON.stringify({ action, ...options }),
  });
  return res.json();
}

export async function expireSuggestions(now: string): Promise<MemorySuggestion[]> {
  const res = await apiFetch('/api/suggestions/expire', {
    method: 'POST',
    body: JSON.stringify({ now }),
  });
  return res.json();
}

// ── Memory assets, loadouts, and distillation ───────────────────────────────

export type MemoryAssetType =
  | 'wiki'
  | 'chat'
  | 'skill'
  | 'codegraph'
  | 'source'
  | 'scenario'
  | 'persona';

export type MemoryLayer = 'L0' | 'L1' | 'L2' | 'L3';

export type MemoryScope = {
  id: string;
  wiki_id: string;
  scope_type: 'human' | 'topic' | 'project' | 'repo' | 'person' | 'org';
  name: string;
  parent_scope_id: string | null;
  budget_tokens: number;
  budget_items: number;
  retention_policy: Record<string, unknown>;
  created_at?: string;
  updated_at?: string;
};

export type UpsertMemoryScopeInput = {
  id: string;
  scope_type: MemoryScope['scope_type'];
  name: string;
  parent_scope_id?: string | null;
  budget_tokens?: number;
  budget_items?: number;
  retention_policy?: Record<string, unknown>;
};

export async function listMemoryScopes(
  scopeType?: MemoryScope['scope_type'],
): Promise<MemoryScope[]> {
  const suffix = scopeType ? `?scope_type=${encodeURIComponent(scopeType)}` : '';
  const res = await apiFetch(`/api/memory/scopes${suffix}`);
  return res.json();
}

export async function upsertMemoryScope(
  input: UpsertMemoryScopeInput,
): Promise<MemoryScope> {
  const res = await apiFetch('/api/memory/scopes', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export type MemoryAsset = {
  id: string;
  wiki_id: string;
  asset_type: MemoryAssetType;
  layer: MemoryLayer;
  name: string;
  owner: string;
  scope: string;
  status: 'draft' | 'active' | 'archived';
  visibility: 'private' | 'shared' | 'public';
  version: number;
  page_slug: string | null;
  summary: string;
  body: string;
  tags: string[];
  metadata: Record<string, unknown>;
  citations: Citation[];
  approved_by: string | null;
  reviewed_at: string | null;
  supersedes: string[];
  superseded_by: string[];
  conflict_lineage: string[];
  retired_at: string | null;
  created_at: string;
  updated_at: string;
};

export type MemoryAssetVersion = {
  asset_id: string;
  version: number;
  name: string;
  summary: string;
  body: string;
  status: string;
  change_note: string;
  created_at: string;
};

export type AgentProfile = {
  agent_key: string;
  wiki_id: string;
  name: string;
  description: string;
};

export type AssetBinding = {
  agent_key: string;
  asset_id: string;
  mode: 'always' | 'on_demand';
  priority: number;
};

export type LoadoutEntry = {
  asset: MemoryAsset;
  mode: 'always' | 'on_demand';
  priority: number;
  reason: string;
};

export type LoadoutPackage = {
  agent_key: string;
  query: string;
  entries: LoadoutEntry[];
  citations: Citation[];
  insufficient_evidence: boolean;
  reason: string | null;
};

export type DistillReport = {
  source_id: string;
  session_id: string;
  scope: string;
  atoms_total: number;
  atoms_accepted: number;
  atoms_pending_review: number;
  conflicts_flagged: number;
  sentences_scanned: number;
  asset_ids: string[];
  scenario_id: string | null;
  persona_updated: boolean;
  skill_id: string | null;
  skill_reason: string | null;
  pages_written: string[];
};

export async function listMemoryAssets(
  filters: {
    asset_type?: string;
    layer?: string;
    status?: string;
    /** Whose memory this is — `person:self` for everything the owner owns. */
    owner?: string;
    /** Which vault or repo it belongs to, e.g. `wiki:default`. */
    scope?: string;
    page_slug?: string;
  } = {},
): Promise<MemoryAsset[]> {
  const params = new URLSearchParams();
  if (filters.asset_type) params.set('asset_type', filters.asset_type);
  if (filters.layer) params.set('layer', filters.layer);
  if (filters.status) params.set('status', filters.status);
  if (filters.owner) params.set('owner', filters.owner);
  if (filters.scope) params.set('scope', filters.scope);
  if (filters.page_slug) params.set('page_slug', filters.page_slug);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const res = await apiFetch(`/api/memory/assets${suffix}`);
  return res.json();
}

export async function getMemoryAsset(assetId: string): Promise<MemoryAsset> {
  const res = await apiFetch(`/api/memory/assets/${encodeURIComponent(assetId)}`);
  return res.json();
}

export async function listMemoryAssetVersions(assetId: string): Promise<MemoryAssetVersion[]> {
  const res = await apiFetch(`/api/memory/assets/${encodeURIComponent(assetId)}/versions`);
  return res.json();
}

export async function setMemoryAssetStatus(
  assetId: string,
  status: 'draft' | 'active' | 'archived',
): Promise<MemoryAsset> {
  const res = await apiFetch(`/api/memory/assets/${encodeURIComponent(assetId)}/status`, {
    method: 'POST',
    body: JSON.stringify({ status }),
  });
  return res.json();
}

export async function setMemoryAssetVisibility(
  assetId: string,
  visibility: 'private' | 'shared' | 'public',
): Promise<MemoryAsset> {
  const res = await apiFetch(`/api/memory/assets/${encodeURIComponent(assetId)}/visibility`, {
    method: 'POST',
    body: JSON.stringify({ visibility }),
  });
  return res.json();
}

export async function listMemoryAgents(): Promise<AgentProfile[]> {
  const res = await apiFetch('/api/memory/agents');
  return res.json();
}

export async function upsertMemoryAgent(input: {
  agent_key: string;
  name: string;
  description?: string;
}): Promise<AgentProfile> {
  const res = await apiFetch('/api/memory/agents', {
    method: 'POST',
    body: JSON.stringify({ ...input, description: input.description ?? '' }),
  });
  return res.json();
}

export async function listAgentBindings(agentKey: string): Promise<AssetBinding[]> {
  const res = await apiFetch(`/api/memory/agents/${encodeURIComponent(agentKey)}/bindings`);
  return res.json();
}

export async function bindMemoryAsset(
  agentKey: string,
  input: { asset_id: string; mode?: 'always' | 'on_demand'; priority?: number },
): Promise<AssetBinding> {
  const res = await apiFetch(`/api/memory/agents/${encodeURIComponent(agentKey)}/bindings`, {
    method: 'POST',
    body: JSON.stringify({
      asset_id: input.asset_id,
      mode: input.mode ?? 'always',
      priority: input.priority ?? 100,
    }),
  });
  return res.json();
}

export async function unbindMemoryAsset(
  agentKey: string,
  assetId: string,
): Promise<{ removed: boolean }> {
  const res = await apiFetch(
    `/api/memory/agents/${encodeURIComponent(agentKey)}/bindings/${encodeURIComponent(assetId)}`,
    { method: 'DELETE' },
  );
  return res.json();
}

export async function getAgentLoadout(
  agentKey: string,
  query = '',
): Promise<LoadoutPackage> {
  const suffix = query ? `?query=${encodeURIComponent(query)}` : '';
  const res = await apiFetch(
    `/api/memory/agents/${encodeURIComponent(agentKey)}/loadout${suffix}`,
  );
  return res.json();
}

export type CatalogReport = {
  wiki_assets: number;
  source_assets: number;
  codegraph_assets: number;
  asset_ids: string[];
};

export async function catalogMemoryAssets(): Promise<CatalogReport> {
  const res = await apiFetch('/api/memory/catalog', { method: 'POST' });
  return res.json();
}

export async function distillSource(input: {
  source_id: string;
  scenario_key?: string;
  threshold?: number;
  write_pages?: boolean;
}): Promise<DistillReport> {
  const res = await apiFetch('/api/memory/distill', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export type CaptureTurnInput = {
  role: 'system' | 'user' | 'assistant' | 'tool' | string;
  text: string;
  ts?: string;
  tool_calls?: Array<{
    name: string;
    arguments?: Record<string, unknown>;
    result?: string | null;
    call_id?: string | null;
    ok?: boolean;
  }>;
};

export type CaptureConversationInput = {
  session_id: string;
  interface?: string;
  started_at?: string;
  turns: CaptureTurnInput[];
  scope?: string;
  origin_uri?: string;
};

export type CaptureResponse = {
  source_id: string;
  content_hash: string;
  version: number;
  document_id: string;
  chunk_count: number;
  deduplicated: boolean;
};

export async function captureConversation(
  input: CaptureConversationInput,
): Promise<CaptureResponse> {
  const res = await apiFetch('/api/sources/capture', {
    method: 'POST',
    body: JSON.stringify({
      session_id: input.session_id,
      interface: input.interface ?? 'archivum_home',
      started_at: input.started_at ?? '',
      scope: input.scope ?? 'person:self',
      origin_uri: input.origin_uri ?? '',
      turns: input.turns.map((turn) => ({
        role: turn.role,
        text: turn.text,
        ts: turn.ts ?? '',
        tool_calls: (turn.tool_calls ?? []).map((call) => ({
          name: call.name,
          arguments: call.arguments ?? {},
          result: call.result ?? null,
          call_id: call.call_id ?? null,
          ok: call.ok ?? true,
        })),
      })),
    }),
  });
  return res.json();
}

// ── Graph audit ─────────────────────────────────────────────────────────────

export type GraphCommunity = {
  id: string;
  label: string;
  size: number;
  member_ids: string[];
};

export type SurprisingLink = {
  src_id: string;
  dst_id: string;
  src_label: string;
  dst_label: string;
  rel_type: string;
  score: number;
  neighbor_overlap: number;
  cross_community: boolean;
  reason: string;
};

export type GraphReport = {
  scope: string | null;
  node_count: number;
  edge_count: number;
  by_kind: Record<string, number>;
  by_extraction_method: Record<string, number>;
  self_cited_ids: string[];
  low_confidence_ids: string[];
  orphan_ids: string[];
  communities: GraphCommunity[];
  surprising_links: SurprisingLink[];
  narrative: string[];
  /** Name and kind per record, so the picture can be drawn from this alone. */
  node_labels: Record<string, string>;
  node_kinds: Record<string, string>;
  /** The relationships analysed. Without these you can only draw a layout. */
  edges: { source: string; target: string; relation: string; extraction_method: string }[];
};

export type GraphPathResult = {
  source: string;
  target: string;
  found: boolean;
  length: number;
  steps: { from_id: string; to_id: string; relation: string }[];
  reason: string | null;
};

/**
 * Audit a knowledge graph.
 *
 * `scope` points the same deterministic analysis at something other than the
 * wiki — an indexed repository, for instance. The algorithms never cared which
 * graph they were given; only the routes did.
 */
export async function getGraphAudit(
  surpriseLimit = 10,
  scope?: string,
): Promise<GraphReport> {
  const suffix = scope ? `&scope=${encodeURIComponent(scope)}` : '';
  const res = await apiFetch(`/api/graph/audit?surprise_limit=${surpriseLimit}${suffix}`);
  return res.json();
}

export interface VaultHit {
  slug: string;
  title: string;
  excerpt: string;
  score: number;
}

/**
 * Search the vault properly — semantic, keyword and bounded graph together.
 *
 * The Everything box used to filter titles client-side, which meant anything
 * you could not name was effectively lost, while the embeddings and the hybrid
 * endpoint sat unused.
 */
export interface FoundPage {
  slug: string;
  title: string;
  excerpt: string;
  in_title: boolean;
}

/**
 * Literal text search: names and bodies, straight off the FTS index.
 *
 * Separate from `searchVault`, which is semantic and will decline a weak match.
 * That is right for "things about retrieval" and wrong for "the file where I
 * wrote that string".
 */
export async function findPages(query: string, limit = 30): Promise<FoundPage[]> {
  const res = await apiFetch(
    `/api/find?q=${encodeURIComponent(query)}&limit=${limit}`,
  );
  return res.json();
}

export async function searchVault(query: string, limit = 20): Promise<VaultHit[]> {
  const res = await apiFetch(
    `/api/search?q=${encodeURIComponent(query)}&limit=${limit}`,
  );
  return res.json();
}

export interface OpenTask {
  text: string;
  slug: string;
  page_title: string;
  line: number;
}

/** Check or uncheck a task. Edits the line in the page, which owns the truth. */
export async function toggleTask(input: {
  slug: string;
  line: number;
  done: boolean;
}): Promise<OpenTask> {
  const res = await apiFetch('/api/tasks/toggle', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export interface CodeRepo {
  scope: string;
  name: string;
  path: string;
  status: 'pending' | 'indexing' | 'ready' | 'error';
  files: number;
  nodes: number;
  edges: number;
  pages: number;
  error: string | null;
  indexed_at: string | null;
}

/** The repositories this vault has indexed into code memory. */
export async function listRepos(): Promise<CodeRepo[]> {
  const res = await apiFetch('/api/repos');
  return res.json();
}

/**
 * Point Archivum at a repository on the machine it is running on.
 *
 * Indexing is queued, so this comes back `pending` and the counts fill in once
 * the worker has read the repo.
 */
export async function registerRepo(input: {
  path: string;
  name?: string;
}): Promise<CodeRepo> {
  const res = await apiFetch('/api/repos', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

/** Queue an already-registered repository to be read again. */
export async function reindexRepo(name: string): Promise<CodeRepo> {
  const res = await apiFetch(`/api/repos/${encodeSlugPath(name)}/reindex`, {
    method: 'POST',
  });
  return res.json();
}

export async function getGraphPath(source: string, target: string): Promise<GraphPathResult> {
  const res = await apiFetch(
    `/api/graph/path?source=${encodeURIComponent(source)}&target=${encodeURIComponent(target)}`,
  );
  return res.json();
}

export async function getContextPackage(
  input: ContextPackageRequest = {},
): Promise<ContextPackage> {
  const res = await apiFetch('/api/context-package', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function retrieveContext(input: { query: string; limit?: number }): Promise<RetrieveResponse> {
  const res = await apiFetch('/api/retrieve', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function ensureDailyNote(date?: string): Promise<Page> {
  const res = await apiFetch('/api/life/daily', {
    method: 'POST',
    body: JSON.stringify({ date }),
  });
  return res.json();
}

async function parseSSEStream(
  response: Response,
  onEvent: (data: unknown) => void,
): Promise<void> {
  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const parts = buffer.split('\n\n');
    buffer = parts.pop() ?? '';

    for (const part of parts) {
      const lines = part.split('\n');
      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const raw = line.slice(6).trim();
          if (raw === '[DONE]') return;
          try {
            onEvent(JSON.parse(raw));
          } catch {
            // non-JSON data line — ignore
          }
        }
      }
    }
  }
}

export async function ingestFile(
  file: File,
): Promise<{ accepted: boolean; file: string | null }> {
  const formData = new FormData();
  formData.append('file', file);

  const csrf = csrfToken();
  const response = await fetch('/api/ingest/file', {
    method: 'POST',
    credentials: 'include',
    body: formData,
    ...(csrf ? { headers: { 'X-CSRF-Token': csrf } } : {}),
  });

  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }

  return response.json();
}

export async function ingestUrl(
  url: string,
): Promise<{ accepted: boolean; url: string | null }> {
  const csrf = csrfToken();
  const response = await fetch('/api/ingest/url', {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
    },
    body: JSON.stringify({ url }),
  });

  if (!response.ok) {
    throw new Error(await responseErrorMessage(response));
  }

  return response.json();
}

export async function listIngestHistory(limit = 25): Promise<IngestLog[]> {
  const res = await apiFetch(`/api/ingest/history?limit=${encodeURIComponent(String(limit))}`);
  return res.json();
}

export function openIngestSocket(
  onMessage: (message: IngestSocketMessage) => void,
  onClose?: () => void,
): WebSocket {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${protocol}//${window.location.host}/api/ingest/ws`);

  socket.addEventListener('message', (event) => {
    try {
      onMessage(JSON.parse(event.data) as IngestSocketMessage);
    } catch {
      // Ignore malformed socket messages.
    }
  });
  if (onClose) {
    socket.addEventListener('close', onClose);
  }
  return socket;
}

export type InviteToken = {
  id: number;
  wiki_id: string;
  token: string;
  role: string;
  created_by: string;
  created_at: string;
  expires_at: string | null;
  used: number;
};

export type LintIssue = {
  type: string;
  page: string;
  target?: string;
  suggestion: string;
};

export type AudioSupportStatus = {
  available: boolean;
  audio_available?: boolean;
  video_available?: boolean;
  dependencies: {
    openai_whisper: boolean;
    ffmpeg: boolean;
  };
  missing: string[];
  notes: string[];
};

export type AudioInstallAction = {
  name: string;
  status: 'installed' | 'already_available' | 'failed';
  detail: string;
};

export type AudioSupportInstallResult = {
  ok: boolean;
  actions: AudioInstallAction[];
  status: AudioSupportStatus;
};

export type McpSettings = {
  endpoint: string;
  auth_required: boolean;
  api_key_configured: boolean;
  client_config: {
    mcpServers: {
      archivum: {
        url: string;
        headers?: Record<string, string>;
      };
    };
  };
};

export async function createInvite(
  role: 'viewer' | 'collaborator',
  expires_in_days: number | null,
): Promise<{ token: string; url: string; role: string; expires_at: string | null }> {
  const res = await apiFetch('/api/auth/invites', {
    method: 'POST',
    body: JSON.stringify({ role, expires_in_days }),
  });
  return res.json();
}

export async function listInvites(): Promise<InviteToken[]> {
  const res = await apiFetch('/api/auth/invites');
  return res.json();
}

export async function getAudioSupport(): Promise<AudioSupportStatus> {
  const res = await apiFetch('/api/audio-support');
  return res.json();
}

export async function installAudioSupport(): Promise<AudioSupportInstallResult> {
  const res = await apiFetch('/api/audio-support/install', {
    method: 'POST',
  });
  return res.json();
}

export async function getMcpSettings(): Promise<McpSettings> {
  const res = await apiFetch('/api/settings/mcp');
  return res.json();
}

export type McpDevice = {
  id: string;
  name: string;
  created_at: string;
  last_seen_at: string | null;
  revoked_at: string | null;
};

export type PairingToken = { token: string; expires_at: string };

export async function getMcpDevices(): Promise<McpDevice[]> {
  const res = await apiFetch('/api/mcp/devices');
  if (!res.ok) throw new Error('Failed to load linked devices');
  return (await res.json()).devices;
}

export async function issuePairingToken(): Promise<PairingToken> {
  const res = await apiFetch('/api/mcp/pairing-tokens', { method: 'POST' });
  if (!res.ok) throw new Error('Failed to issue a pairing token');
  return res.json();
}

export async function revokeMcpDevice(deviceId: string): Promise<boolean> {
  const res = await apiFetch(`/api/mcp/devices/${encodeURIComponent(deviceId)}`, {
    method: 'DELETE',
  });
  if (!res.ok) throw new Error('Failed to revoke device');
  return (await res.json()).revoked;
}

export type LlmSettings = {
  llm_extraction_provider: string;
  llm_synthesis_provider: string;
  llm_model: string;
  llm_synthesis_model: string;
  ollama_base_url: string;
  ollama_api_key_configured: boolean;
  ollama_api_key_masked: string;
  cli_providers?: Record<string, {
    available: boolean;
    command: string;
    label: string;
  }>;
};

export type CodexAuthStatus = {
  available: boolean;
  authenticated: boolean;
  detail: string;
};

export type CodexDeviceLogin = {
  started: boolean;
  provider: string;
  url: string;
  code: string;
  detail: string;
};

export type UpdateLlmSettingsInput = {
  llm_extraction_provider: string;
  llm_synthesis_provider: string;
  llm_model: string;
  llm_synthesis_model: string;
  ollama_base_url: string;
  ollama_api_key?: string | null;
};

export async function getLlmSettings(): Promise<LlmSettings> {
  const res = await apiFetch('/api/settings/llm');
  return res.json();
}

export async function getCodexAuthStatus(): Promise<CodexAuthStatus> {
  const res = await apiFetch('/api/settings/cli-auth/codex');
  return res.json();
}

export async function startCodexDeviceLogin(): Promise<CodexDeviceLogin> {
  const res = await apiFetch('/api/settings/cli-auth/codex/start', {
    method: 'POST',
  });
  return res.json();
}

export async function updateLlmSettings(input: UpdateLlmSettingsInput): Promise<LlmSettings> {
  const res = await apiFetch('/api/settings/llm', {
    method: 'PUT',
    body: JSON.stringify(input),
  });
  return res.json();
}

export async function lintWiki(): Promise<{ issues: LintIssue[]; counts: { issues: number } }> {
  const res = await apiFetch('/api/lint');
  return res.json();
}

export async function applyLintFix(fix: { type: string; [key: string]: string }): Promise<{ detail: string; message?: string }> {
  const res = await apiFetch('/api/lint/fix', {
    method: 'POST',
    body: JSON.stringify(fix),
  });
  return res.json();
}

export async function query(
  question: string,
  onToken: (t: string) => void,
  onCitations: (c: Page[]) => void,
): Promise<void> {
  const csrf = csrfToken();
  const response = await fetch('/api/query', {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
    },
    body: JSON.stringify({ question }),
  });

  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText);
    throw new Error(text || `HTTP ${response.status}`);
  }

  await parseSSEStream(response, (data) => {
    const event = data as { type: string; token?: string; citations?: Page[] };
    if (event.type === 'token' && event.token !== undefined) {
      onToken(event.token);
    } else if (event.type === 'citations' && event.citations) {
      onCitations(event.citations);
    }
  });
}

// ── Redesign surfaces ───────────────────────────────────────────────────────
// The stream, the entry list, the owner profile, and the memory pipeline view
// each read from one endpoint rather than stitching several client-side.

export type ActivityKind =
  | 'page_created'
  | 'page_edited'
  | 'suggestion'
  | 'ingest'
  | 'memory'
  | 'session'
  | 'fix';

export type ActivityActor = 'you' | 'agent' | 'system';

export type ActivityItem = {
  id: string;
  kind: ActivityKind;
  at: string;
  title: string;
  summary: string;
  actor: ActivityActor;
  slug: string | null;
  needs_review: boolean;
  payload: Record<string, unknown>;
};

export type ActivityFeed = {
  items: ActivityItem[];
  next_before: string | null;
  pending_review: number;
  /** Still outstanding. A standing list, not part of the timeline. */
  open_tasks: OpenTask[];
};

export async function getActivity(
  options: { limit?: number; before?: string } = {},
): Promise<ActivityFeed> {
  const params = new URLSearchParams();
  if (options.limit) params.set('limit', String(options.limit));
  if (options.before) params.set('before', options.before);
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const res = await apiFetch(`/api/activity${suffix}`);
  return res.json();
}

export type EntryKind =
  | 'note'
  | 'thought'
  | 'source'
  | 'conversation'
  | 'person'
  | 'decision'
  | 'daily';

export type Entry = {
  id: string;
  kind: EntryKind;
  title: string;
  slug: string | null;
  folder: string;
  updated_at: string;
  created_at: string;
  actor: 'you' | 'agent';
  needs_review: boolean;
  tags: string[];
  detail: string;
};

export type EntryList = {
  entries: Entry[];
  counts: Record<string, number>;
  total: number;
};

export async function listEntries(
  options: { kind?: string; needsReview?: boolean; limit?: number } = {},
): Promise<EntryList> {
  const params = new URLSearchParams();
  if (options.kind) params.set('kind', options.kind);
  if (options.needsReview) params.set('needs_review', 'true');
  if (options.limit) params.set('limit', String(options.limit));
  const suffix = params.toString() ? `?${params.toString()}` : '';
  const res = await apiFetch(`/api/entries${suffix}`);
  return res.json();
}

export type OwnerProfile = {
  wiki_id: string;
  scope_id: string;
  name: string;
  initials: string;
  role: string;
  since: string | null;
  needs_setup: boolean;
  pages: number;
  memories_active: number;
  memories_total: number;
  agents: number;
  pending_review: number;
};

export async function getOwner(): Promise<OwnerProfile> {
  const res = await apiFetch('/api/me');
  return res.json();
}

export type MemoryStats = {
  suggestions_total: number;
  suggestions_pending: number;
  suggestions_kept: number;
  suggestions_dropped: number;
  suggestions_by_status: Record<string, number>;
  assets_total: number;
  assets_active: number;
  assets_draft: number;
  assets_archived: number;
  assets_disputed: number;
  assets_by_layer: Record<string, number>;
};

export async function getMemoryStats(): Promise<MemoryStats> {
  const res = await apiFetch('/api/memory/stats');
  return res.json();
}

export type CapturePreview = {
  kind: EntryKind;
  folder: string;
  links: { slug: string; title: string }[];
  tags: string[];
  reason: string;
};

export async function previewCapture(text: string): Promise<CapturePreview> {
  const res = await apiFetch('/api/capture/preview', {
    method: 'POST',
    body: JSON.stringify({ text }),
  });
  return res.json();
}

export type GraphCommunitiesResponse = {
  communities: GraphCommunity[];
};

export async function getGraphCommunities(): Promise<GraphCommunity[]> {
  const res = await apiFetch('/api/graph/communities');
  const body = await res.json();
  return Array.isArray(body) ? body : (body.communities ?? []);
}

export type RegisterMemoryAssetInput = {
  id: string;
  asset_type: MemoryAssetType;
  layer?: MemoryLayer;
  name: string;
  summary?: string;
  body?: string;
  page_slug?: string | null;
  tags?: string[];
  status?: MemoryAsset['status'];
  visibility?: MemoryAsset['visibility'];
};

export async function registerMemoryAsset(
  input: RegisterMemoryAssetInput,
): Promise<MemoryAsset> {
  const res = await apiFetch('/api/memory/assets', {
    method: 'POST',
    body: JSON.stringify(input),
  });
  return res.json();
}

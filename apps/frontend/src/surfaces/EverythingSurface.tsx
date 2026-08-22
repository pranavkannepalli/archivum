import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  findPages,
  listEntries,
  searchVault,
  type Entry,
  type EntryList,
  type FoundPage,
  type VaultHit,
} from '../api';
import { searchResults } from './searchResults';
import { Icon } from '../shell/Icon';
import { cn } from '../lib/cn';

/**
 * One place to look for anything.
 *
 * Two views over the same entries: a flat list sorted by what you touched last,
 * and a Finder-style column browser over the folders on disk. Facets are
 * computed from the entry kind, so there is no collection to curate.
 */

const KIND_LABELS: { key: string; label: string; icon: string }[] = [
  { key: 'note', label: 'Notes', icon: 'file' },
  { key: 'thought', label: 'Thoughts', icon: 'zap' },
  { key: 'source', label: 'Sources', icon: 'database' },
  { key: 'conversation', label: 'Conversations', icon: 'message' },
  { key: 'person', label: 'People', icon: 'users' },
  { key: 'decision', label: 'Decisions', icon: 'check' },
  { key: 'daily', label: 'Daily', icon: 'clock' },
];

const KIND_ICON: Record<string, string> = {
  note: 'file',
  thought: 'zap',
  source: 'database',
  conversation: 'message',
  person: 'users',
  decision: 'check',
  daily: 'clock',
};

type ColumnNode = { name: string; path: string; entry?: Entry; isDir: boolean };

function columnsFor(entries: Entry[], path: string[]): ColumnNode[][] {
  const columns: ColumnNode[][] = [];
  for (let depth = 0; depth <= path.length; depth += 1) {
    const prefix = path.slice(0, depth);
    const seen = new Map<string, ColumnNode>();
    for (const entry of entries) {
      if (!entry.slug) continue;
      const parts = entry.slug.split('/');
      if (parts.length <= depth) continue;
      if (prefix.some((segment, index) => parts[index] !== segment)) continue;
      const name = parts[depth];
      const isDir = parts.length > depth + 1;
      const nodePath = parts.slice(0, depth + 1).join('/');
      if (!seen.has(name)) {
        seen.set(name, { name, path: nodePath, isDir, entry: isDir ? undefined : entry });
      }
    }
    const column = [...seen.values()].sort((a, b) => {
      if (a.isDir !== b.isDir) return a.isDir ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
    if (column.length === 0) break;
    columns.push(column);
  }
  return columns;
}

export default function EverythingSurface() {
  const [params, setParams] = useSearchParams();
  const [data, setData] = useState<EntryList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [view, setView] = useState<'list' | 'files'>('list');
  const [path, setPath] = useState<string[]>([]);
  const [selected, setSelected] = useState<Entry | null>(null);
  const navigate = useNavigate();

  const kind = params.get('kind') ?? '';
  const needsReview = params.get('needs_review') === '1';

  useEffect(() => {
    listEntries({
      ...(kind ? { kind } : {}),
      ...(needsReview ? { needsReview: true } : {}),
      limit: 500,
    })
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : 'Could not load entries'));
  }, [kind, needsReview]);

  // Semantic search, debounced. The box used to filter titles in the browser,
  // which meant anything you could not name was effectively lost — while the
  // embeddings and the hybrid endpoint sat unused behind it.
  const [hits, setHits] = useState<VaultHit[] | null>(null);
  const [found, setFound] = useState<FoundPage[]>([]);

  useEffect(() => {
    const needle = search.trim();
    if (!needle) {
      setHits(null);
      setFound([]);
      return;
    }
    let cancelled = false;
    const id = window.setTimeout(() => {
      // Two channels, because they answer different questions. Literal text
      // search is an index lookup and always finds what is actually written;
      // semantic search finds pages that are *about* it and declines weak
      // matches. Running only the second meant typing a file name could return
      // nothing at all.
      findPages(needle)
        .then((rows) => !cancelled && setFound(rows))
        .catch(() => !cancelled && setFound([]));
      searchVault(needle)
        .then((rows) => !cancelled && setHits(rows))
        // A failed search falls back to matching titles rather than showing
        // nothing: degraded search beats no search.
        .catch(() => !cancelled && setHits(null));
    }, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(id);
    };
  }, [search]);

  const entries = useMemo(() => {
    const all = data?.entries ?? [];
    const needle = search.trim().toLowerCase();
    if (!needle) return all;
    // The engine ranked these against the whole vault, so they decide both
    // membership and order. Filtering them through the loaded page dropped
    // anything not already on screen.
    if (hits || found.length) return searchResults(all, hits ?? [], found);
    return all.filter(
      (entry) =>
        entry.title.toLowerCase().includes(needle) ||
        (entry.slug ?? '').toLowerCase().includes(needle),
    );
  }, [data, search, hits, found]);

  const columns = useMemo(() => columnsFor(entries, path), [entries, path]);

  function setFacet(next: string) {
    const updated = new URLSearchParams(params);
    if (next) updated.set('kind', next);
    else updated.delete('kind');
    setParams(updated, { replace: true });
  }

  function toggleNeedsReview() {
    const updated = new URLSearchParams(params);
    if (needsReview) updated.delete('needs_review');
    else updated.set('needs_review', '1');
    setParams(updated, { replace: true });
  }

  if (error) return <div className="surface-error">{error}</div>;

  const counts = data?.counts ?? {};
  const total = data?.total ?? 0;

  return (
    <div className="surface on">
      <div className="col-wide">
        <div className="ev-top">
          <div className="bigsearch">
            <Icon name="search" size={18} />
            <input
              value={search}
              placeholder="Search names and page text…"
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>

          <div className="facets">
            <button type="button" className={cn('facet', !kind && 'on')} onClick={() => setFacet('')}>
              Everything
            </button>
            {KIND_LABELS.filter((option) => counts[option.key]).map((option) => (
              <button
                key={option.key}
                type="button"
                className={cn('facet', kind === option.key && 'on')}
                onClick={() => setFacet(option.key)}
              >
                <Icon name={option.icon} size={12} />
                {option.label} <span style={{ color: 'var(--text-3)' }}>{counts[option.key]}</span>
              </button>
            ))}
            <span style={{ width: 1, height: 20, background: 'var(--border)', margin: '0 4px' }} />
            <button
              type="button"
              className={cn('facet', needsReview && 'on')}
              onClick={toggleNeedsReview}
            >
              <Icon name="alert" size={12} />
              Needs you
            </button>
          </div>

          <div className="ev-bar">
            <span className="c">
              {entries.length} of {total} entries · sorted by what you touched last
            </span>
            <div className="seg">
              <button type="button" className={cn(view === 'list' && 'on')} onClick={() => setView('list')}>
                List
              </button>
              <button
                type="button"
                className={cn(view === 'files' && 'on')}
                onClick={() => setView('files')}
              >
                Files
              </button>
            </div>
          </div>
        </div>

        {view === 'list' ? (
          <div className="rows">
            {entries.length === 0 && (
              <div className="stream-empty">
                <h3>Nothing matches</h3>
                <p>Try a different filter, or clear the search.</p>
              </div>
            )}
            {entries.map((entry) => (
              <button
                key={entry.id}
                type="button"
                className="row-i"
                onClick={() => entry.slug && navigate(`/wiki/${entry.slug}`)}
              >
                <Icon name={KIND_ICON[entry.kind] ?? 'file'} />
                <div>
                  <div className="t">{entry.title}</div>
                  <div className="s">
                    {entry.folder && <span className="path">{entry.folder}/</span>}
                    {(entry.detail || entry.tags.join(', ')) && (
                      <span>
                        {entry.folder ? ' · ' : ''}
                        {entry.detail || entry.tags.join(', ')}
                      </span>
                    )}
                  </div>
                </div>
                <div className="meta">
                  {entry.needs_review && <span className="chip chip-warn">needs you</span>}
                  {entry.actor === 'agent' && <span className="chip chip-accent">agent</span>}
                </div>
              </button>
            ))}
          </div>
        ) : (
          <div className="cols">
            {columns.map((column, depth) => (
              <div className="pane" key={depth}>
                {column.map((node) => (
                  <button
                    key={node.path}
                    type="button"
                    className={cn(
                      'crow',
                      (path[depth] === node.name || selected?.slug === node.path) && 'on',
                    )}
                    onClick={() => {
                      if (node.isDir) {
                        setPath([...path.slice(0, depth), node.name]);
                        setSelected(null);
                      } else {
                        setPath(path.slice(0, depth));
                        setSelected(node.entry ?? null);
                      }
                    }}
                    onDoubleClick={() => node.entry?.slug && navigate(`/wiki/${node.entry.slug}`)}
                  >
                    <Icon name={node.isDir ? 'folder' : KIND_ICON[node.entry?.kind ?? 'note'] ?? 'file'} />
                    <span className="nm">{node.entry?.title ?? node.name}</span>
                    {node.isDir ? (
                      <Icon name="chevronRight" className="chev" />
                    ) : (
                      node.entry?.needs_review && <span className="wait" />
                    )}
                  </button>
                ))}
              </div>
            ))}
            <div className="pane preview">
              {selected ? (
                <>
                  <div className="kind">
                    <span className="chip chip-accent">{selected.kind}</span>
                    {selected.needs_review && <span className="chip chip-warn">needs you</span>}
                  </div>
                  <h3>{selected.title}</h3>
                  <dl>
                    <div className="prop">
                      <dt>Path</dt>
                      <dd style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--t-11)' }}>
                        {selected.slug}
                      </dd>
                    </div>
                    <div className="prop">
                      <dt>Written by</dt>
                      <dd>{selected.actor === 'agent' ? 'An agent' : 'You'}</dd>
                    </div>
                    {selected.tags.length > 0 && (
                      <div className="prop">
                        <dt>Tags</dt>
                        <dd>{selected.tags.join(', ')}</dd>
                      </div>
                    )}
                  </dl>
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    onClick={() => selected.slug && navigate(`/wiki/${selected.slug}`)}
                  >
                    <Icon name="arrowRight" />
                    Open
                  </button>
                </>
              ) : (
                <div style={{ color: 'var(--text-3)', fontSize: 'var(--t-13)', paddingTop: 40, textAlign: 'center' }}>
                  Pick something to preview it.
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

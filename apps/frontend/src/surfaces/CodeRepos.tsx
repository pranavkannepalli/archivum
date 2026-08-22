import { useCallback, useEffect, useState } from 'react';
import { listRepos, registerRepo, reindexRepo, type CodeRepo } from '../api';
import { useToast } from '../components/ui/Toast';
import { Icon } from '../shell/Icon';
import { cn } from '../lib/cn';

/**
 * Point Archivum at the repositories you actually work in.
 *
 * A second brain for someone who writes code should remember the code. Indexing
 * a repo reads it into the same knowledge graph as everything else, writes a
 * page per cluster into your vault, and links symbols to the decisions that
 * named them — so "why is this like this?" has an answer with a citation.
 *
 * Indexing runs on a queue, so a freshly registered repo shows as pending and
 * fills in its counts once the worker gets to it. That is deliberate: reading a
 * large repository takes a while, and the vault has to stay usable meanwhile.
 */

const STATUS_LABEL: Record<CodeRepo['status'], string> = {
  pending: 'queued',
  indexing: 'reading…',
  ready: 'indexed',
  error: 'failed',
};

export default function CodeRepos() {
  const [repos, setRepos] = useState<CodeRepo[]>([]);
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  const load = useCallback(async () => {
    try {
      setRepos(await listRepos());
    } catch {
      // A missing list is not worth an error toast on every poll.
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // While anything is queued or being read, keep checking so the counts appear
  // without the person having to reload the page.
  useEffect(() => {
    if (!repos.some((repo) => repo.status === 'pending' || repo.status === 'indexing')) {
      return;
    }
    const id = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(id);
  }, [repos, load]);

  async function add() {
    const trimmed = path.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    try {
      const repo = await registerRepo({ path: trimmed });
      setPath('');
      await load();
      push({
        kind: 'success',
        title: 'Queued for indexing',
        description: `${repo.name} will show up in your vault under code/${repo.name}.`,
      });
    } catch (error) {
      push({
        kind: 'error',
        title: "Couldn't add that repository",
        description: error instanceof Error ? error.message : 'Unknown error',
      });
    } finally {
      setBusy(false);
    }
  }

  async function refresh(repo: CodeRepo) {
    try {
      await reindexRepo(repo.name);
      await load();
      push({ kind: 'success', title: 'Queued for a re-read', description: repo.name });
    } catch (error) {
      push({
        kind: 'error',
        title: "Couldn't queue that",
        description: error instanceof Error ? error.message : 'Unknown error',
      });
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <input
          className="soft-border w-full rounded-[8px] border bg-transparent px-3 py-2 text-sm"
          placeholder="/path/to/your/repository"
          value={path}
          spellCheck={false}
          onChange={(event) => setPath(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              void add();
            }
          }}
        />
        <button
          type="button"
          className="btn btn-primary"
          disabled={!path.trim() || busy}
          onClick={() => void add()}
        >
          {busy ? 'Adding…' : 'Index'}
        </button>
      </div>

      {repos.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No repositories yet. Add one and Archivum will read it into your graph, write a
          page for each cluster it finds, and connect the code to whatever you have already
          decided about it.
        </p>
      ) : (
        <ul className="space-y-2">
          {repos.map((repo) => (
            <li
              key={repo.scope}
              className="soft-border flex items-center gap-3 rounded-[8px] border bg-white/[0.02] p-3"
            >
              <Icon name="graph" size={14} />
              <div style={{ minWidth: 0 }}>
                <div className="text-sm font-medium">{repo.name}</div>
                <div className="text-xs text-muted-foreground">
                  <span
                    className={cn(
                      'chip',
                      repo.status === 'ready' && 'chip-ok',
                      repo.status === 'error' && 'chip-bad',
                    )}
                  >
                    {STATUS_LABEL[repo.status]}
                  </span>{' '}
                  {repo.status === 'ready' && (
                    <>
                      {repo.nodes} records · {repo.edges} links · {repo.pages} pages
                    </>
                  )}
                  {repo.error && <span title={repo.error}> — {repo.error}</span>}
                </div>
              </div>
              <button
                type="button"
                className="btn btn-outline btn-sm"
                style={{ marginLeft: 'auto' }}
                onClick={() => void refresh(repo)}
              >
                Re-read
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

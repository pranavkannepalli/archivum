import { useState } from 'react';
import { catalogMemoryAssets, reindexVault } from '../api';
import { useToast } from '../components/ui/Toast';
import { Icon } from '../shell/Icon';
import { describeDegraded } from './ReindexControl';

/**
 * The catch-up pass for a vault that predates the current write path.
 *
 * Writing a page now indexes it and registers it as governed memory in one go,
 * and ingesting a file stores its bytes as evidence and registers that too. A
 * vault written before that is missing those records, and nothing repairs them
 * on its own — the watcher only reacts to files that change, and a page nobody
 * touches never changes.
 *
 * So this is a repair button, not a routine one: it re-reads every file from
 * disk and backfills anything the registry never heard about. Running it on an
 * up-to-date vault is a no-op, which is the property that makes it safe to
 * press when you are not sure.
 */
export default function VaultRepair() {
  const [busy, setBusy] = useState(false);
  const { push } = useToast();

  async function run() {
    setBusy(true);
    try {
      // Files first: cataloguing reads the pages and sources that pass leaves
      // behind, so the order is what makes one press enough.
      const indexed = await reindexVault({ force: true });
      const catalogued = await catalogMemoryAssets();

      const registered = catalogued.wiki_assets + catalogued.source_assets + catalogued.codegraph_assets;
      if (indexed.degraded.length > 0) {
        push({
          kind: 'error',
          title: 'Repaired, with gaps',
          description: `${indexed.pages} pages re-read and ${registered} records registered, but ${describeDegraded(indexed.degraded).toLowerCase()} is still out of date.`,
        });
      } else {
        push({
          kind: 'success',
          title: 'Vault caught up',
          description: `${indexed.pages} page${indexed.pages === 1 ? '' : 's'} re-read from disk, ${registered} record${registered === 1 ? '' : 's'} registered as memory.`,
        });
      }
    } catch (error) {
      push({
        kind: 'error',
        title: "Couldn't finish the repair pass",
        description: error instanceof Error ? error.message : 'Unknown error',
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2">
      <button type="button" className="btn btn-outline" disabled={busy} onClick={run}>
        <Icon name="history" className={busy ? 'spin' : undefined} />
        {busy ? 'Catching up…' : 'Re-read the vault and catch memory up'}
      </button>
      <p className="text-sm text-muted-foreground">
        Re-reads every file from disk and registers anything your agents have not been told
        about yet. Everything you write from here on does this by itself; this is for what
        was already there.
      </p>
    </div>
  );
}

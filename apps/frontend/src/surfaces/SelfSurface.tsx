import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  getOwner,
  listMemoryAssets,
  listMemoryAgents,
  setMemoryAssetStatus,
  type AgentProfile,
  type MemoryAsset,
  type OwnerProfile,
} from '../api';
import { useToast } from '../components/ui/Toast';
import { Icon } from '../shell/Icon';
import { cn } from '../lib/cn';

/**
 * You, as an entry.
 *
 * Everything in the vault is stored relative to `person:self`, so this page is
 * the root of the graph rather than a settings screen. The toggles write
 * straight through to asset status — active means agents may use it, archived
 * means they may not.
 */

// Every memory asset is *owned* by person:self and *scoped* to the wiki it
// belongs to. This page is the owner's view, so it filters on owner; filtering
// on scope matches nothing and renders an empty section under a live count.
const SELF_OWNER = 'person:self';

export default function SelfSurface() {
  const [owner, setOwner] = useState<OwnerProfile | null>(null);
  const [assets, setAssets] = useState<MemoryAsset[]>([]);
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const navigate = useNavigate();
  const { push } = useToast();

  useEffect(() => {
    Promise.all([
      getOwner(),
      listMemoryAssets({ owner: SELF_OWNER }),
      listMemoryAgents(),
    ])
      .then(([nextOwner, nextAssets, nextAgents]) => {
        setOwner(nextOwner);
        setAssets(nextAssets);
        setAgents(nextAgents);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Could not load your profile'));
  }, []);

  async function toggle(asset: MemoryAsset) {
    const next = asset.status === 'active' ? 'archived' : 'active';
    setBusy(asset.id);
    try {
      const updated = await setMemoryAssetStatus(asset.id, next);
      setAssets((prev) => prev.map((item) => (item.id === asset.id ? updated : item)));
      push({
        kind: 'success',
        title: next === 'active' ? 'Agents can use this' : 'Turned off for agents',
        description: asset.name,
      });
    } catch (err) {
      push({
        kind: 'error',
        title: "Couldn't change that",
        description: err instanceof Error ? err.message : 'Unknown error',
      });
    } finally {
      setBusy(null);
    }
  }

  if (error) return <div className="surface-error">{error}</div>;
  if (!owner) return <div className="surface-loading">Loading your profile…</div>;

  return (
    <div className="surface on">
      <div className="col">
        <div className="self-hero">
          <div className="self-av">{owner.initials}</div>
          <div style={{ minWidth: 0 }}>
            <h1>{owner.name}</h1>
            <div className="sub">Everything in this vault is stored relative to you.</div>
            <div className="self-chips">
              <span className="chip chip-accent">person · self</span>
              <span className="chip">{owner.pages} entries</span>
              <span className="chip">{owner.memories_active} memories</span>
              <span className="chip">
                {owner.agents} agent{owner.agents === 1 ? '' : 's'}
              </span>
              {owner.since && (
                <span className="chip">since {new Date(owner.since).toLocaleDateString()}</span>
              )}
            </div>
          </div>
          <button
            type="button"
            className="btn btn-outline"
            style={{ marginLeft: 'auto', flex: 'none' }}
            onClick={() => navigate('/visualized')}
          >
            <Icon name="graph" />
            See the graph around you
          </button>
        </div>

        <div className="self-sec">
          <div className="hd">
            <h2>What your agents are told about you</h2>
            <p>· before they touch anything</p>
          </div>

          {assets.length === 0 ? (
            <div className="stream-empty">
              <h3>Nothing yet</h3>
              <p>
                Archivum has not distilled anything about you. Capture a few things, or run a
                distill over a conversation, and what it learns will show up here for you to
                approve.
              </p>
            </div>
          ) : (
            assets.map((asset) => {
              const disputed = asset.conflict_lineage.length > 0;
              return (
                <div key={asset.id} className={cn('know', disputed && 'rule')}>
                  <button
                    type="button"
                    className={cn('toggle', asset.status === 'active' && 'on')}
                    disabled={busy === asset.id}
                    aria-label={
                      asset.status === 'active' ? 'Turn off for agents' : 'Turn on for agents'
                    }
                    onClick={() => void toggle(asset)}
                  />
                  <div className="txt">
                    {asset.summary || asset.name}
                    <div className="sub">
                      <span
                        className={cn(
                          'chip',
                          disputed ? 'chip-warn' : asset.status === 'active' ? 'chip-ok' : '',
                        )}
                      >
                        {disputed ? 'disputed' : asset.status === 'active' ? 'live' : 'off'}
                      </span>
                      <span>
                        {asset.layer} · {asset.citations.length} citation
                        {asset.citations.length === 1 ? '' : 's'}
                        {asset.status === 'active'
                          ? ' · agents may use this'
                          : ' · agents cannot see this'}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })
          )}
        </div>

        {agents.length > 0 && (
          <div className="self-sec">
            <div className="hd">
              <h2>Agents connected to you</h2>
              <p>· what reads this vault</p>
            </div>
            <div className="ppl">
              {agents.map((agent) => (
                <div key={agent.agent_key} className="p">
                  <span className="av">
                    <Icon name="bot" size={13} />
                  </span>
                  <span>
                    <span className="t">{agent.name || agent.agent_key}</span>{' '}
                    <span className="s">{agent.description || agent.agent_key}</span>
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        <div className="self-sec" style={{ paddingBottom: '30vh' }}>
          <div className="tracebar" style={{ margin: 0 }}>
            <Icon name="file" size={13} />
            This is memory owned by <code style={{ fontFamily: 'var(--font-mono)' }}>{SELF_OWNER}</code>.
            Turn a line off and the agents stop knowing it.
          </div>
        </div>
      </div>
    </div>
  );
}

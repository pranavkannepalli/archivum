import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  getGraph,
  getGraphAudit,
  getMemoryStats,
  getOwner,
  listAgentBindings,
  listMemoryAgents,
  listRepos,
  type AgentProfile,
  type CodeRepo,
  type GraphReport,
  type MemoryStats,
  type OwnerProfile,
} from '../api';
import type { GraphEdge, GraphNode } from '../types';
import { Icon } from '../shell/Icon';
import KnowledgeGraph from './KnowledgeGraph';

type Tab = 'graph' | 'flow' | 'agents';
import { cn } from '../lib/cn';

/**
 * The structures under the vault, drawn from the stores that hold them.
 *
 * Nothing here is synthesised for the picture: rings come from graph
 * communities, the funnel from memory stats, the agent bars from bindings. If a
 * number is unavailable the panel says so rather than inventing a plausible one.
 */
function FlowPanel({ stats }: { stats: MemoryStats | null }) {
  if (!stats) return <div className="surface-loading">Counting…</div>;

  const steps = [
    {
      label: 'Proposed',
      value: stats.suggestions_total,
      note: 'everything an agent or an ingest ever put forward',
    },
    {
      label: 'You kept',
      value: stats.suggestions_kept,
      note: `${stats.suggestions_dropped} rejected · ${stats.suggestions_pending} still waiting on you`,
    },
    {
      label: 'Live memory',
      value: stats.assets_active,
      note: `${stats.assets_archived} switched off · ${stats.assets_draft} still drafts`,
    },
  ];

  const max = Math.max(...steps.map((step) => step.value), 1);

  if (stats.suggestions_total === 0 && stats.assets_total === 0) {
    return (
      <div className="stream-empty">
        <h3>Nothing has been distilled yet</h3>
        <p>
          When an agent proposes something, or a source is read, the pipeline from proposal to live
          memory shows up here.
        </p>
      </div>
    );
  }

  return (
    <div className="panel-body">
      <div style={{ display: 'grid', gap: 22, padding: '8px 0 4px' }}>
        {steps.map((step) => (
          <div key={step.label} style={{ display: 'grid', gridTemplateColumns: '150px minmax(0,1fr)', gap: 16, alignItems: 'center' }}>
            <div style={{ textAlign: 'right' }}>
              <div style={{ fontSize: 'var(--t-13)', fontWeight: 600 }}>{step.label}</div>
              <div style={{ fontSize: 'var(--t-11)', color: 'var(--text-3)' }}>
                {step.value.toLocaleString()}
              </div>
            </div>
            <div>
              <div
                style={{
                  height: 34,
                  width: `${Math.max((step.value / max) * 100, 2)}%`,
                  background: 'var(--accent)',
                  borderRadius: 'var(--r-sm)',
                  opacity: 0.85,
                }}
              />
              <div style={{ fontSize: 'var(--t-11)', color: 'var(--text-3)', marginTop: 6 }}>
                {step.note}
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="legend-row">
        {stats.assets_disputed > 0 && (
          <span className="lg">
            <span className="sw" style={{ background: 'var(--warn)' }} />
            {stats.assets_disputed} disputed
          </span>
        )}
        {Object.entries(stats.assets_by_layer).map(([layer, count]) => (
          <span className="lg" key={layer}>
            {layer} <span className="v">{count}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

function AgentsPanel({ agents }: { agents: AgentProfile[] }) {
  const [counts, setCounts] = useState<Record<string, number>>({});

  useEffect(() => {
    let cancelled = false;
    Promise.all(
      agents.map((agent) =>
        listAgentBindings(agent.agent_key)
          .then((bindings) => [agent.agent_key, bindings.length] as const)
          .catch(() => [agent.agent_key, 0] as const),
      ),
    ).then((pairs) => {
      if (!cancelled) setCounts(Object.fromEntries(pairs));
    });
    return () => {
      cancelled = true;
    };
  }, [agents]);

  if (agents.length === 0) {
    return (
      <div className="stream-empty">
        <h3>No agents connected</h3>
        <p>Point an MCP client at Archivum and it will show up here with what it can read.</p>
      </div>
    );
  }

  const max = Math.max(...Object.values(counts), 1);

  return (
    <div className="panel-body" id="agentList">
      {agents.map((agent) => (
        <div className="agent-card" key={agent.agent_key}>
          <div className="agent-id">
            <Icon name="bot" />
            <span>
              <span className="t">{agent.name || agent.agent_key}</span>
              <br />
              <span className="s">{agent.description || agent.agent_key}</span>
            </span>
          </div>
          <div>
            <div className="bar-track">
              <span
                style={{
                  background: 'var(--accent)',
                  width: `${((counts[agent.agent_key] ?? 0) / max) * 100}%`,
                }}
              />
              <span style={{ background: 'var(--bg-active)', flex: 1 }} />
            </div>
            <div className="agent-meta">
              <span className="chip chip-accent">
                {counts[agent.agent_key] ?? 0} memories bound
              </span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function VisualizedSurface() {
  const [tab, setTab] = useState<Tab>('graph');
  const [owner, setOwner] = useState<OwnerProfile | null>(null);
  const [stats, setStats] = useState<MemoryStats | null>(null);
  const [audit, setAudit] = useState<GraphReport | null>(null);
  const [graph, setGraph] = useState<{ nodes: GraphNode[]; edges: GraphEdge[]; source?: string } | null>(
    null,
  );
  const [agents, setAgents] = useState<AgentProfile[]>([]);
  const [repos, setRepos] = useState<CodeRepo[]>([]);
  // Undefined means the vault itself; a repo scope points the same analysis at
  // code. The clustering and surprise algorithms never cared which graph they
  // were given — only the routes did.
  const [scope, setScope] = useState<string | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    listRepos()
      .then((next) => setRepos(next.filter((repo) => repo.status === 'ready')))
      .catch(() => setRepos([]));
  }, []);

  useEffect(() => {
    Promise.all([
      getOwner(),
      getMemoryStats(),
      getGraphAudit(10, scope).catch(() => null),
      getGraph().catch(() => null),
      listMemoryAgents().catch(() => []),
    ])
      .then(([nextOwner, nextStats, nextAudit, nextGraph, nextAgents]) => {
        setOwner(nextOwner);
        setStats(nextStats);
        setAudit(nextAudit);
        setGraph(nextGraph);
        setAgents(nextAgents);
      })
      .catch((err) => setError(err instanceof Error ? err.message : 'Could not load'));
  }, [scope]);

  if (error) return <div className="surface-error">{error}</div>;


  return (
    <div className="surface on viz-root">
      <div className="col-wide">
        <div className="viz-head">
          <div className="viz-title">
            <h1>Visualized</h1>
            <p>What is actually under the vault — the graph, the memory pipeline, and who reads what.</p>
            {repos.length > 0 && (
              <div className="viz-scope" style={{ display: 'flex', gap: 6, marginTop: 8 }}>
                <button
                  type="button"
                  className={cn('btn btn-sm', scope === undefined ? 'btn-primary' : 'btn-outline')}
                  onClick={() => setScope(undefined)}
                >
                  Your vault
                </button>
                {repos.map((repo) => (
                  <button
                    key={repo.scope}
                    type="button"
                    className={cn('btn btn-sm', scope === repo.scope ? 'btn-primary' : 'btn-outline')}
                    onClick={() => setScope(repo.scope)}
                  >
                    {repo.name}
                  </button>
                ))}
              </div>
            )}
          </div>

          <div className="tiles">
            <div className="tile">
              <div className="lab">Entries</div>
              <div className="num">{owner?.pages ?? '—'}</div>
              <div className="sub">pages on disk</div>
            </div>
            <div className="tile">
              <div className="lab">Links</div>
              <div className="num">{graph ? graph.edges.length : '—'}</div>
              <div className="sub">
                {audit && audit.orphan_ids.length > 0
                  ? `${audit.orphan_ids.length} link to nothing`
                  : 'between your entries'}
              </div>
            </div>
            <div className="tile">
              <div className="lab">Live memory</div>
              <div className="num">{stats?.assets_active ?? '—'}</div>
              <div className="sub">
                {stats ? `${stats.assets_archived} switched off` : ''}
              </div>
            </div>
            <div className="tile">
              <div className="lab">Waiting on you</div>
              <div className="num" style={{ color: 'var(--warn)' }}>
                {stats?.suggestions_pending ?? '—'}
              </div>
              <div className="sub">
                <button
                  type="button"
                  className="btn btn-sm"
                  style={{ padding: 0, height: 'auto' }}
                  onClick={() => navigate('/entries?needs_review=1')}
                >
                  Review them
                </button>
              </div>
            </div>
          </div>

          <div className="viz-tabs">
            <button type="button" className={cn(tab === 'graph' && 'on')} onClick={() => setTab('graph')}>
              <Icon name="graph" />
              Graph
            </button>
            <button type="button" className={cn(tab === 'flow' && 'on')} onClick={() => setTab('flow')}>
              <Icon name="merge" />
              Memory flow
            </button>
            <button type="button" className={cn(tab === 'agents' && 'on')} onClick={() => setTab('agents')}>
              <Icon name="bot" />
              Agents
            </button>
          </div>
        </div>

        <div className="panel">
          <div className="panel-head">
            <div>
              <h2>
                {tab === 'graph'
                  ? 'Knowledge graph'
                  : tab === 'flow'
                    ? 'How something becomes a memory'
                    : 'What your agents can read'}
              </h2>
              <p>
                {tab === 'graph'
                  ? 'You are the root. Clusters hang off you, entries hang off clusters.'
                  : tab === 'flow'
                    ? 'Every claim starts as a proposal. Most do not survive review — that is the pipeline working.'
                    : 'Each agent sees only what it is bound to. Turning a memory off removes it here immediately.'}
              </p>
            </div>
          </div>

          {tab === 'graph' && (
            <KnowledgeGraph
              report={audit}
              onOpen={(nodeId) => {
                // Page records carry their slug in the id; anything else is a
                // record without a page, so there is nowhere to navigate to.
                const match = /^page:[^:]+:(.+)$/.exec(nodeId);
                if (match) navigate(`/wiki/${match[1]}`);
              }}
            />
          )}
          {tab === 'flow' && <FlowPanel stats={stats} />}
          {tab === 'agents' && <AgentsPanel agents={agents} />}
        </div>
      </div>
    </div>
  );
}

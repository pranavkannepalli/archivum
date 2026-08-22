import { useEffect, useMemo, useRef, useState } from 'react';
import { DataSet } from 'vis-data';
import { Network } from 'vis-network';
import type { GraphCommunity, GraphReport } from '../api';
import { cn } from '../lib/cn';

/**
 * The graph as it actually is.
 *
 * This replaces a hand-drawn radial: you in the middle, clusters on a ring,
 * three leaves each. That was a *layout*, not the graph — it showed roughly a
 * sixth of the records and none of the relationships between clusters, so the
 * one thing a knowledge graph is for (this connects to that, through these) was
 * the one thing you could not see.
 *
 * The records were never arranged in rings. Archductor, Archfleet and Archivum
 * reference each other directly and all three reference Perceo; the data had
 * those edges the whole time. So this draws every node and every edge the audit
 * analysed and lets a force layout find the shape.
 *
 * Colour is community membership, which is the one grouping the graph computed
 * rather than one imposed on it.
 */

const PALETTE = [
  '#4B91F1', '#E0724B', '#4BC08A', '#B47BE0', '#E0B24B',
  '#4BC0C0', '#E04B7B', '#8AC04B', '#7B8AE0', '#C04B4B',
];

// Above this the force simulation stops being readable and starts being soup.
// The audit is still complete; this bounds what is drawn, and says so.
const MAX_DRAWN = 400;

const KIND_SHAPE: Record<string, string> = {
  page: 'dot',
  entity: 'diamond',
  symbol: 'dot',
  type: 'triangle',
  file: 'square',
  source: 'square',
  fix: 'star',
  session: 'hexagon',
  community_summary: 'ellipse',
  person: 'star',
};

export default function KnowledgeGraph({
  report,
  onOpen,
}: {
  report: GraphReport | null;
  onOpen: (nodeId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const networkRef = useRef<Network | null>(null);
  const [focus, setFocus] = useState<string | null>(null);
  const [filter, setFilter] = useState('');

  const communityOf = useMemo(() => {
    const map = new Map<string, GraphCommunity>();
    for (const community of report?.communities ?? []) {
      for (const member of community.member_ids) map.set(member, community);
    }
    return map;
  }, [report]);

  const colourOf = useMemo(() => {
    const index = new Map<string, string>();
    (report?.communities ?? []).forEach((community, i) => {
      index.set(community.id, PALETTE[i % PALETTE.length]);
    });
    return index;
  }, [report]);

  useEffect(() => {
    if (!containerRef.current || !report) return;

    const labels = report.node_labels ?? {};
    const kinds = report.node_kinds ?? {};
    const ids = Object.keys(labels).slice(0, MAX_DRAWN);
    const drawn = new Set(ids);

    const nodes = new DataSet(
      ids.map((id) => {
        const community = communityOf.get(id);
        const colour = community ? colourOf.get(community.id) ?? PALETTE[0] : '#6c7086';
        // Degree drives size, so the things everything hangs off read as hubs
        // without anyone deciding in advance which those are.
        const degree = (report.edges ?? []).filter(
          (e) => e.source === id || e.target === id,
        ).length;
        return {
          id,
          label: labels[id],
          title: `${labels[id]}\n${kinds[id] ?? ''}${community ? `\ncluster: ${community.label}` : ''}`,
          shape: KIND_SHAPE[kinds[id]] ?? 'dot',
          size: Math.min(10 + Math.sqrt(degree) * 5, 34),
          color: { background: colour, border: colour, highlight: { background: '#fff', border: colour } },
          font: { color: '#cdd6f4', size: 12 },
        };
      }),
    );

    const edges = new DataSet(
      (report.edges ?? [])
        .filter((e) => drawn.has(e.source) && drawn.has(e.target))
        .map((e, i) => ({
          id: `${e.source}->${e.target}:${i}`,
          from: e.source,
          to: e.target,
          label: e.relation,
          // Inferred links are drawn dashed: the picture should not present a
          // guess and a fact with the same weight.
          dashes: e.extraction_method !== 'EXTRACTED',
          font: { color: '#6c7086', size: 9, strokeWidth: 0, align: 'middle' as const },
          color: { color: '#3a3a4a', highlight: '#4B91F1', opacity: 0.75 },
          arrows: { to: { enabled: true, scaleFactor: 0.4 } },
          smooth: { enabled: true, type: 'continuous', roundness: 0.5 },
        })),
    );

    const network = new Network(
      containerRef.current,
      { nodes, edges: edges as never },
      {
        physics: {
          solver: 'forceAtlas2Based',
          forceAtlas2Based: { gravitationalConstant: -60, springLength: 110, avoidOverlap: 0.4 },
          stabilization: { iterations: 220, fit: true },
          // Stop the moment it settles rather than easing off forever. Left to
          // decay on its own the whole graph keeps rotating and swimming under
          // the cursor, which makes it unreadable and impossible to click.
          minVelocity: 0.75,
        },
        interaction: { hover: true, tooltipDelay: 120, navigationButtons: false },
        nodes: { borderWidth: 1.5 },
        edges: { width: 1 },
        layout: { improvedLayout: false },
      },
    );

    // Freeze the layout once it has settled. Physics is how the shape is found,
    // not how it should be held: a live simulation drifts continuously, so the
    // node you reached for is somewhere else by the time you click.
    const freeze = () => network.setOptions({ physics: { enabled: false } });
    network.once('stabilizationIterationsDone', freeze);
    network.once('stabilized', freeze);

    network.on('click', (params) => {
      const id = params.nodes[0] ? String(params.nodes[0]) : null;
      setFocus(id);
      if (id) onOpen(id);
    });

    networkRef.current = network;
    return () => {
      network.destroy();
      networkRef.current = null;
    };
  }, [report, communityOf, colourOf, onOpen]);

  // Search moves the camera rather than hiding nodes: losing the surrounding
  // shape is what makes a filtered graph useless.
  useEffect(() => {
    const network = networkRef.current;
    if (!network || !report || !filter.trim()) return;
    const needle = filter.trim().toLowerCase();
    const hit = Object.entries(report.node_labels ?? {}).find(([, label]) =>
      label.toLowerCase().includes(needle),
    );
    if (hit) {
      network.selectNodes([hit[0]]);
      network.focus(hit[0], { scale: 1.1, animation: { duration: 400, easingFunction: 'easeInOutQuad' } });
      setFocus(hit[0]);
    }
  }, [filter, report]);

  if (!report) return <div className="surface-loading">Drawing your graph…</div>;

  const total = Object.keys(report.node_labels ?? {}).length;
  const focusLabel = focus ? report.node_labels?.[focus] : null;

  return (
    <>
      <div className="graph-bar">
        <input
          className="graph-search"
          placeholder="Find a record…"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
        <span className="graph-meta">
          {Math.min(total, MAX_DRAWN)} of {report.node_count} records · {report.edge_count} links
          {total > MAX_DRAWN && ' · showing the first 400'}
        </span>
        {focusLabel && <span className="chip chip-accent">{focusLabel}</span>}
      </div>
      <div ref={containerRef} className="graph-canvas" />
      <div className="legend">
        <div className={cn('legend-row')}>
          {(report.communities ?? []).slice(0, 8).map((community) => (
            <span key={community.id} className="legend-item">
              <span className="dot" style={{ background: colourOf.get(community.id) }} />
              {community.label} · {community.size}
            </span>
          ))}
        </div>
        <div className="legend-note">
          Colour is a cluster the graph found, not a folder you made. Dashed links were
          inferred rather than read directly.
        </div>
      </div>
    </>
  );
}

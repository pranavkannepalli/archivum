import type { Entry, FoundPage, VaultHit } from '../api';

/**
 * What the list shows while a search is active.
 *
 * This used to intersect the hits with the entries already loaded — a page of
 * at most 200 rows, further narrowed by whatever facet was selected. Anything
 * the engine found that was not already on screen was dropped without a trace,
 * so a search could rank a page first and then not show it. Search is supposed
 * to reach the whole vault; filtering its answer through a partial local list
 * gives back exactly the thing the local list could already do.
 *
 * Now the hits decide membership and order, and the loaded entry is used only
 * to fill in what the search result does not carry.
 */
export function searchResults(
  all: Entry[],
  hits: VaultHit[],
  found: FoundPage[] = [],
): Entry[] {
  // Literal matches lead. Semantic search decides what a page is *about* and
  // will decline a weak match — correct for "things about retrieval", wrong
  // when you typed a file name or a string you know is in a page, which used to
  // come back empty.
  const ordered: VaultHit[] = [
    ...found.map((hit) => ({
      slug: hit.slug,
      title: hit.title,
      excerpt: hit.excerpt,
      score: 1,
    })),
    ...hits,
  ];
  return rank(all, ordered);
}

function rank(all: Entry[], hits: VaultHit[]): Entry[] {
  const known = new Map(all.filter((entry) => entry.slug).map((entry) => [entry.slug!, entry]));
  const seen = new Set<string>();
  const results: Entry[] = [];

  for (const hit of hits) {
    if (!hit.slug || seen.has(hit.slug)) continue;
    seen.add(hit.slug);
    const entry = known.get(hit.slug);
    if (entry) {
      // Prefer the excerpt: it is the line that explains why this matched.
      results.push(hit.excerpt ? { ...entry, detail: hit.excerpt } : entry);
      continue;
    }
    // A hit for something outside the loaded page still has to be reachable.
    results.push({
      id: `page:${hit.slug}`,
      kind: 'note',
      title: hit.title || hit.slug,
      slug: hit.slug,
      folder: hit.slug.includes('/') ? hit.slug.slice(0, hit.slug.lastIndexOf('/')) : '',
      updated_at: '',
      created_at: '',
      actor: 'you',
      needs_review: false,
      tags: [],
      detail: hit.excerpt ?? '',
    });
  }

  return results;
}

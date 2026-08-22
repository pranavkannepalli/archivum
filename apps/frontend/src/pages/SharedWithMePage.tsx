import { useEffect, useState } from 'react';
import { listSharedWithMe, type SharedListing } from '../api';
import { Icon } from '../shell/Icon';

/**
 * A recipient's whole world: the things they were given, and nothing else.
 *
 * Deliberately not the app shell — a recipient is not a member of this vault,
 * so they get a reading surface rather than a sidebar full of doors that would
 * all be locked.
 */
const ICON_FOR: Record<string, string> = {
  entry: 'file',
  folder: 'folder',
  asset: 'sparkles',
  view: 'search',
  scope: 'layers',
  source: 'database',
};

export default function SharedWithMePage() {
  const [items, setItems] = useState<SharedListing[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listSharedWithMe()
      .then(setItems)
      .catch((err: unknown) =>
        setError(
          err instanceof Error ? err.message : 'Could not load what was shared with you',
        ),
      );
  }, []);

  return (
    <div className="shared-root">
      <div className="col">
        <div className="shared-index-head">
          <span className="eyebrow">Shared with you</span>
          <h1>What you can see</h1>
          <p className="shared-note">
            Someone gave you access to these. Everything else in their vault stays
            theirs.
          </p>
        </div>

        {error && <div className="share-error">{error}</div>}

        {items && items.length === 0 && (
          <div className="stream-empty">
            <h3>Nothing yet</h3>
            <p>
              Your invitation worked, but nothing has been shared with you at the
              moment.
            </p>
          </div>
        )}

        <div className="rows">
          {(items ?? []).map((item) => (
            <a
              key={item.urn}
              className="row-i"
              href={`/shared/view?urn=${encodeURIComponent(item.urn)}`}
            >
              <Icon name={ICON_FOR[item.kind] ?? 'file'} />
              <span>
                <span className="t">{item.title}</span>
                <span className="s">{item.kind}</span>
              </span>
              <span className="meta">
                {item.role === 'commenter' ? 'can suggest' : 'can read'}
              </span>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}

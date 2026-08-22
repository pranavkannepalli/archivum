import { useCallback, useEffect, useState } from 'react';
import { approveShareHold, listShareHolds, type ShareHold } from '../api';
import { useToast } from '../components/ui/Toast';
import { Icon } from '../shell/Icon';

/**
 * Things an agent filed somewhere a person can read, waiting on you.
 *
 * This is the visible half of the review gate. Shared folders stay live — drop
 * a note in and the recipient sees it — but when an *agent* put it there, it
 * waits here first. Without this surface the gate would be silent, and a share
 * would just look mysteriously empty.
 */
function nameOf(urn: string): string {
  const parts = urn.split(':');
  const local = parts.slice(2).join(':');
  return local.split('/').pop() || local || urn;
}

export default function ShareHolds() {
  const [holds, setHolds] = useState<ShareHold[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const { push } = useToast();

  const load = useCallback(() => {
    listShareHolds()
      .then(setHolds)
      .catch(() => setHolds([]));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function approve(hold: ShareHold) {
    const key = `${hold.grant_id}:${hold.resource_urn}`;
    setBusy(key);
    try {
      await approveShareHold(hold.grant_id, hold.resource_urn);
      setHolds((prev) =>
        prev.filter(
          (item) =>
            !(
              item.grant_id === hold.grant_id &&
              item.resource_urn === hold.resource_urn
            ),
        ),
      );
      push({
        kind: 'success',
        title: `${hold.display_name ?? 'They'} can see it now`,
        description: nameOf(hold.resource_urn),
      });
    } catch (err) {
      push({
        kind: 'error',
        title: "Couldn't share that",
        description: err instanceof Error ? err.message : 'Unknown error',
      });
    } finally {
      setBusy(null);
    }
  }

  if (holds.length === 0) return null;

  return (
    <div className="self-sec" style={{ paddingTop: 18 }}>
      <div className="hd">
        <h2>Waiting to be shared</h2>
        <p>· an agent filed these somewhere someone can read</p>
      </div>

      {holds.map((hold) => {
        const key = `${hold.grant_id}:${hold.resource_urn}`;
        return (
          <div key={key} className="share-hold">
            <div className="txt">
              Archivum filed <b>{nameOf(hold.resource_urn)}</b> into{' '}
              <b>{nameOf(hold.grant_urn) || 'a shared folder'}</b>, which{' '}
              {hold.display_name ?? 'someone'} can read.
              <div className="sub">
                <span className="chip chip-warn">held</span>
                <span>
                  {hold.display_name ?? 'They'} cannot see it until you say so
                </span>
              </div>
            </div>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy === key}
              onClick={() => void approve(hold)}
            >
              <Icon name="check" />
              Show them
            </button>
          </div>
        );
      })}
    </div>
  );
}

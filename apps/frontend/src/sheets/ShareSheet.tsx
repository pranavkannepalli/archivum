import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  createShareGrant,
  createSharePrincipal,
  listShareAccess,
  revokeShareGrant,
  type ShareAccessRow,
  type ShareRole,
  type ShareTarget,
} from '../api';
import { Icon } from '../shell/Icon';
import { cn } from '../lib/cn';

/**
 * Sharing is a verb, so it lives in a sheet rather than a screen — the same
 * surface Capture and Ask use.
 *
 * Two ways to share sit in one list because they are one thing underneath: a
 * grant. Naming a person creates a grant with a claim link you send them;
 * flipping "anyone with the link" creates a grant whose holder is the URL.
 */

function initialsOf(name: string): string {
  const parts = name.split(' ').filter(Boolean);
  if (parts.length === 0) return '··';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function absoluteUrl(path: string): string {
  if (typeof window === 'undefined') return path;
  return `${window.location.origin}${path}`;
}

export default function ShareSheet({
  open,
  target,
  resourceTitle,
  citedCount = 0,
  onClose,
}: {
  open: boolean;
  target: ShareTarget;
  resourceTitle: string;
  citedCount?: number;
  onClose: () => void;
}) {
  const [rows, setRows] = useState<ShareAccessRow[]>([]);
  const [name, setName] = useState('');
  const [role, setRole] = useState<ShareRole>('viewer');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [claimUrl, setClaimUrl] = useState<string | null>(null);
  const [claimFor, setClaimFor] = useState<string>('');
  const [linkUrl, setLinkUrl] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const refresh = useCallback(async () => {
    try {
      setRows(await listShareAccess(target));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load who has access');
    }
  }, [target.resource_kind, target.resource_id]);

  useEffect(() => {
    if (!open) return;
    setName('');
    setRole('viewer');
    setError(null);
    setClaimUrl(null);
    setLinkUrl(null);
    setCopied(null);
    void refresh();
    const id = window.setTimeout(() => inputRef.current?.focus(), 10);
    return () => window.clearTimeout(id);
  }, [open, refresh]);

  const people = useMemo(
    () => rows.filter((row) => row.subject_kind === 'principal'),
    [rows],
  );
  const linkRow = useMemo(
    () => rows.find((row) => row.subject_kind === 'link') ?? null,
    [rows],
  );

  async function copy(value: string, key: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(key);
      window.setTimeout(() => setCopied(null), 1600);
    } catch {
      setError('Could not copy — select the text and copy it by hand.');
    }
  }

  async function shareWithPerson() {
    const displayName = name.trim();
    if (!displayName || busy) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createSharePrincipal(displayName);
      await createShareGrant({
        ...target,
        principal_id: created.principal.id,
        role,
      });
      setClaimUrl(absoluteUrl(created.claim_url));
      setClaimFor(displayName);
      setName('');
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not share that');
    } finally {
      setBusy(false);
    }
  }

  async function toggleLink() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      if (linkRow) {
        await revokeShareGrant(linkRow.id);
        setLinkUrl(null);
      } else {
        const created = await createShareGrant({
          ...target,
          subject_kind: 'link',
          role: 'viewer',
        });
        if (created.share_url) setLinkUrl(absoluteUrl(created.share_url));
      }
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not change the link');
    } finally {
      setBusy(false);
    }
  }

  async function revoke(row: ShareAccessRow) {
    setBusy(true);
    try {
      await revokeShareGrant(row.id);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not remove that');
    } finally {
      setBusy(false);
    }
  }

  if (!open) return null;

  return (
    <div
      className="overlay on"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="sheet" role="dialog" aria-label={`Share ${resourceTitle}`}>
        <div className="sheet-in">
          <Icon name="users" size={18} />
          <input
            ref={inputRef}
            value={name}
            autoComplete="off"
            placeholder={`Share “${resourceTitle}” with…`}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') {
                event.preventDefault();
                void shareWithPerson();
              }
            }}
          />
          <div className="seg" style={{ marginLeft: 0 }}>
            <button
              type="button"
              className={cn(role === 'viewer' && 'on')}
              onClick={() => setRole('viewer')}
            >
              Can read
            </button>
            <button
              type="button"
              className={cn(role === 'commenter' && 'on')}
              onClick={() => setRole('commenter')}
            >
              Can suggest
            </button>
          </div>
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!name.trim() || busy}
            onClick={() => void shareWithPerson()}
          >
            Share
          </button>
        </div>

        {claimUrl && (
          <div className="share-claim">
            <div className="share-claim-head">
              <Icon name="link" size={14} />
              <span>
                Send this to <b>{claimFor}</b>. It works once.
              </span>
            </div>
            <div className="share-copy">
              <input readOnly value={claimUrl} onFocus={(e) => e.currentTarget.select()} />
              <button
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => void copy(claimUrl, 'claim')}
              >
                {copied === 'claim' ? 'Copied' : 'Copy'}
              </button>
            </div>
          </div>
        )}

        <div className="sheet-list">
          {people.length === 0 && !linkRow && (
            <div className="share-empty">
              Only you can see this. Nobody else has been given access.
            </div>
          )}

          {people.map((row) => (
            <div key={row.id} className="sheet-item share-person">
              <span className="share-av">{initialsOf(row.display_name ?? '?')}</span>
              <span className="share-name">{row.display_name}</span>
              <span className="share-tail">
                <span className={cn('chip', row.role === 'commenter' && 'chip-accent')}>
                  {row.role === 'commenter' ? 'can suggest' : 'can read'}
                </span>
                <button
                  type="button"
                  className="btn btn-icon btn-sm"
                  aria-label={`Remove ${row.display_name}`}
                  disabled={busy}
                  onClick={() => void revoke(row)}
                >
                  <Icon name="x" />
                </button>
              </span>
            </div>
          ))}

          <div className="sheet-item share-person">
            <span className="share-av share-av-link">
              <Icon name={linkRow ? 'link' : 'lock'} size={13} />
            </span>
            <span className="share-name">
              Anyone with the link
              <span className="share-sub">
                {linkRow ? 'can read this, no sign-in' : 'off — link does nothing'}
              </span>
            </span>
            <span className="share-tail">
              <button
                type="button"
                className={cn('toggle', linkRow && 'on')}
                aria-label="Anyone with the link"
                aria-pressed={Boolean(linkRow)}
                disabled={busy}
                onClick={() => void toggleLink()}
              />
            </span>
          </div>

          {linkUrl && (
            <div className="share-copy share-copy-inset">
              <input readOnly value={linkUrl} onFocus={(e) => e.currentTarget.select()} />
              <button
                type="button"
                className="btn btn-outline btn-sm"
                onClick={() => void copy(linkUrl, 'link')}
              >
                {copied === 'link' ? 'Copied' : 'Copy link'}
              </button>
            </div>
          )}
        </div>

        {error && <div className="share-error">{error}</div>}

        <div className="sheet-foot">
          <span>
            <span className="kbd">↵</span> share
          </span>
          {citedCount > 0 && (
            <span>
              {citedCount} source{citedCount === 1 ? '' : 's'} cited · not shared
            </span>
          )}
          <span style={{ marginLeft: 'auto' }}>
            <span className="kbd">esc</span> close
          </span>
        </div>
      </div>
    </div>
  );
}

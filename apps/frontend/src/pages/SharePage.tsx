import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams, useSearchParams } from 'react-router-dom';
import {
  commentOnShare,
  getSharedResource,
  openShareLink,
  type SharedResource,
} from '../api';
import { Icon } from '../shell/Icon';
import { renderMarkdown } from './markdown';

/**
 * What a recipient sees.
 *
 * Built on the app's own tokens rather than a stripped-down public theme: a
 * shared page should look like the place it came from. Citations render as
 * plain titles unless the cited source was itself shared — linking them into a
 * login wall is worse than not linking them at all.
 */
export default function SharePage() {
  const params = useParams();
  const [search] = useSearchParams();
  const token = params['token'];
  const urn = search.get('urn');

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [resource, setResource] = useState<SharedResource | null>(null);
  const [comment, setComment] = useState('');
  // Navigating within a share keeps the credential you arrived with: a link
  // holder stays on their token, a claimed recipient rides their session.
  const linkTo = (target: string) =>
    token
      ? `/share/${token}?urn=${encodeURIComponent(target)}`
      : `/shared/view?urn=${encodeURIComponent(target)}`;
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // A urn means the recipient is navigating within what they were given;
      // a bare token means they just opened the link they were sent.
      if (urn) setResource(await getSharedResource(urn, token));
      else if (token) setResource(await openShareLink(token));
      else setError('This link is missing its token.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'This link no longer works.');
    } finally {
      setLoading(false);
    }
  }, [token, urn]);

  useEffect(() => {
    void load();
  }, [load]);

  const html = useMemo(
    // A recipient has no vault to open, so a wikilink renders as text rather
    // than as a link to a login wall.
    () => (resource?.body ? renderMarkdown(resource.body, { wikilinks: 'text' }) : ''),
    [resource?.body],
  );

  async function submitComment() {
    const text = comment.trim();
    if (!text || !resource || sending) return;
    setSending(true);
    try {
      await commentOnShare(resource.urn, text);
      setComment('');
      setSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not send that');
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="shared-root">
      <div className="col">
        {loading && (
          <div className="shared-doc">
            <div className="skeleton h-6 w-2/3" />
            <div className="skeleton mt-3 h-4 w-full" />
            <div className="skeleton mt-2 h-4 w-5/6" />
          </div>
        )}

        {!loading && error && (
          <div className="shared-gone">
            <Icon name="lock" size={22} />
            <h1>Nothing here</h1>
            <p>{error}</p>
          </div>
        )}

        {!loading && resource && (
          <article className="shared-doc">
            <div className="shared-eyebrow">
              <span className="eyebrow">Shared with you</span>
              <span className={`chip ${resource.may_comment ? 'chip-accent' : ''}`}>
                {resource.may_comment ? 'you can suggest edits' : 'read only'}
              </span>
            </div>

            <h1>{resource.title}</h1>

            {resource.tags.length > 0 && (
              <div className="shared-tags">
                {resource.tags.slice(0, 8).map((tag) => (
                  <span key={tag} className="chip">
                    {tag}
                  </span>
                ))}
              </div>
            )}

            {resource.kind === 'folder' ? (
              <div className="shared-children">
                {resource.children.length === 0 ? (
                  <p className="shared-note">
                    Nothing in here has been shared with you yet.
                  </p>
                ) : (
                  resource.children.map((child) => (
                    <a
                      key={child.urn}
                      className="row-i shared-child"
                      href={linkTo(child.urn)}
                    >
                      <Icon name="file" />
                      <span>
                        <span className="t">{child.title}</span>
                      </span>
                    </a>
                  ))
                )}
              </div>
            ) : (
              <div className="body" dangerouslySetInnerHTML={{ __html: html }} />
            )}

            {resource.citations.length > 0 && (
              <div className="shared-sources">
                <span className="eyebrow">Sources</span>
                {resource.citations.map((citation, index) => (
                  <div key={`${citation.title}-${index}`} className="shared-source">
                    <span className="cite">{index + 1}</span>
                    {citation.urn ? (
                      <a href={linkTo(citation.urn)}>
                        {citation.title}
                      </a>
                    ) : (
                      // Cited but not shared: named, deliberately not linked.
                      <span className="shared-source-locked">
                        {citation.title}
                        <Icon name="lock" size={11} />
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}

            {resource.may_comment && (
              <div className="shared-comment">
                {sent ? (
                  <p className="shared-note">
                    Sent. It is waiting for the owner to look at — nothing changed yet.
                  </p>
                ) : (
                  <>
                    <span className="eyebrow">Suggest a change</span>
                    <textarea
                      value={comment}
                      placeholder="What should this say instead?"
                      onChange={(event) => setComment(event.target.value)}
                    />
                    <div className="shared-comment-foot">
                      <span>Goes to the owner's review queue, not the page.</span>
                      <button
                        type="button"
                        className="btn btn-primary btn-sm"
                        disabled={!comment.trim() || sending}
                        onClick={() => void submitComment()}
                      >
                        {sending ? 'Sending…' : 'Send'}
                      </button>
                    </div>
                  </>
                )}
              </div>
            )}

            <div className="tracebar">
              <Icon name="eye" size={13} />
              You are seeing one part of someone's Archivum. Nothing else is reachable
              from here.
            </div>
          </article>
        )}
      </div>
    </div>
  );
}

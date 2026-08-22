import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { claimShare } from '../api';
import { Icon } from '../shell/Icon';

/**
 * Claiming turns a link somebody sent you into a session.
 *
 * There is no sign-up: the token *is* the credential, it works once, and what
 * it buys you is access to exactly what you were granted.
 */
export default function ClaimPage() {
  const params = useParams();
  const navigate = useNavigate();
  const token = params['token'];

  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      setError('This invitation is missing its token.');
      return;
    }
    let cancelled = false;
    claimShare(token)
      .then((claimed) => {
        if (cancelled) return;
        setName(claimed.display_name);
        window.setTimeout(() => navigate('/shared', { replace: true }), 900);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(
          err instanceof Error
            ? err.message
            : 'This invitation has already been used, or it never existed.',
        );
      });
    return () => {
      cancelled = true;
    };
  }, [token, navigate]);

  return (
    <div className="shared-root">
      <div className="col">
        <div className="shared-claim-card">
          {error ? (
            <>
              <Icon name="lock" size={22} />
              <h1>That invitation is spent</h1>
              <p>{error}</p>
              <p className="shared-note">
                Invitations work once. Ask whoever sent it for a fresh one.
              </p>
            </>
          ) : name ? (
            <>
              <Icon name="check" size={22} />
              <h1>You're in, {name}</h1>
              <p>Taking you to what has been shared with you…</p>
            </>
          ) : (
            <>
              <h1>Opening your invitation…</h1>
              <div className="skeleton mt-4 h-4 w-2/3" />
            </>
          )}
        </div>
      </div>
    </div>
  );
}

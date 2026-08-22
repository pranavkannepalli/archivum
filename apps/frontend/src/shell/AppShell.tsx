import { useCallback, useEffect, useMemo, useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { ensureDailyNote, getOwner, listEntries, type Entry, type OwnerProfile } from '../api';
import { useAppDispatch, useAppState } from '../store';
import { cn } from '../lib/cn';
import { Icon } from './Icon';
import { BrandMark } from './BrandMark';
import VaultTree from './VaultTree';
import CaptureSheet from '../sheets/CaptureSheet';
import AskSheet from '../sheets/AskSheet';

/**
 * The whole application chrome: one sidebar, one canvas, two sheets.
 *
 * There is no tab bar and no tool section. Nouns you return to live in the
 * sidebar; verbs (capture, ask, go to file) are keyboard-first sheets.
 */

type NavKey = 'stream' | 'everything' | 'visualized' | 'self';

function useOwner(): OwnerProfile | null {
  const [owner, setOwner] = useState<OwnerProfile | null>(null);
  useEffect(() => {
    let cancelled = false;
    getOwner()
      .then((next) => {
        if (!cancelled) setOwner(next);
      })
      .catch(() => {
        // The profile is decoration on every surface except /me, which renders
        // its own error. Failing quietly here keeps the shell usable.
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return owner;
}

export default function AppShell() {
  const { pages, currentSlug } = useAppState();
  const dispatch = useAppDispatch();
  const navigate = useNavigate();
  const location = useLocation();
  const owner = useOwner();

  const [captureOpen, setCaptureOpen] = useState(false);
  const [askOpen, setAskOpen] = useState(false);
  const [askMode, setAskMode] = useState<'ask' | 'file'>('ask');
  const [waitingEntries, setWaitingEntries] = useState<Entry[]>([]);

  const refreshWaiting = useCallback(() => {
    listEntries({ needsReview: true, limit: 200 })
      .then((res) => setWaitingEntries(res.entries))
      .catch(() => setWaitingEntries([]));
  }, []);

  useEffect(() => {
    refreshWaiting();
  }, [refreshWaiting, location.pathname]);

  const waitingSlugs = useMemo(
    () => new Set(waitingEntries.map((entry) => entry.slug).filter(Boolean) as string[]),
    [waitingEntries],
  );

  const active: NavKey = location.pathname.startsWith('/entries')
    ? 'everything'
    : location.pathname.startsWith('/visualized')
      ? 'visualized'
      : location.pathname.startsWith('/me')
        ? 'self'
        : 'stream';

  const theme = useAppState().theme;

  const openSheet = useCallback((mode: 'ask' | 'file') => {
    setCaptureOpen(false);
    setAskMode(mode);
    setAskOpen(true);
  }, []);

  /**
   * Open today's note, creating it on the first ask.
   *
   * The endpoint existed from the start and nothing called it, so there was no
   * "today" — the one page a daily-notes habit needs to be one keystroke away.
   */
  const openToday = useCallback(async () => {
    try {
      const page = await ensureDailyNote();
      navigate(`/wiki/${page.slug}`);
    } catch {
      // Never block the shell on it; the vault is still usable without today.
    }
  }, [navigate]);

  useEffect(() => {
    function onKeydown(event: KeyboardEvent) {
      const target = event.target as HTMLElement | null;
      const typing =
        target instanceof HTMLElement &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.isContentEditable);

      const mod = event.metaKey || event.ctrlKey;
      if (mod && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        openSheet('ask');
        return;
      }
      if (mod && event.key.toLowerCase() === 'p') {
        event.preventDefault();
        openSheet('file');
        return;
      }
      if (event.key === 'Escape') {
        setCaptureOpen(false);
        setAskOpen(false);
        return;
      }
      if (typing || mod) return;

      const key = event.key.toLowerCase();
      if (key === 't') {
        event.preventDefault();
        void openToday();
        return;
      }
      if (key === 'c') {
        event.preventDefault();
        setAskOpen(false);
        setCaptureOpen(true);
      } else if (key === 's') navigate('/');
      else if (key === 'e') navigate('/entries');
      else if (key === 'v') navigate('/visualized');
    }

    window.addEventListener('keydown', onKeydown);
    return () => window.removeEventListener('keydown', onKeydown);
  }, [navigate, openSheet, openToday]);

  const pinned = useMemo(() => pages.slice(0, 3), [pages]);

  return (
    <div className="app" data-sb="open">
      <aside className="sidebar">
        <div className="sb-top">
          <div className="brand">
            <BrandMark size={26} />
            <b>Archivum</b>
          </div>

          <button type="button" className="selfrow" onClick={() => navigate('/me')} data-active={active === 'self'}>
            <span className="selfav">{owner?.initials ?? '··'}</span>
            <span className="selfmeta">
              <span className="t">{owner?.name ?? 'Loading…'}</span>
              <span className="s">you · the centre of it all</span>
            </span>
          </button>

          <button type="button" className="capture-btn" onClick={() => setCaptureOpen(true)}>
            <Icon name="plus" />
            Capture
            <span className="kbd">C</span>
          </button>

          <div className="nav">
            <button type="button" className={cn(active === 'stream' && 'on')} onClick={() => navigate('/')}>
              <Icon name="layers" />
              Stream
            </button>
            <button
              type="button"
              className={cn(active === 'everything' && 'on')}
              onClick={() => navigate('/entries')}
            >
              <Icon name="search" />
              Everything
              <span className="n">{pages.length || ''}</span>
            </button>
            {/* Two different things, so two buttons. Find was previously
                reachable only by knowing ⌘P, which meant text search inside
                pages was, in practice, not in the product. */}
            <button type="button" onClick={() => openSheet('file')}>
              <Icon name="search" />
              Find text
              <span className="kbd" style={{ marginLeft: 'auto' }}>⌘P</span>
            </button>
            <button type="button" onClick={() => openSheet('ask')}>
              <Icon name="sparkles" />
              Ask a question
              <span className="kbd" style={{ marginLeft: 'auto' }}>⌘K</span>
            </button>
            <button type="button" onClick={openToday}>
              <Icon name="clock" />
              Today
              <span className="kbd" style={{ marginLeft: 'auto' }}>T</span>
            </button>
            <button type="button" onClick={() => navigate('/entries?needs_review=1')}>
              <Icon name="check" />
              Needs you
              {waitingEntries.length > 0 && <span className="badge">{waitingEntries.length}</span>}
            </button>
            <button
              type="button"
              className={cn(active === 'visualized' && 'on')}
              onClick={() => navigate('/visualized')}
            >
              <Icon name="graph" />
              Visualized
            </button>
          </div>
        </div>

        <div className="sb-scroll">
          {pinned.length > 0 && (
            <div className="sec" data-open="true">
              <div className="sb-head-row">
                <span className="sb-head">
                  <Icon name="chevronDown" />
                  <span>Recent</span>
                </span>
              </div>
              <div className="sec-body">
                {pinned.map((page) => (
                  <button
                    key={page.slug}
                    type="button"
                    className="pin"
                    onClick={() => navigate(`/wiki/${page.slug}`)}
                  >
                    <Icon name="file" />
                    <span>{page.title || page.slug}</span>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="sec" data-open="true">
            <div className="sb-head-row">
              <span className="sb-head">
                <Icon name="chevronDown" />
                <span>Vault</span>
              </span>
            </div>
            <div className="sec-body">
              <VaultTree pages={pages} currentSlug={currentSlug ?? null} waiting={waitingSlugs} />
            </div>
          </div>
        </div>

        <div className="sb-foot">
          <span className="avatar">{owner?.initials ?? '··'}</span>
          <span className="health">
            <b>{owner ? `${owner.memories_active} memories live` : 'Loading…'}</b>
            {owner ? `${owner.agents} agent${owner.agents === 1 ? '' : 's'} connected` : ''}
          </span>
          <button
            type="button"
            className="btn btn-icon"
            title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
            onClick={() => dispatch({ type: 'TOGGLE_THEME' })}
          >
            <Icon name={theme === 'dark' ? 'sun' : 'moon'} />
          </button>
        </div>
      </aside>

      <main className="canvas">
        <Outlet />
      </main>

      <CaptureSheet
        open={captureOpen}
        onClose={() => setCaptureOpen(false)}
        onCaptured={(slug) => {
          setCaptureOpen(false);
          if (slug) navigate(`/wiki/${slug}`);
        }}
      />
      <AskSheet
        open={askOpen}
        mode={askMode}
        onModeChange={setAskMode}
        onClose={() => setAskOpen(false)}
        pages={pages}
      />
    </div>
  );
}

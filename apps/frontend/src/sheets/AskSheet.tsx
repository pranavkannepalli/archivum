import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { findPages, query, type FoundPage } from '../api';
import type { Page } from '../types';
import { Icon } from '../shell/Icon';
import { cn } from '../lib/cn';

/**
 * One sheet, two jobs that are easy to confuse.
 *
 * **Find** (⌘P) is literal and instant: it matches page names *and page text*
 * against the FTS index, and shows the line it matched on. Nothing is sent
 * anywhere. Use it when you know the words.
 *
 * **Ask** (⌘K) sends a question to a model and streams back an answer with
 * citations into the pages it used. It takes seconds, because something is
 * reading for you.
 *
 * They share a surface because they answer the same impulse — "get me to the
 * thing" — but each one is now labelled with what it does and what it costs,
 * because a box that sometimes greps and sometimes calls a model is a box you
 * cannot predict. Find used to match titles only, and only against the pages
 * already loaded in the browser, which meant text inside a file was unreachable
 * from anywhere in the app.
 */

type Mode = 'ask' | 'file';

export default function AskSheet({
  open,
  mode,
  onModeChange,
  onClose,
  pages,
}: {
  open: boolean;
  mode: Mode;
  onModeChange: (mode: Mode) => void;
  onClose: () => void;
  pages: Page[];
}) {
  const [text, setText] = useState('');
  const [cursor, setCursor] = useState(0);
  const [answer, setAnswer] = useState('');
  const [citations, setCitations] = useState<Page[]>([]);
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (!open) return;
    setText('');
    setCursor(0);
    setAnswer('');
    setCitations([]);
    setError(null);
    const id = window.setTimeout(() => inputRef.current?.focus(), 10);
    return () => window.clearTimeout(id);
  }, [open, mode]);

  // Local title matching answers immediately from what is already loaded, so
  // the list never goes blank while the index round-trips.
  const localMatches = useMemo(() => {
    const needle = text.trim().toLowerCase();
    const ranked = needle
      ? pages.filter(
          (page) =>
            page.title.toLowerCase().includes(needle) ||
            page.slug.toLowerCase().includes(needle),
        )
      : pages;
    return ranked.slice(0, 40);
  }, [pages, text]);

  const [found, setFound] = useState<FoundPage[] | null>(null);

  useEffect(() => {
    if (mode !== 'file') return;
    const needle = text.trim();
    if (!needle) {
      setFound(null);
      return;
    }
    let cancelled = false;
    // Short: this is an index lookup, not a model call. Waiting on it should
    // not be perceptible.
    const id = window.setTimeout(() => {
      findPages(needle)
        .then((hits) => !cancelled && setFound(hits))
        .catch(() => !cancelled && setFound(null));
    }, 90);
    return () => {
      cancelled = true;
      window.clearTimeout(id);
    };
  }, [mode, text]);

  // Names first — if you typed a file name you want the file, not a page that
  // mentions it — then everything the text search turned up.
  const matches = useMemo(() => {
    if (!found) return localMatches;
    const byName = new Map(localMatches.map((page) => [page.slug, page]));
    const rest = found
      .filter((hit) => !byName.has(hit.slug))
      .map((hit) => ({
        slug: hit.slug,
        title: hit.title,
        content: hit.excerpt,
        tags: [],
      })) as unknown as Page[];
    return [...localMatches, ...rest].slice(0, 40);
  }, [localMatches, found]);

  const excerptFor = useMemo(() => {
    const index = new Map((found ?? []).map((hit) => [hit.slug, hit.excerpt]));
    return (slug: string) => index.get(slug) ?? '';
  }, [found]);

  useEffect(() => {
    setCursor(0);
  }, [text]);

  async function ask() {
    const question = text.trim();
    if (!question || asking) return;
    setAsking(true);
    setAnswer('');
    setCitations([]);
    setError(null);
    try {
      await query(
        question,
        (token) => setAnswer((prev) => prev + token),
        (next) => setCitations(next),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong');
    } finally {
      setAsking(false);
    }
  }

  function openMatch(page: Page) {
    onClose();
    navigate(`/wiki/${page.slug}`);
  }

  if (!open) return null;

  return (
    <div
      className="overlay on"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div className="sheet" role="dialog" aria-label={mode === 'ask' ? 'Ask Archivum' : 'Go to file'}>
        <div className="sheet-in">
          <Icon name={mode === 'ask' ? 'sparkles' : 'search'} size={18} />
          <span className="mode">{mode === 'ask' ? 'Ask' : 'Find'}</span>
          <input
            ref={inputRef}
            value={text}
            autoComplete="off"
            placeholder={
              mode === 'ask'
                ? 'Ask a question — an assistant reads your vault and answers with sources'
                : 'Find text in any page, or a page by name'
            }
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'ArrowDown') {
                event.preventDefault();
                setCursor((c) => Math.min(c + 1, matches.length - 1));
              } else if (event.key === 'ArrowUp') {
                event.preventDefault();
                setCursor((c) => Math.max(c - 1, 0));
              } else if (event.key === 'Enter') {
                event.preventDefault();
                if (mode === 'ask') void ask();
                else if (matches[cursor]) openMatch(matches[cursor]);
              }
            }}
          />
          <button
            type="button"
            className="btn btn-sm"
            onClick={() => onModeChange(mode === 'ask' ? 'file' : 'ask')}
          >
            {mode === 'ask' ? 'Find text instead' : 'Ask a question instead'}
          </button>
        </div>

        {mode === 'ask' && (answer || asking || error) && (
          <div className="answer">
            {error ? (
              <p style={{ color: 'var(--bad)' }}>{error}</p>
            ) : (
              <p>{answer || 'Reading your notes…'}</p>
            )}
            {citations.length > 0 && (
              <div className="srcs">
                {citations.map((page, index) => (
                  <button key={page.slug} type="button" className="s" onClick={() => openMatch(page)}>
                    <span className="cite">{index + 1}</span>
                    {page.title || page.slug}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}

        {mode === 'file' && (
          <div className="sheet-list">
            {matches.length === 0 ? (
              <div style={{ padding: 24, textAlign: 'center', color: 'var(--text-3)', fontSize: 'var(--t-13)' }}>
                No page name or text matches that.
              </div>
            ) : (
              matches.map((page, index) => (
                <button
                  key={page.slug}
                  type="button"
                  className={cn('sheet-item', index === cursor && 'on')}
                  onMouseEnter={() => setCursor(index)}
                  onClick={() => openMatch(page)}
                >
                  <Icon name="file" />
                  <span>
                    {page.title || page.slug}
                    {excerptFor(page.slug) && (
                      <span className="findline">{excerptFor(page.slug)}</span>
                    )}
                  </span>
                  <span className="pathsub">{page.slug}</span>
                </button>
              ))
            )}
          </div>
        )}

        <div className="sheet-foot">
          <span>
            <span className="kbd">↑↓</span> move
          </span>
          <span>
            <span className="kbd">↵</span> {mode === 'ask' ? 'ask' : 'open'}
          </span>
          <span>
            {mode === 'ask' ? (
              <>
                <span className="kbd">⌘P</span> find text instead — instant, no model
              </>
            ) : (
              <>
                <span className="kbd">⌘K</span> ask a question instead — takes a few seconds
              </>
            )}
          </span>
          <span style={{ marginLeft: 'auto' }}>
            <span className="kbd">esc</span> close
          </span>
        </div>
      </div>
    </div>
  );
}

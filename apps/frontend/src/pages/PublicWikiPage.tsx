import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { getPublicPage, listPublicPages } from '../api';
import { renderMarkdown } from './markdown';
import type { PublicPage, PublicPageSummary } from '../api';

export default function PublicWikiPage() {
  const params = useParams();
  const navigate = useNavigate();
  const slug = params['*'];
  const [pages, setPages] = useState<PublicPageSummary[]>([]);
  const [page, setPage] = useState<PublicPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    listPublicPages()
      .then((rows) => {
        setPages(rows);
        if (!slug && rows.length > 0) {
          navigate(`/public/wiki/${rows[0].slug}`, { replace: true });
        }
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [navigate, slug]);

  useEffect(() => {
    if (!slug) return;
    setLoading(true);
    setError(null);
    getPublicPage(slug)
      .then((p) => setPage(p))
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [slug]);

  const sanitizedHtml = useMemo(() => {
    if (!page?.content) return '';
    return renderMarkdown(page.content, { wikilinks: 'text' });
  }, [page?.content]);

  return (
    <div className="min-h-screen bg-bg text-text-primary">
      <header className="h-12 border-b border-border flex items-center px-4 bg-panel/40">
        <Link to="/public" className="font-semibold tracking-wide">
          Archivum
        </Link>
      </header>
      <div className="grid min-h-[calc(100vh-3rem)] md:grid-cols-[280px_1fr]">
        <aside className="border-r border-border bg-panel/30 p-3 overflow-y-auto">
          <div className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
            Public wiki
          </div>
          <div className="space-y-1">
            {pages.map((p) => (
              <Link
                key={p.slug}
                to={`/public/wiki/${p.slug}`}
                className={`block rounded-md px-2 py-1.5 text-sm hover:bg-surface ${
                  p.slug === slug ? 'bg-surface text-accent' : 'text-text-secondary'
                }`}
              >
                {p.title}
              </Link>
            ))}
          </div>
        </aside>
        <main className="p-6 overflow-y-auto">
          {loading && <div className="text-sm text-muted-foreground">Loading public wiki...</div>}
          {!loading && error && (
            <div className="rounded-lg p-3 border border-red-400/25 bg-red-500/10 text-sm text-red-300">
              {error}
            </div>
          )}
          {!loading && !error && page && (
            <article className="max-w-3xl">
              <h1 className="text-2xl font-bold mb-4">{page.title}</h1>
              <div
                className="md-body"
                dangerouslySetInnerHTML={{ __html: sanitizedHtml }}
              />
            </article>
          )}
        </main>
      </div>
    </div>
  );
}

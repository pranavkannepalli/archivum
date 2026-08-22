import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Hash, Plus, X } from 'lucide-react';
import { useAppDispatch } from '../store';
import {
  acceptSuggestion,
  deletePage,
  getPage,
  listPageSuggestions,
  listPages,
  rejectSuggestion,
  updatePage,
} from '../api';
import type { MemorySuggestion } from '../api';
import type { Page } from '../types';
import Editor, { type EditorHandle } from '../components/Editor/Editor';
import { Button } from '../components/ui/Button';
import { Input } from '../components/ui/Input';
import { Badge } from '../components/ui/Badge';
import { Dialog } from '../components/ui/Dialog';
import { useToast } from '../components/ui/Toast';
import PageActions from '../components/PageActions';
import ShareSheet from '../sheets/ShareSheet';
import { entryTarget } from '../api';
import EntryMemory from '../surfaces/EntryMemory';
import ReindexControl from '../surfaces/ReindexControl';
import { Icon } from '../shell/Icon';
import { useAppState } from '../store';
import { mergeFrontmatterProperties, parseFrontmatter } from './frontmatter';
import { addTag, removeTag } from './wikiMetadata';

export default function WikiPage() {
  const params = useParams();
  const navigate = useNavigate();
  const slug = params['*'];
  const dispatch = useAppDispatch();
  const { pages } = useAppState();
  const { push } = useToast();
  const [page, setPage] = useState<Page | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState('');
  const [tagsDraft, setTagsDraft] = useState<string[]>([]);
  const [tagInput, setTagInput] = useState('');
  const [addingTag, setAddingTag] = useState(false);
  const [contentDraft, setContentDraft] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [suggestions, setSuggestions] = useState<MemorySuggestion[]>([]);
  const editorRef = useRef<EditorHandle | null>(null);
  const metaSaveTimer = useRef<ReturnType<typeof setTimeout>>();

  const parsedTags = useMemo(() => {
    return tagsDraft.map((t) => t.trim()).filter(Boolean);
  }, [tagsDraft]);
  const editorContent = useMemo(() => (page ? parseFrontmatter(page.content).body : ''), [page]);
  const prepareContentForSave = useCallback(
    (content: string, title = titleDraft, tags = parsedTags) =>
      page ? mergeFrontmatterProperties(page.content, content, title, tags) : content,
    [page, parsedTags, titleDraft],
  );

  useEffect(() => {
    if (!slug) return;
    dispatch({ type: 'SET_CURRENT_SLUG', slug });
    setLoading(true);
    setError(null);
    Promise.all([getPage(slug), listPageSuggestions(slug).catch(() => [])])
      .then(([p, pageSuggestions]) => {
        setPage(p);
        setTitleDraft(p.title ?? '');
        setTagsDraft(p.tags ?? []);
        setTagInput('');
        setAddingTag(false);
        setContentDraft(null);
        setSuggestions(pageSuggestions);
        dispatch({ type: 'UPSERT_PAGE', page: p });
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [slug, dispatch]);

  // Close the share sheet when navigating: it is scoped to one entry, and
  // leaving it open would show the previous page's access on the new one.
  useEffect(() => {
    setShareOpen(false);
  }, [slug]);

  if (!slug) return null;
  const slugStr = slug;

  function scheduleMetaSave(next: { title?: string; tags?: string[] }) {
    clearTimeout(metaSaveTimer.current);
    metaSaveTimer.current = setTimeout(async () => {
      dispatch({ type: 'SET_SAVE_STATUS', status: 'saving' });
      try {
        const updated = await updatePage(slugStr, {
          title: next.title ?? titleDraft,
          tags: next.tags ?? parsedTags,
          content: page
            ? prepareContentForSave(
                contentDraft ?? editorRef.current?.getContent() ?? editorContent,
                next.title ?? titleDraft,
                next.tags ?? parsedTags,
              )
            : undefined,
        });
        setPage(updated);
        dispatch({ type: 'UPSERT_PAGE', page: updated });
        dispatch({ type: 'SET_SAVE_STATUS', status: 'saved' });
      } catch (e) {
        dispatch({ type: 'SET_SAVE_STATUS', status: 'error' });
        setError((e as Error).message);
      }
    }, 650);
  }

  function commitTags(nextTags: string[]) {
    setTagsDraft(nextTags);
    scheduleMetaSave({ tags: nextTags });
  }

  function handleAddTag() {
    const nextTags = addTag(parsedTags, tagInput);
    setTagInput('');
    setAddingTag(false);
    if (nextTags !== parsedTags) commitTags(nextTags);
  }

  function handleRemoveTag(tag: string) {
    commitTags(removeTag(parsedTags, tag));
  }

  async function handleSaveNow() {
    dispatch({ type: 'SET_SAVE_STATUS', status: 'saving' });
    try {
      const editorBody = contentDraft ?? editorRef.current?.getContent() ?? editorContent;
      const content = prepareContentForSave(editorBody);
      const updated = await updatePage(slugStr, { title: titleDraft, tags: parsedTags, content });
      setPage(updated);
      setContentDraft(null);
      dispatch({ type: 'UPSERT_PAGE', page: updated });
      dispatch({ type: 'SET_SAVE_STATUS', status: 'saved' });
      push({ kind: 'success', title: 'Saved', description: updated.title });
    } catch (e) {
      dispatch({ type: 'SET_SAVE_STATUS', status: 'error' });
      setError((e as Error).message);
      push({ kind: 'error', title: 'Save failed', description: (e as Error).message });
    }
  }

  async function handleDelete() {
    if (!page) return;
    dispatch({ type: 'SET_SAVE_STATUS', status: 'saving' });
    try {
      await deletePage(page.slug);
      dispatch({ type: 'DELETE_PAGE', slug: page.slug });
      push({ kind: 'success', title: 'Deleted page', description: page.title });
      setDeleteOpen(false);
      // Pick a next page if available; otherwise go to ingest
      const pages = await listPages().catch(() => null);
      if (pages && pages.length > 0) {
        dispatch({ type: 'SET_PAGES', pages });
        window.location.assign(`/wiki/${pages[0].slug}`);
      } else {
        window.location.assign('/ingest');
      }
    } catch (e) {
      dispatch({ type: 'SET_SAVE_STATUS', status: 'error' });
      setError((e as Error).message);
      push({ kind: 'error', title: 'Delete failed', description: (e as Error).message });
    }
  }

  // Sharing opens the sheet rather than minting a link on the spot: a link is
  // one kind of grant, not the whole of sharing, and choosing it should be a
  // deliberate act rather than the side effect of pressing Share.
  function handleShare() {
    setShareOpen(true);
  }


  async function handleAcceptSuggestion(suggestion: MemorySuggestion) {
    try {
      const updated = await acceptSuggestion(suggestion.id);
      setSuggestions((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      push({ kind: 'success', title: 'Suggestion accepted' });
    } catch (e) {
      push({ kind: 'error', title: 'Accept failed', description: (e as Error).message });
    }
  }

  async function handleRejectSuggestion(suggestion: MemorySuggestion) {
    try {
      const updated = await rejectSuggestion(suggestion.id);
      setSuggestions((current) =>
        current.map((item) => (item.id === updated.id ? updated : item)),
      );
      push({ kind: 'success', title: 'Suggestion rejected' });
    } catch (e) {
      push({ kind: 'error', title: 'Reject failed', description: (e as Error).message });
    }
  }

  const segments = slugStr.split('/');
  const folder = segments.slice(0, -1);
  // Siblings are the other entries in the same folder, in the order the vault
  // lists them, so ⌥↑/↓ walks the folder rather than the whole vault.
  const siblings = pages
    .filter((candidate) => {
      const parts = candidate.slug.split('/');
      return parts.slice(0, -1).join('/') === folder.join('/');
    })
    .sort((a, b) => a.slug.localeCompare(b.slug));
  const position = siblings.findIndex((candidate) => candidate.slug === slugStr);

  function goToSibling(delta: number) {
    if (position < 0) return;
    const next = siblings[position + delta];
    if (next) navigate(`/wiki/${next.slug}`);
  }

  return (
    <div className="surface on entry-doc">
      <div className="entry-top">
        <button
          type="button"
          className="btn btn-icon"
          title="Back to the stream"
          onClick={() => navigate('/')}
        >
          <Icon name="chevronRight" className="rotate-180" />
        </button>
        <div className="crumbs">
          {folder.map((segment, index) => (
            <span key={segment + index} style={{ display: 'contents' }}>
              <button type="button" onClick={() => navigate('/entries')}>
                {segment}
              </button>
              <span className="sep">/</span>
            </span>
          ))}
          <span className="now">{titleDraft || segments[segments.length - 1]}</span>
        </div>
        {siblings.length > 1 && position >= 0 && (
          <div className="sibs">
            <button
              type="button"
              className="btn btn-icon btn-sm"
              title="Previous in this folder"
              disabled={position === 0}
              onClick={() => goToSibling(-1)}
            >
              <Icon name="chevronDown" className="rotate-180" />
            </button>
            <button
              type="button"
              className="btn btn-icon btn-sm"
              title="Next in this folder"
              disabled={position === siblings.length - 1}
              onClick={() => goToSibling(1)}
            >
              <Icon name="chevronDown" />
            </button>
            <span className="pos">
              {position + 1} of {siblings.length} in {folder[folder.length - 1] ?? 'the vault'}
            </span>
          </div>
        )}
        <ReindexControl slug={slugStr} />
      </div>

      <div className="wiki-document-gutter group/document shrink-0 pb-2 pt-7">
        <div className="flex w-full items-start gap-4">
          <div className="min-w-0 flex-1">
            <Input
              value={titleDraft}
              onChange={(e) => {
                setTitleDraft(e.target.value);
                scheduleMetaSave({ title: e.target.value });
              }}
              placeholder={loading ? 'Loading…' : 'Untitled'}
              className="h-auto min-h-14 border-0 bg-transparent px-0 py-0 text-[38px] font-bold leading-tight tracking-normal text-foreground shadow-none placeholder:text-muted-foreground/45 focus-visible:ring-0 md:text-[46px]"
              aria-label="Page title"
            />
            <div className="mt-2 flex min-h-8 flex-wrap items-center gap-1.5">
              {page?.authored_by === 'agent' && <Badge variant="info">AI-authored</Badge>}
              {parsedTags.map((tag) => (
                <span
                  key={tag}
                  className="group inline-flex h-7 items-center gap-1 rounded-[5px] px-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-foreground/[0.05] hover:text-foreground"
                >
                  <Hash className="h-3 w-3" />
                  {tag}
                  <button
                    type="button"
                    onClick={() => handleRemoveTag(tag)}
                    className="ml-0.5 rounded-[4px] p-0.5 opacity-0 transition-opacity hover:bg-foreground/10 hover:text-foreground group-hover:opacity-100"
                    aria-label={`Remove ${tag} tag`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
              {addingTag ? (
                <input
                  autoFocus
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onBlur={handleAddTag}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      handleAddTag();
                    }
                    if (e.key === 'Escape') {
                      setTagInput('');
                      setAddingTag(false);
                    }
                  }}
                  placeholder="Tag"
                  className="h-7 w-28 rounded-[5px] border border-transparent bg-transparent px-1.5 text-xs text-foreground outline-none placeholder:text-muted-foreground focus:border-foreground/15"
                  aria-label="New tag"
                />
              ) : (
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  onClick={() => setAddingTag(true)}
                  className="h-7 gap-1 rounded-[5px] px-1.5 text-xs text-muted-foreground hover:bg-foreground/[0.05] hover:text-foreground"
                >
                  <Plus className="h-3.5 w-3.5" />
                  Tag
                </Button>
              )}
            </div>
          </div>
          <div className="sticky top-3 flex shrink-0 items-center opacity-80 transition-opacity group-hover/document:opacity-100">
            <PageActions
              slug={slugStr}
              disabled={!page}
              shareLoading={false}
              onSave={handleSaveNow}
              onShare={handleShare}
              onDelete={() => setDeleteOpen(true)}
            />
          </div>
        </div>
        {error && <div className="mt-3 text-xs text-destructive">{error}</div>}
      </div>

      <div className="flex min-h-0 flex-1 overflow-hidden bg-transparent">
        {loading && !page && (
          <div className="space-y-2 p-6">
            <div className="skeleton h-4 w-full" />
            <div className="skeleton h-4 w-5/6" />
            <div className="skeleton h-4 w-4/5" />
          </div>
        )}

        {page && (
          <Editor
            ref={editorRef}
            slug={page.slug}
            initialContent={editorContent}
            onSave={(s) => dispatch({ type: 'SET_SAVE_STATUS', status: s })}
            onChange={(c) => setContentDraft(c)}
            prepareContentForSave={prepareContentForSave}
            pendingSuggestions={suggestions}
            onAcceptSuggestion={handleAcceptSuggestion}
            onRejectSuggestion={handleRejectSuggestion}
          />
        )}
      </div>

      <div className="col" style={{ paddingBottom: '20vh' }}>
        <EntryMemory slug={slugStr} />
      </div>

      <Dialog
        open={deleteOpen}
        onOpenChange={setDeleteOpen}
        title="Delete this page?"
        description="This will remove the markdown file and delete it from the index."
        footer={
          <div className="flex items-center justify-end gap-2">
            <Button variant="ghost" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button variant="danger" onClick={handleDelete}>
              Delete
            </Button>
          </div>
        }
      />

      <ShareSheet
        open={shareOpen}
        target={entryTarget(slugStr)}
        resourceTitle={titleDraft || slugStr}
        onClose={() => setShareOpen(false)}
      />
    </div>
  );
}

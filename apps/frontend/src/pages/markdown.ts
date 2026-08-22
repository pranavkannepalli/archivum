/**
 * One markdown renderer for the whole app.
 *
 * There were three, all hand-rolled from chained `String.replace` calls, and
 * all subtly different. None of them handled tables, nested lists, task lists
 * or setext headings; two emitted bare `<li>` with no list around it; one
 * inlined a hard-coded palette so shared pages ignored the reader's theme. A
 * document that renders correctly in the editor and wrongly in a share link is
 * the kind of thing you only find out about after sending the link.
 *
 * So: a real CommonMark + GFM parser, one sanitiser, one set of semantic class
 * names. Output is sanitised here rather than at the call sites, because "the
 * caller sanitises" is a rule that holds right up until someone adds a caller.
 */

import DOMPurify from 'dompurify';
import { marked } from 'marked';

export type WikilinkMode = 'link' | 'text';

export type RenderOptions = {
  /**
   * `link` points wikilinks into the vault. `text` renders them as plain spans,
   * which is what a share recipient needs — they cannot open `/wiki/...` and a
   * link to a login wall is worse than no link.
   */
  wikilinks?: WikilinkMode;
};

const WIKILINK_RE = /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g;

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/**
 * Wikilinks are not markdown, so they are substituted before parsing — but only
 * outside code, where `[[foo]]` is text the author meant literally.
 */
function replaceWikilinks(source: string, mode: WikilinkMode): string {
  const segments = source.split(/(```[\s\S]*?```|`[^`\n]*`)/g);
  return segments
    .map((segment, index) => {
      if (index % 2 === 1) return segment; // the captured code span itself
      return segment.replace(WIKILINK_RE, (_match, target: string, label?: string) => {
        const slug = target.trim();
        const text = (label ?? slug).trim();
        if (mode === 'text') {
          return `<span class="wikilink-plain">${escapeHtml(text)}</span>`;
        }
        const href = `/wiki/${slug.split('/').map(encodeURIComponent).join('/')}`;
        return `<a class="wikilink" href="${escapeHtml(href)}">${escapeHtml(text)}</a>`;
      });
    })
    .join('');
}

marked.setOptions({ gfm: true, breaks: true });

/**
 * Send links to other sites to a new tab, and never hand them the opener.
 * `rel="noopener"` matters most on shared pages, where the author of the
 * document is not necessarily someone the reader trusts.
 */
function hardenExternalLinks(root: DocumentFragment | HTMLElement): void {
  for (const anchor of Array.from(root.querySelectorAll('a[href]'))) {
    const href = anchor.getAttribute('href') ?? '';
    if (!/^https?:\/\//i.test(href)) continue;
    anchor.setAttribute('target', '_blank');
    anchor.setAttribute('rel', 'noopener noreferrer');
  }
}

export function renderMarkdown(source: string, options: RenderOptions = {}): string {
  const withLinks = replaceWikilinks(source ?? '', options.wikilinks ?? 'link');
  const html = marked.parse(withLinks, { async: false }) as string;
  const clean = DOMPurify.sanitize(html, {
    RETURN_DOM_FRAGMENT: true,
    // Checkbox inputs come from GFM task lists; marked already renders them
    // disabled, and the sanitiser drops any attribute that would make one live.
    ADD_ATTR: ['checked', 'disabled', 'type', 'target', 'rel'],
    FORBID_TAGS: ['style', 'form', 'button'],
  });
  // Hardened after sanitising: adding the attributes first would just give
  // DOMPurify something else to consider stripping.
  hardenExternalLinks(clean);
  const host = document.createElement('div');
  host.appendChild(clean);
  return host.innerHTML;
}

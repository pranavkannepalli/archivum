# Daily Use

What the vault is like to live in, as opposed to what it can store.

## The stream

Home is a reverse-chronological feed of everything that happened, with the
capture box at the top. Seven kinds appear:

| Kind | From |
|---|---|
| `page_created`, `page_edited` | Writes to the vault, yours or an agent's |
| `suggestion` | Memory waiting on your review, decided in place |
| `ingest` | Files and URLs you brought in |
| `memory` | Assets distillation produced |
| `session` | A captured agent session, labelled by what kind of work it was |
| `fix` | A remembered repair: what broke, what caused it, whether it was checked |

Sessions and fixes were the gap. Capture runs automatically, and capture you
cannot see is hard to tell apart from no capture at all.

## Tasks

A task is a checkbox line in a page — `- [ ] Ship it` — not a row in a table.
There used to be a `life_tasks` table; it was deleted because the tool wrote to
a store no screen read, so there were two models of the same noun and only one
was visible.

Open tasks ride along with the first page of the stream as a standing list above
the timeline. Ticking one rewrites the line in the file and reindexes, which is
what keeps a task the same object whether you ticked it in Archivum or in your
own editor. Generated pages under `code/`, `memory/` and `skills/` are skipped:
a todo list is something you keep, not something the code graph writes for you.

## Finding things

Three different questions, which used to be hard to tell apart:

| | What it does | Cost |
|---|---|---|
| **Find text** (`⌘P`) | Literal match on page names *and page bodies*, straight off the SQLite FTS index. Shows the line it matched. | Instant, nothing leaves the machine |
| **Search** (the box on Everything) | Both of the above, plus semantic: pages that are *about* what you typed | ~100ms |
| **Ask** (`⌘K`) | Sends a question to a model, streams back an answer citing the pages it read | Seconds, uses your CLI subscription |

The distinction matters because they fail differently. Semantic search declines
a weak match rather than returning a page of near-misses — right for "things
about retrieval", wrong for "the file where I wrote that string", which is
exactly when it would return nothing. So literal results always lead, and the
box runs both channels.

Find used to match titles only, and only against the pages already loaded in
the browser — so text inside a file was unreachable from anywhere in the app,
even though the FTS index over title, content and tags has been there from the
beginning, kept current by triggers. It was only ever read as one channel inside
the ranker, where the relevance floor could discard it.

Both `Find text` and `Ask a question` are buttons in the sidebar. Find was
previously reachable only by knowing `⌘P`, which meant that in practice it was
not in the product.

## Where things go

A new vault starts with folders — `inbox`, `daily`, `notes`, `projects`, `areas`,
`people`, `decisions`, `reading`, `reference`, `sources`, `archive` — because an
empty vault turns every capture into a filing decision, and the composer can only
guess among folders that exist. On an empty vault it guessed "the root" every
time, so everything piled up there.

They are a starting point, not a policy. `vault_scaffold.ensure_default_folders`
runs once at startup and does nothing at all if the vault already has folders of
its own: arriving with your own structure and being handed a second one on top is
worse than being handed none.

Listing folders used to create the defaults as a side effect, which meant a
folder you deleted came back the next time anything read the list — you could not
actually remove one. Seeding is now an explicit startup step, and `list_folders`
only reads.

`code/`, `memory/` and `skills/` are deliberately not in the list. The system
writes those; they are output, not places you put things.

## Today

`T`, or the Today button, opens the daily note and creates it on first ask. The
endpoint existed from the beginning and nothing called it, so there was no
"today" — the one page a daily-notes habit needs to be one keystroke away.

## Keyboard

| Key | Does |
|---|---|
| `C` | Capture a thought from anywhere |
| `T` | Today's note |
| `S` / `E` / `V` | Stream, Everything, Visualized |
| `⌘K` | Ask |
| `⌘P` | Jump to a page |

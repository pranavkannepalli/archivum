# Code memory that remembers the work, not just the artifact

## Why

Archivum can read a repository into a graph, but what it remembers is the code
*as it stands*. It has nothing to say about how it got that way — which bug bit,
what was tried, what fixed it, what was decided and why.

Three findings set the scope:

1. **Retrieval hands back pointers, not context.** `retrieve_code_context`
   returns at most `max_nodes` records (default 10, cap 50), each a label, a kind
   and a `file:line` citation. No signature, no docstring, no source. The scope
   budget system does not apply, because budgets live in `memory_scopes` and no
   row exists for a `repo:` scope.
2. **Nothing is captured automatically.** `capture_conversation` is a tool an
   agent must choose to call; the frontend binding has no caller; the Claude Code
   transcript importer has no runner. The distillation pipeline, the atoms and
   `decided_in` therefore sit idle on a real install.
3. **The source data for "why" does not exist.** A commit record carries only its
   SHA — no message, author, date, touched files or diff.

## Decisions

- **Capture is automatic.** A transcript watcher ingests sessions without being
  asked. Everything lands review-gated, as memory already does, so recall costs
  nothing but a review queue.
- **Diffs are evidence, not pages.** A change is stored as a content-addressed
  L0 blob and cited, never pasted into markdown.
- **Order is A → B → C, with D alongside B.** C built on today's SHA-only
  commits would be guesswork dressed as memory.

## A. Make the code graph carry enough to be context

1. Signature and docstring on `symbol`/`type` records, from the existing
   tree-sitter pass.
2. Commit records carrying message, author, date and touched files.
3. `changed_in` edges from commit to symbol, by intersecting diff line ranges
   with symbol spans.
4. Bounded source excerpts in retrieval, opt-out for name-only mapping.
5. A `memory_scopes` row per repository, so budgets cover code.

## B. Capture the work, not just the artifact

6. Automatic session capture: a watcher over agent transcripts, through the
   existing importer and redaction.
7. A `record_work` tool for when an agent knows it did something worth keeping.
8. Session classification: bugfix, feature, refactor, investigation.
9. Sessions linked to commits by SHA and to symbols by touched file.

## C. Bug-fix memory as a first-class kind

10. A `fix` asset: symptom, diagnosis, change, verification.
11. `fixes` and `verified_by` edges onto the symbols involved.
12. Symptom retrieval — "have I hit this error before?"
13. Decisions anchored on a commit or session rather than a name match.

## D. Make agents use it

14. A skill encoding the loop: load before changing, check before debugging,
    record after finishing.
15. A default agent profile with bindings at setup.
16. Tool descriptions written as decision guidance.
17. Every tool documented.

## E. Close the loop in the interface

18. Fixes and decisions on the repository index and cluster pages.
19. A per-file view: its symbols, its fixes, its decisions, its recent changes.

---
name: archivum-memory
description: Use when working in a repository Archivum has indexed - before debugging an error, before changing unfamiliar code, and after finishing a piece of work. Archivum remembers what broke before, what fixed it, and why the code is the way it is.
---

# Archivum memory

Archivum is a second brain that remembers your code and the work you did on it.
It already holds the graph of this repository, the sessions that changed it, and
the fixes that settled past bugs. This skill is about *consulting* that before
you act, so you are not solving something you already solved.

## The rule

**Ask before you dig. Record what you learned.**

Two of these are cheap and one is free — the cost of skipping them is redoing
work you already did months ago and forgot.

## When you hit an error

Before reading any code, before forming a theory:

```
recall_fix(symptom="<paste the error>")
```

If it comes back with something, you have the symptom, the diagnosis, the files
that changed, and how it was verified — cited back to the session it came from.
Read that before you start. It is often the whole answer.

If it comes back empty it says so. That is also useful: this is new trouble, and
worth recording once you solve it.

## When you touch unfamiliar code

```
retrieve_code_context(query="<what you are looking for>", repo="<repo name>")
```

Returns the symbols that matter with their signatures, summaries and
`file:line` citations. Ask for `include_source=true` when you need the bodies
rather than the map — leave it off when you are orienting, because the source of
everything is a lot of context to spend on a question you have not asked yet.

Follow the graph from there: `graph_neighbors` for what a symbol connects to,
`graph_shortest_path` for how two things relate.

## When you finish

Sessions are captured automatically, so you do not have to do anything for the
work to be remembered. But automatic capture infers; you *know*. When a piece of
work mattered — a non-obvious bug, a decision with a reason, a gotcha worth
warning the next person about — say so plainly:

```
record_work(
  request="what was asked",
  outcome="what you found and did, including the cause",
  changed_paths=["..."],
  verified_by="the command that proves it",
)
```

Be specific about the *cause*. "Fixed the test" is worth nothing in six months.
"The fixture shared a connection across event loops, so the second test saw a
closed socket" is worth a great deal.

## What not to do

- **Do not paste secrets into `record_work`.** It is memory, and memory is read
  back. Transcripts are redacted on capture; what you type here is not.
- **Do not record work you did not do.** A fix that was not verified should say
  so rather than claiming a test that never ran. Archivum tracks whether a fix
  was verified and weights it accordingly; a false claim poisons that.
- **Do not skip `recall_fix` because the error looks simple.** Simple-looking
  errors are exactly the ones that recur.

## If Archivum is not reachable

Say so and carry on. Memory is an advantage, not a dependency — a vault you
cannot reach should slow you down, not stop you.

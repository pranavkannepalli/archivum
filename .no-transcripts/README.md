# Placeholder

Compose mounts `TRANSCRIPT_HOST_DIR` into the containers so session capture can
read agent transcripts. This directory is the default when that variable is
unset, so the mount always resolves and the stack starts on a fresh clone.

Point `TRANSCRIPT_HOST_DIR` at your real transcript directory — for Claude Code
that is `~/.claude/projects` — and set `TRANSCRIPT_DIRS=/data/transcripts`.
Leaving both unset simply means nothing is captured.

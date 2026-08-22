from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from archivum.archgraph.registry import CODE_SUFFIXES
from archivum.archgraph.extractors.base import _make_id
from archivum.archgraph.mapper import CandidateArtifact, CandidateRelationship, Provenance

_PRUNE = {".git", "node_modules", ".venv", "__pycache__"}


# A commit message can be a novel; keep enough to recognise the change.
_MAX_COMMIT_MESSAGE = 2000
_MAX_COMMIT_FILES = 200

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class RepoSnapshot:
    repo_id: str
    commit_sha: str
    root: Path
    remote_url: str | None
    # What the commit actually was. Carrying only the SHA meant the question
    # every developer asks — what changed, and why — had no data behind it.
    message: str = ""
    author: str = ""
    committed_at: str = ""
    files: tuple[str, ...] = ()


def _git(root: Path, *args: str) -> str | None:
    """Run a read-only git command, or None if git or the repo is unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def commit_details(root: Path, sha: str) -> tuple[str, str, str, tuple[str, ...]]:
    """(message, author, committed_at, files) for a commit, empty when unknown."""
    if not sha or sha == "working-tree":
        return "", "", "", ()
    shown = _git(root, "show", "-s", "--format=%an%n%aI%n%B", sha)
    if shown is None:
        return "", "", "", ()
    author, _, rest = shown.partition("\n")
    committed_at, _, message = rest.partition("\n")
    listed = _git(root, "show", "--name-only", "--format=", sha) or ""
    files = tuple(
        line.strip() for line in listed.splitlines() if line.strip()
    )[:_MAX_COMMIT_FILES]
    return (
        message.strip()[:_MAX_COMMIT_MESSAGE],
        author.strip(),
        committed_at.strip(),
        files,
    )


def changed_line_ranges(root: Path, sha: str) -> dict[str, list[tuple[int, int]]]:
    """Which lines each file gained in a commit, as (start, end) per file.

    Line ranges rather than filenames are what let a commit be attributed to the
    symbols it actually changed. Attributing it to every symbol in a touched
    file would tell you a module moved, not which function did.
    """
    if not sha or sha == "working-tree":
        return {}
    diff = _git(root, "show", "-U0", "--format=", sha)
    if not diff:
        return {}

    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        if not line.startswith("@@") or current is None:
            continue
        match = _HUNK_RE.match(line)
        if match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2) or 1)
        if count == 0:
            # A pure deletion: attribute it to the line it was removed after.
            ranges.setdefault(current, []).append((start, start))
            continue
        ranges.setdefault(current, []).append((start, start + count - 1))
    return ranges


def snapshot_repo(root: Path) -> RepoSnapshot:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            commit_sha = result.stdout.strip()
        else:
            commit_sha = "working-tree"
    except (FileNotFoundError, subprocess.CalledProcessError):
        commit_sha = "working-tree"

    remote_url: str | None = None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            remote_url = result.stdout.strip() or None
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass

    repo_id = _make_id(Path(remote_url).stem if remote_url else root.resolve().name)
    message, author, committed_at, files = commit_details(root, commit_sha)
    return RepoSnapshot(
        repo_id=repo_id,
        commit_sha=commit_sha,
        root=root,
        remote_url=remote_url,
        message=message,
        author=author,
        committed_at=committed_at,
        files=files,
    )


def collect_files(root: Path) -> list[Path]:
    results = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _PRUNE for part in p.parts):
            continue
        if p.suffix in CODE_SUFFIXES:
            results.append(p)
    return sorted(results)


def repo_artifacts(snap: RepoSnapshot, *, scope: str) -> list[object]:
    prov = Provenance(
        chunk_id=f"repo:{snap.repo_id}",
        span="L0",
        extraction_method="EXTRACTED",
    )
    repo_art = CandidateArtifact(
        id=snap.repo_id,
        kind="repo",
        name=snap.repo_id,
        scope=scope,
        confidence=1.0,
        extraction_method="EXTRACTED",
        provenance=[prov],
    )
    commit_id = _make_id(snap.repo_id, snap.commit_sha)
    commit_art = CandidateArtifact(
        id=commit_id,
        kind="commit",
        name=snap.commit_sha,
        scope=scope,
        confidence=1.0,
        extraction_method="EXTRACTED",
        provenance=[prov],
        properties={
            "message": snap.message,
            "author": snap.author,
            "committed_at": snap.committed_at,
            "files": list(snap.files),
        },
    )
    rel = CandidateRelationship(
        id=_make_id(commit_id, snap.repo_id, "in_commit"),
        src_id=commit_id,
        dst_id=snap.repo_id,
        rel_type="in_commit",
        scope=scope,
        confidence=1.0,
        extraction_method="EXTRACTED",
        provenance=[prov],
    )
    return [repo_art, commit_art, rel]

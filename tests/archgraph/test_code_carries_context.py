"""A code record has to say enough to be useful without opening the file.

Retrieval handed agents a label, a kind and a `file:line` citation. That is a
pointer, not context: an agent still had to go and read everything before it
could act. And a commit carried nothing but its SHA, so the question every
developer actually asks — what changed, and why — had no data behind it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from archivum.archgraph.extract import extract_file
from archivum.archgraph.repo import changed_line_ranges, repo_artifacts, snapshot_repo

GEO = '''"""Geo helpers."""


def haversine(lat: float, lon: float) -> float:
    """Distance on a sphere, in degrees."""
    return normalise(lat) + normalise(lon)


class Bearing:
    """A compass bearing."""

    def to_degrees(self) -> float:
        return 0.0


def normalise(value):
    return value % 360
'''


@pytest.fixture(autouse=True)
def _needs_git():
    if shutil.which("git") is None:
        pytest.skip("git not available")


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=dev@example.com", "-c", "user.name=Dev",
            "commit", "-q", "-m", message,
        ],
        check=True,
    )
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "atlas"
    root.mkdir()
    (root / "geo.py").write_text(GEO, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _commit(root, "Add geo helpers")
    return root


# ── A1: symbols say what they are ─────────────────────────────────────────


def test_a_function_records_its_signature(repo):
    extraction = extract_file(repo / "geo.py", root=repo, scope="repo:t:atlas")

    haversine = next(n for n in extraction.nodes if n.label == "haversine")
    assert "lat" in haversine.properties["signature"]
    assert "float" in haversine.properties["signature"]


def test_a_function_records_its_docstring(repo):
    extraction = extract_file(repo / "geo.py", root=repo, scope="repo:t:atlas")

    haversine = next(n for n in extraction.nodes if n.label == "haversine")
    assert haversine.properties["summary"] == "Distance on a sphere, in degrees."


def test_a_type_records_its_docstring(repo):
    extraction = extract_file(repo / "geo.py", root=repo, scope="repo:t:atlas")

    bearing = next(n for n in extraction.nodes if n.label == "Bearing")
    assert bearing.properties["summary"] == "A compass bearing."


def test_a_symbol_without_a_docstring_says_nothing_rather_than_guessing(repo):
    extraction = extract_file(repo / "geo.py", root=repo, scope="repo:t:atlas")

    normalise = next(n for n in extraction.nodes if n.label == "normalise")
    assert normalise.properties.get("summary", "") == ""


# ── A2: a commit says what it was ─────────────────────────────────────────


def test_a_commit_records_its_message_author_and_date(repo):
    snapshot = snapshot_repo(repo)
    artifacts = repo_artifacts(snapshot, scope="repo:t:atlas")

    commit = next(a for a in artifacts if getattr(a, "kind", "") == "commit")
    assert commit.properties["message"] == "Add geo helpers"
    assert commit.properties["author"] == "Dev"
    assert commit.properties["committed_at"]


def test_a_commit_records_the_files_it_touched(repo):
    snapshot = snapshot_repo(repo)
    artifacts = repo_artifacts(snapshot, scope="repo:t:atlas")

    commit = next(a for a in artifacts if getattr(a, "kind", "") == "commit")
    assert commit.properties["files"] == ["geo.py"]


def test_a_repository_with_no_commits_still_snapshots(tmp_path):
    """A working tree is a legitimate thing to index."""
    root = tmp_path / "fresh"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    snapshot = snapshot_repo(root)
    artifacts = repo_artifacts(snapshot, scope="repo:t:fresh")

    commit = next(a for a in artifacts if getattr(a, "kind", "") == "commit")
    assert commit.properties.get("message", "") == ""


# ── A3: which lines a commit changed ──────────────────────────────────────


def test_changed_line_ranges_names_the_lines_a_commit_touched(repo):
    body = (repo / "geo.py").read_text(encoding="utf-8")
    (repo / "geo.py").write_text(
        body.replace("return value % 360", "return round(value % 360, 6)"),
        encoding="utf-8",
    )
    sha = _commit(repo, "Round the normalised bearing")

    ranges = changed_line_ranges(repo, sha)

    assert "geo.py" in ranges
    touched = ranges["geo.py"]
    assert any(start <= 17 <= end for start, end in touched), touched


def test_changed_line_ranges_is_empty_for_an_unknown_commit(repo):
    assert changed_line_ranges(repo, "0" * 40) == {}

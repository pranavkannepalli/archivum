"""The vendored skill the API serves must match the one in the repo root."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "skills" / "archivum-memory" / "SKILL.md"
VENDORED = (
    REPO_ROOT
    / "apps" / "backend" / "archivum" / "agent_skills" / "archivum-memory" / "SKILL.md"
)


def test_the_vendored_skill_matches_the_repo_root_copy():
    """Two copies exist because the Docker build context cannot see the root one.

    Editing the root copy alone would ship a stale skill to every machine that
    runs `archivum connect`, and nothing else would notice.
    """
    assert VENDORED.read_text() == SOURCE.read_text(), (
        "skills/archivum-memory/SKILL.md changed without updating the vendored "
        "copy under apps/backend/archivum/agent_skills/. Copy it across."
    )

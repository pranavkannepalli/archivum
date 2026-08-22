"""What search returns, and what it refuses to return.

Measured against the live vault before changing anything: the endpoint answered
in ~105ms, so it was never slow. What it returned was the problem. Every
excerpt was the page's YAML frontmatter, so all results read identically, and a
nonsense query came back with sixteen confident-looking hits.
"""

import pytest

from archivum.db.qdrant_client import _chunk_text, embeddable_text
from archivum.retrieval.hybrid import readable_excerpt

PAGE = (
    "---\ntitle: Local-first Architecture\ntags: [architecture, perceo]\n"
    "entities: [Perceo, Docker]\n---\n\n"
    "# Local-first Architecture\n\n"
    "Perceo's core design principle is that the machine holding your code must "
    "be one you control.\n"
)


def test_frontmatter_is_not_what_gets_embedded():
    """Every page opens with similar YAML, so embedding it makes pages alike.

    That is why an unrelated query still scored ~0.50 against everything: the
    strongest signal in chunk 0 was structure every page shares.
    """
    text = embeddable_text("Local-first Architecture", PAGE)
    assert "tags:" not in text
    assert "entities:" not in text
    assert "core design principle" in text
    # The title is worth embedding; it is what people actually search for.
    assert text.startswith("Local-first Architecture")


def test_the_first_chunk_is_prose_not_yaml():
    first = _chunk_text(embeddable_text("Local-first Architecture", PAGE))[0]
    assert not first.lstrip().startswith("---")


def test_an_excerpt_reads_as_a_sentence():
    assert readable_excerpt(PAGE).startswith("Perceo's core design principle")
    assert "---" not in readable_excerpt(PAGE)
    assert "#" not in readable_excerpt(PAGE)


def test_an_excerpt_falls_back_rather_than_coming_back_empty():
    assert readable_excerpt("---\ntitle: Only frontmatter\n---\n") == ""


@pytest.mark.parametrize("value", ["", None])
def test_an_empty_page_does_not_crash_the_excerpt(value):
    assert readable_excerpt(value) == ""


def test_weak_vector_matches_are_dropped_before_fusion():
    """"xyzzy" returned sixteen hits scored 0.50-0.52 on the live vault.

    Dense search always hands back its top-k; without a floor, "nothing here
    matches" is indistinguishable from "here are the sixteen best pages", and
    the second is what got rendered.
    """
    from archivum.retrieval.hybrid import above_floor

    rows = [
        {"slug": "a", "score": 0.74},
        {"slug": "b", "score": 0.61},
        {"slug": "c", "score": 0.52},
        {"slug": "d", "score": 0.50},
    ]
    assert [row["slug"] for row in above_floor(rows, floor=0.6)] == ["a", "b"]


def test_the_floor_never_swallows_an_exact_keyword_match():
    """Keyword hits are evidence in their own right and do not go through it."""
    from archivum.retrieval.hybrid import above_floor

    assert above_floor([], floor=0.6) == []
    # A row with no score is a keyword row shape; it must survive.
    assert above_floor([{"slug": "a"}], floor=0.6) == [{"slug": "a"}]

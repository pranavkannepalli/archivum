"""Plain text search: find a file, or a line inside one.

Distinct from Ask, which sends a question to a model, and from the hybrid
search, which is semantic and deliberately refuses weak matches. Find is
literal, instant, and never says "nothing" for text that is on the page — it is
the thing you reach for when you already know what you are looking for.

FTS5 has its own query syntax, so raw typing has to be translated. `deploy?`,
`local-first` and `C++` are ordinary things to type and all three are syntax
errors to FTS.
"""

import pytest

import archivum.db.sqlite as sqlite_mod
from archivum.config import Settings
from archivum.search.find import find_pages, fts_query


@pytest.fixture
async def vault(tmp_path):
    settings = Settings(db_path=tmp_path / "archivum.db", blob_dir=tmp_path / "blobs")
    await sqlite_mod.init_db(settings)
    await sqlite_mod.upsert_page(
        "projects/perceo/archivum",
        "Archivum",
        "Archivum is a local-first knowledge vault.\nDeploys go through compose.",
        ["memory"],
        "user",
        "default",
    )
    await sqlite_mod.upsert_page(
        "daily/2026-08-22", "Friday", "Fixed the retry backoff in the worker.", [], "user", "default"
    )
    return settings


class TestQueryTranslation:
    def test_punctuation_that_would_be_a_syntax_error_is_quoted(self):
        # Every one of these is something a person types and FTS rejects.
        for raw in ["deploy?", "local-first", "C++", 'say "hi"', "a AND"]:
            assert fts_query(raw), f"{raw!r} produced no usable query"

    def test_input_with_no_words_searches_for_nothing(self):
        """"*" is punctuation, not a term; searching for it would match all."""
        assert fts_query("*") == ""
        assert fts_query("!?") == ""

    def test_the_last_word_matches_as_a_prefix(self):
        """Typing is incremental; "arch" has to find "archivum" before you finish."""
        assert fts_query("arch").endswith('*')

    def test_all_words_must_appear(self):
        assert " AND " in fts_query("retry backoff")

    def test_empty_input_has_no_query(self):
        assert fts_query("   ") == ""


class TestFinding:
    async def test_finds_a_page_by_words_in_its_body(self, vault):
        hits = await find_pages("retry backoff", wiki_id="default")
        assert [hit["slug"] for hit in hits] == ["daily/2026-08-22"]

    async def test_finds_a_page_by_name_while_you_are_still_typing(self, vault):
        hits = await find_pages("archiv", wiki_id="default")
        assert any(hit["slug"] == "projects/perceo/archivum" for hit in hits)

    async def test_returns_the_line_that_matched(self, vault):
        hits = await find_pages("compose", wiki_id="default")
        assert "compose" in hits[0]["excerpt"].lower()
        # The excerpt is for reading, so the frontmatter is not it.
        assert not hits[0]["excerpt"].startswith("---")

    async def test_punctuation_does_not_raise(self, vault):
        assert await find_pages("local-first", wiki_id="default")

    async def test_nothing_matching_is_an_empty_list_not_an_error(self, vault):
        assert await find_pages("zzzznotpresent", wiki_id="default") == []

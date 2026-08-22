import pytest

from archivum.sharing.urn import (
    ResourceUrn,
    UrnError,
    ancestors,
    build,
    parse,
)


# ── Parsing ───────────────────────────────────────────────────────────────────


def test_parse_splits_type_wiki_and_local_id():
    urn = parse("entry:default:people/alice")
    assert urn == ResourceUrn(kind="entry", wiki_id="default", local_id="people/alice")


def test_parse_keeps_colons_inside_the_local_id():
    # Asset ids are themselves colon-separated ("memory:skill:deploy"), so the
    # split has to stop after the wiki segment or every asset urn loses its tail.
    urn = parse("asset:default:memory:skill:deploy")
    assert urn.kind == "asset"
    assert urn.wiki_id == "default"
    assert urn.local_id == "memory:skill:deploy"


def test_parse_accepts_the_vault_root_folder():
    urn = parse("folder:default:")
    assert urn.kind == "folder"
    assert urn.local_id == ""


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "entry",
        "entry:default",
        ":default:slug",
        "entry::slug",
        "wiki:default:slug",
        "entry:default:../etc/passwd",
        "entry:default:/leading-slash",
    ],
)
def test_parse_rejects_malformed_urns(bad):
    with pytest.raises(UrnError):
        parse(bad)


def test_parse_rejects_an_unknown_kind():
    with pytest.raises(UrnError):
        parse("secret:default:thing")


# ── Building ──────────────────────────────────────────────────────────────────


def test_build_round_trips_through_parse():
    urn = build("entry", "default", "people/alice")
    assert urn == "entry:default:people/alice"
    assert parse(urn).local_id == "people/alice"


def test_build_normalises_surrounding_and_duplicate_slashes():
    assert build("folder", "default", "/people/") == "folder:default:people"
    assert build("entry", "default", "people//alice") == "entry:default:people/alice"


def test_build_rejects_a_traversal_segment():
    with pytest.raises(UrnError):
        build("entry", "default", "people/../../etc")


# ── Ancestry ──────────────────────────────────────────────────────────────────


def test_entry_ancestors_run_nearest_folder_first_and_end_at_the_root():
    assert ancestors("entry:default:work/notes/alice") == [
        "folder:default:work/notes",
        "folder:default:work",
        "folder:default:",
    ]


def test_a_root_level_entry_has_only_the_vault_root_as_an_ancestor():
    assert ancestors("entry:default:alice") == ["folder:default:"]


def test_folder_ancestors_exclude_the_folder_itself():
    assert ancestors("folder:default:work/notes") == [
        "folder:default:work",
        "folder:default:",
    ]


def test_the_vault_root_has_no_ancestors():
    assert ancestors("folder:default:") == []


@pytest.mark.parametrize("urn", ["asset:default:memory:skill:deploy", "scope:default:person:self", "view:default:vw_1"])
def test_assets_scopes_and_views_do_not_inherit_from_folders(urn):
    # These do not live in the vault tree, so a grant on the root folder must
    # not sweep them in. They are shareable only by their own grant.
    assert ancestors(urn) == []


def test_ancestors_are_scoped_to_the_wiki_of_the_resource():
    assert ancestors("entry:other:work/alice") == [
        "folder:other:work",
        "folder:other:",
    ]

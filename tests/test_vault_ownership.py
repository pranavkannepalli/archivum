"""One vault directory belongs to one wiki.

No API creates a second wiki — `create_user` inherits the caller's — but
`WIKI_ID` is an env setting, so two backends can be pointed at one `WIKI_DIR`
with different ids. Nothing in the layout would notice: page paths are
`wiki_dir/<slug>.md` with no wiki in them, so the second deployment silently
overwrites the first's markdown and reconciliation then persists that content
into the other wiki's scoped rows.

Namespacing the directory by wiki is the wrong fix — the vault is a plain
markdown folder people open in Obsidian, and burying it under an id nobody
types would cost the primary user something real to serve a case that does not
exist yet. Refusing to start is the honest one.
"""

import pytest

from archivum.vault_scaffold import VaultOwnershipError, claim_vault_dir


def test_claiming_an_empty_directory_records_the_owner(tmp_path):
    claim_vault_dir(tmp_path, wiki_id="default")
    assert (tmp_path / ".archivum-wiki").read_text(encoding="utf-8").strip() == "default"


def test_reclaiming_your_own_vault_is_fine(tmp_path):
    claim_vault_dir(tmp_path, wiki_id="default")
    claim_vault_dir(tmp_path, wiki_id="default")


def test_a_vault_that_belongs_to_another_wiki_is_refused(tmp_path):
    claim_vault_dir(tmp_path, wiki_id="alpha")

    with pytest.raises(VaultOwnershipError) as caught:
        claim_vault_dir(tmp_path, wiki_id="beta")

    assert "alpha" in str(caught.value) and "beta" in str(caught.value)


def test_an_existing_vault_with_no_marker_is_adopted(tmp_path):
    """Upgrades must not require a migration to keep working."""
    (tmp_path / "notes.md").write_text("hello", encoding="utf-8")

    claim_vault_dir(tmp_path, wiki_id="default")

    assert (tmp_path / ".archivum-wiki").exists()

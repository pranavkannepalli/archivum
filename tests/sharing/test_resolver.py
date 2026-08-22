import aiosqlite
import pytest

from archivum.sharing.models import Subject
from archivum.sharing.repository import SharingRepository, init_sharing_schema
from archivum.sharing.resolver import list_visible, resolve


async def _repo(conn):
    await init_sharing_schema(conn)
    return SharingRepository(conn)


async def _principal(repo, name="Alice"):
    principal, _claim = await repo.create_principal("default", name)
    return Subject.principal(principal.id), principal


# ── The five resolution rules ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_direct_grant_grants_its_role():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        grant = await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="entry:default:people/alice",
            role="commenter",
            created_by="owner",
        )

        access = await resolve(repo, subject, "entry:default:people/alice")
        assert access is not None
        assert access.role == "commenter"
        assert access.grant_id == grant.id


@pytest.mark.asyncio
async def test_no_grant_means_no_access():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        assert await resolve(repo, subject, "entry:default:people/alice") is None


@pytest.mark.asyncio
async def test_a_folder_grant_reaches_entries_beneath_it():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:work",
            role="viewer",
            created_by="owner",
        )

        access = await resolve(repo, subject, "entry:default:work/notes/alice")
        assert access is not None
        assert access.role == "viewer"
        assert access.inherited_from == "folder:default:work"


@pytest.mark.asyncio
async def test_the_nearest_ancestor_wins_over_a_broader_one():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:",
            role="commenter",
            created_by="owner",
        )
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:work",
            role="viewer",
            created_by="owner",
        )

        access = await resolve(repo, subject, "entry:default:work/alice")
        assert access is not None
        assert access.role == "viewer"
        assert access.inherited_from == "folder:default:work"


@pytest.mark.asyncio
async def test_a_direct_grant_overrides_an_inherited_one():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:work",
            role="viewer",
            created_by="owner",
        )
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="entry:default:work/alice",
            role="commenter",
            created_by="owner",
        )

        access = await resolve(repo, subject, "entry:default:work/alice")
        assert access is not None
        assert access.role == "commenter"
        assert access.inherited_from is None


# ── Denial paths ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_revoked_grant_denies():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        grant = await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="entry:default:alice",
            role="viewer",
            created_by="owner",
        )
        await repo.revoke_grant(grant.id)
        assert await resolve(repo, subject, "entry:default:alice") is None


@pytest.mark.asyncio
async def test_an_expired_grant_denies():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="entry:default:alice",
            role="viewer",
            created_by="owner",
            expires_in_days=-1,
        )
        assert await resolve(repo, subject, "entry:default:alice") is None


@pytest.mark.asyncio
async def test_revoking_a_principal_denies_every_grant_it_held():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, principal = await _principal(repo)
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:work",
            role="viewer",
            created_by="owner",
        )
        await repo.revoke_principal(principal.id)
        assert await resolve(repo, subject, "entry:default:work/alice") is None


@pytest.mark.asyncio
async def test_a_hold_withholds_a_resource_the_grant_would_otherwise_cover():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        grant = await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:work",
            role="viewer",
            created_by="owner",
        )
        await repo.hold(grant.id, "entry:default:work/secret")

        assert await resolve(repo, subject, "entry:default:work/secret") is None
        # Siblings are unaffected — a hold is per resource, not per grant.
        assert await resolve(repo, subject, "entry:default:work/public") is not None


@pytest.mark.asyncio
async def test_releasing_a_hold_restores_access():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        grant = await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:work",
            role="viewer",
            created_by="owner",
        )
        await repo.hold(grant.id, "entry:default:work/secret")
        await repo.release_hold(grant.id, "entry:default:work/secret")
        assert await resolve(repo, subject, "entry:default:work/secret") is not None


@pytest.mark.asyncio
async def test_a_hold_on_a_broad_grant_does_not_leak_through_a_narrower_one():
    # If two grants cover the same resource and only one is held, the resource
    # stays hidden. Anything else turns "hold" into a suggestion.
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        broad = await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:",
            role="viewer",
            created_by="owner",
        )
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:work",
            role="commenter",
            created_by="owner",
        )
        await repo.hold(broad.id, "entry:default:work/secret")

        assert await resolve(repo, subject, "entry:default:work/secret") is None


# ── Tenancy and subject isolation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_principal_cannot_see_anothers_grant():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        alice, _ = await _principal(repo, "Alice")
        bob, _ = await _principal(repo, "Bob")
        await repo.create_grant(
            wiki_id="default",
            subject=alice,
            resource_urn="entry:default:alice",
            role="viewer",
            created_by="owner",
        )
        assert await resolve(repo, bob, "entry:default:alice") is None


@pytest.mark.asyncio
async def test_a_grant_does_not_reach_across_wikis():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:work",
            role="viewer",
            created_by="owner",
        )
        assert await resolve(repo, subject, "entry:other:work/alice") is None


@pytest.mark.asyncio
async def test_a_root_folder_grant_does_not_reach_assets_or_scopes():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="folder:default:",
            role="viewer",
            created_by="owner",
        )
        assert await resolve(repo, subject, "asset:default:memory:skill:deploy") is None
        assert await resolve(repo, subject, "scope:default:person:self") is None


# ── Link subjects ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_link_grant_resolves_for_the_token_that_created_it():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        grant, token = await repo.create_link_grant(
            wiki_id="default",
            resource_urn="entry:default:alice",
            role="viewer",
            created_by="owner",
        )

        subject = Subject.link_from_token(token)
        access = await resolve(repo, subject, "entry:default:alice")
        assert access is not None
        assert access.grant_id == grant.id


@pytest.mark.asyncio
async def test_a_wrong_link_token_resolves_to_nothing():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        await repo.create_link_grant(
            wiki_id="default",
            resource_urn="entry:default:alice",
            role="viewer",
            created_by="owner",
        )
        assert await resolve(repo, Subject.link_from_token("nope"), "entry:default:alice") is None


@pytest.mark.asyncio
async def test_link_tokens_are_not_stored_in_the_clear():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        _grant, token = await repo.create_link_grant(
            wiki_id="default",
            resource_urn="entry:default:alice",
            role="viewer",
            created_by="owner",
        )
        async with conn.execute("SELECT subject_id FROM share_grants") as cur:
            rows = [row[0] for row in await cur.fetchall()]
        assert token not in rows


# ── Listing ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_visible_returns_live_grants_and_omits_dead_ones():
    async with aiosqlite.connect(":memory:") as conn:
        repo = await _repo(conn)
        subject, _ = await _principal(repo)
        await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="entry:default:kept",
            role="viewer",
            created_by="owner",
        )
        gone = await repo.create_grant(
            wiki_id="default",
            subject=subject,
            resource_urn="entry:default:gone",
            role="viewer",
            created_by="owner",
        )
        await repo.revoke_grant(gone.id)

        urns = {access.resource_urn for access in await list_visible(repo, subject)}
        assert urns == {"entry:default:kept"}

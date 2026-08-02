"""server/core/embeddings.py -- ai.embeddings storage + search. Real
vectors against a real pgvector column throughout; no mocking of the DB
layer, same reasoning tests/conftest.py already gives for running
against real Postgres rather than a fake driver -- the whole point here
is a real nearest-neighbor query under a real visibility filter.

The visibility scoping test (a different user's body-derived embedding
must never come back, regardless of similarity) is the critical case
-- this is safety rule 10's actual regression coverage for the search
axis, the same way test_calendar_provider.py's DST tests are safety
rule 7's.
"""
from __future__ import annotations

from server.core import embeddings
from server.db.schema import EMBEDDING_DIM


def _vector(seed: float) -> list[float]:
    return [seed] * EMBEDDING_DIM


class TestChunkText:
    def test_short_text_is_one_chunk(self):
        assert embeddings.chunk_text("hello world") == ["hello world"]

    def test_blank_text_produces_no_chunks(self):
        assert embeddings.chunk_text("   ") == []

    def test_long_text_is_split_into_multiple_chunks(self):
        text = "x" * 5000
        chunks = embeddings.chunk_text(text, chunk_chars=2000)
        assert len(chunks) == 3
        assert sum(len(c) for c in chunks) == 5000


class TestStoreChunk:
    def test_stores_and_returns_the_row(self, org_id, admin):
        row = embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id="11111111-1111-1111-1111-111111111111",
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="hello world", vector=_vector(0.1), model="fake-model",
        )
        assert row["content_text"] == "hello world"
        assert row["dim"] == EMBEDDING_DIM
        assert str(row["visibility_user_id"]) == str(admin["id"])

    def test_re_storing_the_same_content_upserts_rather_than_duplicates(self, org_id, admin):
        kwargs = dict(
            org_id=org_id, owner_type="interaction",
            owner_id="11111111-1111-1111-1111-111111111111",
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="hello world",
        )
        first = embeddings.store_chunk(vector=_vector(0.1), model="model-a", **kwargs)
        second = embeddings.store_chunk(vector=_vector(0.2), model="model-b", **kwargs)
        assert first["id"] == second["id"]
        results = embeddings.search(org_id, str(admin["id"]), _vector(0.2), limit=10)
        assert len(results) == 1
        assert results[0]["id"] == str(first["id"])


class TestDeleteForOwner:
    def test_removes_every_chunk_for_that_owner(self, org_id, admin):
        owner_id = "11111111-1111-1111-1111-111111111111"
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id=owner_id,
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="chunk one", vector=_vector(0.1), model="fake",
        )
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id=owner_id,
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=1,
            content_text="chunk two", vector=_vector(0.2), model="fake",
        )
        embeddings.delete_for_owner(org_id, "interaction", owner_id)
        results = embeddings.search(org_id, str(admin["id"]), _vector(0.15), limit=10)
        assert results == []


class TestSearchVisibilityScoping:
    """Safety rule 10's regression coverage for the search axis."""

    def test_the_owner_sees_their_own_body_derived_embedding(self, org_id, admin):
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id="11111111-1111-1111-1111-111111111111",
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="a private email body", vector=_vector(0.5), model="fake",
        )
        results = embeddings.search(org_id, str(admin["id"]), _vector(0.5), limit=10)
        assert len(results) == 1
        assert results[0]["content_text"] == "a private email body"

    def test_a_different_user_never_sees_it_regardless_of_similarity(self, org_id, admin, member):
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id="11111111-1111-1111-1111-111111111111",
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="a private email body", vector=_vector(0.5), model="fake",
        )
        # An EXACT vector match -- maximal similarity -- still must not
        # surface to a user who isn't the owner.
        results = embeddings.search(org_id, str(member["id"]), _vector(0.5), limit=10)
        assert results == []

    def test_org_wide_content_null_visibility_is_visible_to_every_user(self, org_id, admin, member):
        embeddings.store_chunk(
            org_id, owner_type="organization", owner_id="22222222-2222-2222-2222-222222222222",
            visibility_user_id=None, chunk_kind="description", chunk_index=0,
            content_text="Acme Corp is a widget maker", vector=_vector(0.7), model="fake",
        )
        as_admin = embeddings.search(org_id, str(admin["id"]), _vector(0.7), limit=10)
        as_member = embeddings.search(org_id, str(member["id"]), _vector(0.7), limit=10)
        assert len(as_admin) == 1
        assert len(as_member) == 1

    def test_results_are_scoped_to_the_calling_org(self, org_id, admin):
        from server.core import users
        org2 = users.create_org("Second Org", "second-embed-org")
        embeddings.store_chunk(
            str(org2["id"]), owner_type="organization",
            owner_id="33333333-3333-3333-3333-333333333333",
            visibility_user_id=None, chunk_kind="description", chunk_index=0,
            content_text="a different org's content", vector=_vector(0.9), model="fake",
        )
        results = embeddings.search(org_id, str(admin["id"]), _vector(0.9), limit=10)
        assert results == []

    def test_nearest_neighbor_ordering(self, org_id, admin):
        # Cosine distance is scale-invariant -- two constant-valued
        # vectors that are merely different POSITIVE magnitudes of the
        # same direction (e.g. [0.5]*N vs [0.1]*N) are equidistant (0)
        # from the query. Using the query's exact direction for "close"
        # and its NEGATION for "far" gives genuinely different distances
        # (0 vs the maximum, 2).
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id="11111111-1111-1111-1111-111111111111",
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="close match", vector=_vector(0.5), model="fake",
        )
        embeddings.store_chunk(
            org_id, owner_type="interaction", owner_id="44444444-4444-4444-4444-444444444444",
            visibility_user_id=str(admin["id"]), chunk_kind="body", chunk_index=0,
            content_text="far match", vector=_vector(-0.5), model="fake",
        )
        results = embeddings.search(org_id, str(admin["id"]), _vector(0.5), limit=10)
        assert [r["content_text"] for r in results] == ["close match", "far match"]

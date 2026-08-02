"""server/core/interaction_embeddings.py's subscriber (enqueues a job,
never embeds inline) + server/jobs/embed_interaction.py's handler (does
the actual chunk/embed/store). The LLM adapter is mocked at its own
boundary throughout -- no real network call anywhere.
"""
from __future__ import annotations

from unittest import mock

from server.core import embeddings, interaction_embeddings, repository
from server.db.schema import EMBEDDING_DIM
from server.jobs import embed_interaction, queue
from server.llm import embeddings as embedding_provider


def _fake_embed(org_id, text):
    return [0.1] * EMBEDDING_DIM, "fake-model"


class TestSubscriberEnqueuesRatherThanEmbedsInline:
    def test_an_inbound_email_enqueues_an_embed_interaction_job(self, org_id, principal, admin):
        with mock.patch.object(embedding_provider, "embed") as fake_embed:
            repository.create(principal, "interaction", {
                "owner_id": str(admin["id"]), "kind": "email", "direction": "inbound",
                "from_channel": "jane@example.com", "body": "Let's meet Tuesday",
            })
        fake_embed.assert_not_called()  # the subscriber only enqueues, never calls the LLM itself
        claimed = queue.claim_batch(
            "test-worker", (interaction_embeddings.JOB_TYPE,), 10
        )
        assert len(claimed) == 1
        assert claimed[0]["payload"]["org_id"] == org_id

    def test_an_interaction_with_no_body_does_not_enqueue(self, org_id, principal, admin):
        repository.create(principal, "interaction", {
            "owner_id": str(admin["id"]), "kind": "email", "direction": "inbound",
            "from_channel": "jane@example.com", "body": "   ",
        })
        assert queue.claim_batch("test-worker", (interaction_embeddings.JOB_TYPE,), 10) == []


class TestEmbedInteractionHandler:
    def test_chunks_embeds_and_stores_the_body(self, org_id, principal, admin):
        interaction = repository.create(principal, "interaction", {
            "owner_id": str(admin["id"]), "kind": "email", "direction": "inbound",
            "from_channel": "jane@example.com", "body": "Let's meet Tuesday at 3pm",
        })
        with mock.patch.object(embedding_provider, "embed", side_effect=_fake_embed):
            embed_interaction.handle_embed_interaction({
                "org_id": org_id, "interaction_id": str(interaction["id"]),
                "owner_id": str(admin["id"]),
            })
        results = embeddings.search(org_id, str(admin["id"]), [0.1] * EMBEDDING_DIM, limit=10)
        assert len(results) == 1
        assert results[0]["content_text"] == "Let's meet Tuesday at 3pm"
        assert results[0]["owner_type"] == "interaction"

    def test_stores_with_the_interaction_owner_as_visibility_user_id(
        self, org_id, principal, admin, member
    ):
        """Safety rule 10 -- body-derived content is owner-scoped, not
        org-wide."""
        interaction = repository.create(principal, "interaction", {
            "owner_id": str(admin["id"]), "kind": "email", "direction": "inbound",
            "from_channel": "jane@example.com", "body": "confidential body",
        })
        with mock.patch.object(embedding_provider, "embed", side_effect=_fake_embed):
            embed_interaction.handle_embed_interaction({
                "org_id": org_id, "interaction_id": str(interaction["id"]),
                "owner_id": str(admin["id"]),
            })
        as_owner = embeddings.search(org_id, str(admin["id"]), [0.1] * EMBEDDING_DIM, limit=10)
        as_other = embeddings.search(org_id, str(member["id"]), [0.1] * EMBEDDING_DIM, limit=10)
        assert len(as_owner) == 1
        assert as_other == []

    def test_a_deleted_interaction_is_a_no_op_not_an_error(self, org_id):
        embed_interaction.handle_embed_interaction({
            "org_id": org_id, "interaction_id": "00000000-0000-0000-0000-000000000000",
            "owner_id": "00000000-0000-0000-0000-000000000000",
        })  # must not raise


class TestDeletingAnInteractionRemovesItsEmbeddings:
    def test_deleting_the_interaction_deletes_its_stored_embeddings(
        self, org_id, principal, admin
    ):
        interaction = repository.create(principal, "interaction", {
            "owner_id": str(admin["id"]), "kind": "email", "direction": "inbound",
            "from_channel": "jane@example.com", "body": "sensitive content",
        })
        with mock.patch.object(embedding_provider, "embed", side_effect=_fake_embed):
            embed_interaction.handle_embed_interaction({
                "org_id": org_id, "interaction_id": str(interaction["id"]),
                "owner_id": str(admin["id"]),
            })
        assert embeddings.search(org_id, str(admin["id"]), [0.1] * EMBEDDING_DIM, limit=10)

        repository.delete(principal, "interaction", str(interaction["id"]))

        assert embeddings.search(org_id, str(admin["id"]), [0.1] * EMBEDDING_DIM, limit=10) == []

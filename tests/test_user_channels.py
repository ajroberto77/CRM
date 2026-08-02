"""server/core/user_channels.py -- resolves an inbound Signal/Telegram
sender to a CRM user. No real network call anywhere; pure DB + Python
logic.
"""
from __future__ import annotations

import pytest

from server.core import user_channels
from server.core.identity import NormalizationError


class TestLinkChannel:
    def test_creates_a_new_link(self, org_id, admin):
        channel = user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        assert channel["kind"] == "signal"
        assert channel["value_normalized"] == "+15550100123"
        assert channel["value_raw"] == "+15550100123"
        assert str(channel["user_id"]) == str(admin["id"])

    def test_re_linking_the_same_value_to_the_same_user_is_idempotent(self, org_id, admin):
        first = user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        second = user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        assert first["id"] == second["id"]
        assert len(user_channels.list_channels(org_id, user_id=str(admin["id"]))) == 1

    def test_linking_a_value_already_claimed_by_another_user_raises(self, org_id, admin, member):
        user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        with pytest.raises(user_channels.AlreadyLinkedError):
            user_channels.link_channel(org_id, str(member["id"]), "signal", "+15550100123")

    def test_an_unnormalizable_value_raises(self, org_id, admin):
        with pytest.raises(NormalizationError):
            user_channels.link_channel(org_id, str(admin["id"]), "telegram", "!!")

    def test_telegram_handle_is_normalized(self, org_id, admin):
        channel = user_channels.link_channel(org_id, str(admin["id"]), "telegram", "@SomeHandle")
        assert channel["value_normalized"] == "somehandle"
        assert channel["value_raw"] == "@SomeHandle"


class TestListChannels:
    def test_scoped_to_one_user(self, org_id, admin, member):
        user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        user_channels.link_channel(org_id, str(member["id"]), "telegram", "@member")
        assert len(user_channels.list_channels(org_id, user_id=str(admin["id"]))) == 1
        assert len(user_channels.list_channels(org_id, user_id=str(member["id"]))) == 1
        assert len(user_channels.list_channels(org_id)) == 2


class TestUnlinkChannel:
    def test_removes_the_row(self, org_id, admin):
        channel = user_channels.link_channel(org_id, str(admin["id"]), "signal", "+15550100123")
        user_channels.unlink_channel(org_id, str(channel["id"]))
        assert user_channels.list_channels(org_id, user_id=str(admin["id"])) == []


class TestResolveUser:
    def test_resolves_a_linked_raw_value(self, org_id, admin):
        user_channels.link_channel(org_id, str(admin["id"]), "signal", "+1 555 010 0123")
        resolved = user_channels.resolve_user(org_id, "signal", "+15550100123")
        assert resolved == str(admin["id"])

    def test_an_unrecognized_sender_resolves_to_none(self, org_id):
        assert user_channels.resolve_user(org_id, "signal", "+19995550000") is None

    def test_an_unnormalizable_value_resolves_to_none_not_an_error(self, org_id):
        assert user_channels.resolve_user(org_id, "telegram", "!!") is None

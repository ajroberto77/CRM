"""Passwords, sessions, first-run setup, and the API auth surface."""
from __future__ import annotations

import pytest

from server.core import passwords, sessions, users


class TestPasswords:
    def test_roundtrip(self):
        stored = passwords.hash_password("correct-horse-battery")
        assert passwords.verify_password("correct-horse-battery", stored)
        assert not passwords.verify_password("wrong", stored)

    def test_salt_is_random(self):
        a = passwords.hash_password("same-password-twice")
        b = passwords.hash_password("same-password-twice")
        assert a != b

    def test_null_hash_never_authenticates(self):
        """An invited-but-not-activated user has no hash and must not be able
        to log in with anything, including an empty password."""
        assert not passwords.verify_password("", None)
        assert not passwords.verify_password("anything", None)

    @pytest.mark.parametrize(
        "stored", ["", "garbage", "pbkdf2_sha256$notanint$aa$bb", "a$b$c", "x$1$zz$zz"]
    )
    def test_malformed_hash_denies_rather_than_raising(self, stored):
        """A corrupted row must deny access, not 500 every request."""
        assert passwords.verify_password("anything", stored) is False

    def test_format_is_self_describing_so_cost_can_be_raised(self):
        stored = passwords.hash_password("correct-horse-battery")
        algorithm, iterations, salt, digest = stored.split("$")
        assert algorithm == passwords.ALGORITHM
        assert int(iterations) == passwords.ITERATIONS
        assert salt and digest

        weaker = f"{passwords.ALGORITHM}$1000${salt}${digest}"
        assert passwords.needs_rehash(weaker)
        assert not passwords.needs_rehash(stored)

    def test_strength_floor(self):
        with pytest.raises(passwords.WeakPasswordError):
            passwords.check_password_strength("short")
        passwords.check_password_strength("x" * passwords.MIN_PASSWORD_LENGTH)

    def test_token_is_not_stored_in_recoverable_form(self):
        token = passwords.new_session_token()
        assert passwords.hash_token(token) != token
        assert passwords.hash_token(token) == passwords.hash_token(token)


class TestAuthenticate:
    def test_success(self, org_id, member):
        user = sessions.authenticate("member@example.com", "correct-horse-battery")
        assert str(user["id"]) == str(member["id"])

    def test_email_is_normalized_at_login(self, org_id, member):
        user = sessions.authenticate("  MEMBER@Example.COM ", "correct-horse-battery")
        assert str(user["id"]) == str(member["id"])

    def test_wrong_password(self, org_id, member):
        with pytest.raises(sessions.AuthenticationError):
            sessions.authenticate("member@example.com", "nope")

    def test_unknown_user(self, org_id):
        with pytest.raises(sessions.AuthenticationError):
            sessions.authenticate("nobody@example.com", "whatever")

    def test_disabled_user_cannot_log_in(self, org_id, member):
        users.set_status(org_id, str(member["id"]), "disabled")
        with pytest.raises(sessions.AuthenticationError):
            sessions.authenticate("member@example.com", "correct-horse-battery")

    def test_failure_messages_are_identical(self, org_id, member):
        """Distinguishing them turns login into an account-enumeration oracle."""
        messages = set()
        for email, password in [
            ("member@example.com", "wrong"),
            ("nobody@example.com", "wrong"),
        ]:
            with pytest.raises(sessions.AuthenticationError) as exc:
                sessions.authenticate(email, password)
            messages.add(str(exc.value))
        assert len(messages) == 1


class TestSessions:
    def test_create_and_resolve(self, org_id, member):
        token, row = sessions.create_session(org_id, str(member["id"]))
        resolved = sessions.resolve_session(token)
        assert resolved is not None
        assert str(resolved["user"]["id"]) == str(member["id"])
        assert str(resolved["session_id"]) == str(row["id"])

    def test_unknown_token(self):
        assert sessions.resolve_session("not-a-real-token") is None
        assert sessions.resolve_session("") is None

    def test_revoked_session_stops_working(self, org_id, member):
        token, row = sessions.create_session(org_id, str(member["id"]))
        assert sessions.revoke_session(org_id, str(row["id"]))
        assert sessions.resolve_session(token) is None

    def test_disabling_a_user_invalidates_live_sessions(self, org_id, member):
        """Status is checked in the resolve query itself, so there is no window
        where a disabled user keeps working until their session expires."""
        token, _ = sessions.create_session(org_id, str(member["id"]))
        assert sessions.resolve_session(token) is not None
        users.set_status(org_id, str(member["id"]), "disabled")
        assert sessions.resolve_session(token) is None

    def test_revoke_all_for_user(self, org_id, member):
        tokens = [sessions.create_session(org_id, str(member["id"]))[0] for _ in range(3)]
        assert sessions.revoke_all_for_user(org_id, str(member["id"])) == 3
        assert all(sessions.resolve_session(t) is None for t in tokens)


class TestFirstRun:
    def test_setup_creates_org_and_admin_then_logs_in(self, client):
        response = client.post(
            "/setup",
            json={
                "org_name": "Acme",
                "email": "admin@acme.com",
                "name": "Admin",
                "password": "correct-horse-battery",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["user"]["is_admin"] is True
        assert body["org"]["name"] == "Acme"

        me = client.get("/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
        assert me.status_code == 200
        assert me.json()["is_admin"] is True

    def test_setup_is_closed_once_configured(self, client):
        first = client.post(
            "/setup",
            json={
                "org_name": "Acme",
                "email": "admin@acme.com",
                "password": "correct-horse-battery",
            },
        )
        assert first.status_code == 201
        second = client.post(
            "/setup",
            json={
                "org_name": "Sneaky",
                "email": "attacker@evil.com",
                "password": "correct-horse-battery",
            },
        )
        assert second.status_code == 409

    def test_weak_password_is_refused(self, client):
        response = client.post(
            "/setup",
            json={"org_name": "Acme", "email": "admin@acme.com", "password": "short"},
        )
        assert response.status_code == 422

    def test_health_reports_first_run_and_rls(self, client):
        body = client.get("/health").json()
        assert body["ok"] is True
        assert body["first_run_required"] is True
        assert body["database"]["rls_enforced"] is True


class TestApiAuth:
    def test_unauthenticated_requests_are_refused(self, client):
        """Fails closed: no ambient access before a session exists."""
        assert client.get("/auth/me").status_code == 401
        assert client.get("/users").status_code == 401

    def test_login_logout_cycle(self, client, org_id, member):
        response = client.post(
            "/auth/login",
            json={"email": "member@example.com", "password": "correct-horse-battery"},
        )
        assert response.status_code == 200
        token = response.json()["token"]

        assert client.get("/auth/me").status_code == 200  # cookie
        assert client.post("/auth/logout").status_code == 200
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401

    def test_bad_login_is_401_with_no_detail(self, client, org_id, member):
        response = client.post(
            "/auth/login", json={"email": "member@example.com", "password": "wrong"}
        )
        assert response.status_code == 401
        assert "invalid email or password" in response.json()["detail"]

    def test_member_cannot_reach_admin_routes(self, client, org_id, member):
        client.post(
            "/auth/login",
            json={"email": "member@example.com", "password": "correct-horse-battery"},
        )
        assert client.get("/users").status_code == 403

    def test_admin_can(self, client, org_id, admin):
        client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "correct-horse-battery"},
        )
        assert client.get("/users").status_code == 200

    def test_password_hash_never_leaves_the_api(self, client, org_id, admin):
        client.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "correct-horse-battery"},
        )
        assert "password_hash" not in client.get("/users").text
        assert "password_hash" not in client.get("/auth/me").text

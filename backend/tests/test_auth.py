"""Authentication: login, sessions, logout, CSRF."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models import Session as LoginSession
from app.models import User
from tests.conftest import PASSWORD, login


def test_login_sets_httponly_cookie_and_returns_csrf(client, make_user):
    make_user("olivia@example.com", groups=("operations",))
    resp = client.post(
        "/api/auth/login", json={"email": "Olivia@Example.com ", "password": PASSWORD}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == "olivia@example.com"  # normalized
    assert body["user"]["groups"] == ["operations"]
    assert len(body["csrf_token"]) >= 32
    cookie = resp.headers["set-cookie"]
    assert "oc_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/auth/me").json()["user"]["email"] == "olivia@example.com"


def test_wrong_password_and_unknown_email_get_the_same_answer(client, make_user):
    make_user("olivia@example.com")
    wrong = client.post(
        "/api/auth/login", json={"email": "olivia@example.com", "password": "x" * 12}
    )
    unknown = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": "x" * 12}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Invalid email or password."}


def test_only_a_hash_of_the_session_token_is_stored(client, make_user, db):
    make_user("olivia@example.com")
    login(client, "olivia@example.com")
    token = client.cookies.get("oc_session")
    stored = db.scalars(select(LoginSession.token_hash)).all()
    assert token not in stored and len(stored) == 1


def test_password_is_hashed_with_argon2(make_user, db):
    make_user("olivia@example.com")
    stored = db.scalar(select(User.password_hash))
    assert PASSWORD not in stored and stored.startswith("$argon2id$")


def test_requests_without_a_session_are_rejected(client):
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set("oc_session", "forged-token")
    assert client.get("/api/auth/me").status_code == 401


def test_logout_invalidates_the_session_server_side(client, make_user):
    make_user("olivia@example.com")
    csrf = login(client, "olivia@example.com")
    token = client.cookies.get("oc_session")

    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204
    # Replaying the old cookie must fail: the session row is gone, not just the cookie.
    client.cookies.set("oc_session", token)
    assert client.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected(client, make_user, db):
    make_user("olivia@example.com")
    login(client, "olivia@example.com")
    session = db.scalar(select(LoginSession))
    session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.get("/api/auth/me").status_code == 401


def test_deactivated_user_loses_access_immediately(client, make_user, db):
    user = make_user("olivia@example.com")
    login(client, "olivia@example.com")
    assert client.get("/api/auth/me").status_code == 200

    user.is_active = False
    db.commit()
    assert client.get("/api/auth/me").status_code == 401  # existing session, next request
    resp = client.post(
        "/api/auth/login", json={"email": "olivia@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 401


def test_state_changing_requests_need_the_csrf_token(client, make_user):
    make_user("olivia@example.com")
    csrf = login(client, "olivia@example.com")

    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 204

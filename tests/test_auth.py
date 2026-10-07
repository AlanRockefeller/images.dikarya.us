from sqlalchemy import func, select

from images_app.models import User, UserSession

from .conftest import csrf, login


def register(client, email="new@example.org", password="a good long password", name="New Person"):
    return client.post(
        "/register",
        data={"name": name, "email": email, "password": password, "csrf_token": csrf(client)},
        follow_redirects=False,
    )


def test_registration_creates_account_and_logs_in(client, db):
    response = register(client, email="New@Example.org")
    assert response.status_code == 303
    user = db.scalar(select(User))
    assert user.email == "new@example.org"
    assert user.password_hash.startswith("$argon2id$")
    assert client.get("/me").status_code == 200


def test_duplicate_email_is_rejected_case_insensitively(client, db):
    assert register(client, email="dup@example.org").status_code == 303
    client.cookies.clear()
    response = register(client, email="DUP@example.org")
    assert response.status_code == 409
    assert "already exists" in response.text
    assert db.scalar(select(func.count()).select_from(User)) == 1


def test_registration_validates_input(client):
    assert register(client, password="short").status_code == 422
    assert register(client, email="not-an-email").status_code == 422
    assert register(client, name="   ").status_code == 422


def test_login_and_logout(client, make, db):
    make.user()
    login(client)
    assert client.get("/me").status_code == 200
    assert db.scalar(select(func.count()).select_from(UserSession)) == 1
    response = client.post("/logout", data={"csrf_token": csrf(client)}, follow_redirects=False)
    assert response.status_code == 303
    assert db.scalar(select(func.count()).select_from(UserSession)) == 0
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_logout_invalidates_a_copied_session_cookie(client, make):
    make.user()
    login(client)
    stolen = client.cookies["session"]
    client.post("/logout", data={"csrf_token": csrf(client)})
    client.cookies.set("session", stolen, domain="testserver.local")
    assert client.get("/me", follow_redirects=False).status_code == 303


def test_login_issues_a_fresh_session_token(client, make):
    make.user()
    client.cookies.set("session", "attacker-chosen-token", domain="testserver.local")
    login(client)
    assert client.cookies["session"] != "attacker-chosen-token"


def test_wrong_password(client, make):
    make.user()
    response = client.post(
        "/login", data={"email": "ranker@example.org", "password": "nope nope nope", "csrf_token": csrf(client)}
    )
    assert response.status_code == 401
    assert "don’t match" in response.text


def test_login_is_rate_limited(client, make):
    make.user()
    for _ in range(10):
        client.post("/login", data={"email": "ranker@example.org", "password": "wrong password!", "csrf_token": csrf(client)})
    response = client.post(
        "/login", data={"email": "ranker@example.org", "password": "a good long password", "csrf_token": csrf(client)}
    )
    assert response.status_code == 429


def test_forms_require_csrf_token(client, make):
    make.user()
    client.get("/about")
    response = client.post("/login", data={"email": "ranker@example.org", "password": "a good long password"})
    assert response.status_code == 403


def test_cross_origin_posts_are_refused(client, make):
    make.user()
    response = client.post(
        "/login",
        data={"email": "ranker@example.org", "password": "a good long password", "csrf_token": csrf(client)},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_next_parameter_only_allows_local_paths(client, make):
    make.user()
    response = client.post(
        "/login",
        data={"email": "ranker@example.org", "password": "a good long password",
              "csrf_token": csrf(client), "next": "//evil.example/"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/"


def test_admin_requires_admin(client, make):
    make.user()
    login(client)
    assert client.get("/admin").status_code == 403


def test_singular_hostname_redirects_preserving_path_and_query(client):
    response = client.get("/foo?bar=1", headers={"Host": "image.dikarya.us"}, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "http://testserver/foo?bar=1"


def test_security_headers(client):
    response = client.get("/about")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"

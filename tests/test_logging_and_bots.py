import time

import pytest
from sqlalchemy import func, select

from images_app import logs, threats
from images_app.models import User

from .conftest import csrf, form_stamp, login


@pytest.fixture
def species(make):
    species = make.taxon(48715, "Amanita muscaria")
    images = [make.image(species) for _ in range(3)]
    make.refresh()
    return species, images


def sign_up(client, **extra):
    data = {"name": "Real Person", "email": "real@example.org", "password": "a good long password",
            "csrf_token": csrf(client), "stamp": form_stamp()}
    return client.post("/register", data=data | extra, follow_redirects=False)


# --- Bot sign-ups -----------------------------------------------------------------

def test_registration_page_has_honeypot_and_stamp(client):
    page = client.get("/register").text
    assert 'name="website"' in page and 'name="stamp"' in page


def test_honeypot_blocks_bots(client, db, logged):
    response = sign_up(client, website="http://spam.example")
    assert response.status_code == 400
    assert db.scalar(select(func.count()).select_from(User)) == 0
    assert "reason=honeypot" in logged.find("scanner", "register.bot")


def test_instant_submissions_are_blocked(client, db, logged):
    assert sign_up(client, stamp=form_stamp(age=0)).status_code == 400
    assert "reason=too_fast" in logged.find("scanner", "register.bot")
    assert sign_up(client, stamp="123.forged").status_code == 400
    assert "reason=missing_or_forged_stamp" in logged.find("scanner", "register.bot")
    assert db.scalar(select(func.count()).select_from(User)) == 0


def test_a_blocked_person_can_simply_resubmit(client, db):
    page = sign_up(client, stamp=form_stamp(age=0)).text
    assert 'value="real@example.org"' in page  # their input is kept
    assert sign_up(client).status_code == 303
    assert db.scalar(select(User.email)) == "real@example.org"


def test_successful_registration_is_logged(client, logged):
    sign_up(client)
    line = logged.find("activity", "register.ok")
    assert "user=1:real@example.org" in line and "password" not in line


# --- Activity ----------------------------------------------------------------------

def test_logins_and_failures_are_logged(client, make, logged):
    make.user()
    client.post("/login", data={"email": "ranker@example.org", "password": "wrong password!",
                                "csrf_token": csrf(client)})
    assert "reason=wrong_password" in logged.find("activity", "login.failed")
    login(client)
    assert "user=1:ranker@example.org" in logged.find("activity", "login.ok")
    client.post("/logout", data={"csrf_token": csrf(client)})
    logged.find("activity", "logout")


def test_ranking_changes_are_logged(client, make, species, logged):
    _, images = species
    make.user()
    login(client)
    headers = {"X-CSRF-Token": csrf(client)}
    client.put("/api/taxa/48715/ranking", json={"image_ids": [images[0].id, images[1].id]}, headers=headers)
    first = logged.find("activity", "ranking.save")
    assert f"added={images[0].id},{images[1].id}" in first and "taxon=48715" in first
    client.put("/api/taxa/48715/ranking", json={"image_ids": [images[1].id]}, headers=headers)
    second = logged.find("activity", "ranking.save")
    assert f"removed={images[0].id}" in second and f"ranking={images[1].id}" in second


def test_search_and_admin_mapping_are_logged(client, species, logged):
    client.get("/search?q=amanita")
    assert 'q=amanita' in logged.find("activity", "search")


def test_browser_problems_are_logged(client, logged):
    response = client.post("/api/client-problem", headers={"X-CSRF-Token": csrf(client)},
                           json={"kind": "save_failed", "message": "network down", "page": "/taxon/1/x"})
    assert response.status_code == 204
    assert 'message="network down"' in logged.find("activity", "browser.save_failed")


def test_broken_internal_links_vs_random_404s(client, logged):
    client.get("/taxon/999/nothing", headers={"Referer": "http://testserver/taxa"})
    logged.find("activity", "broken_link")
    client.get("/some/random/thing")
    logged.find("scanner", "not_found")


# --- Scanners vs targeted attacks --------------------------------------------------

def test_scanner_probes_go_to_the_scanner_log(client, logged):
    for path in ("/wp-login.php", "/.env", "/.git/config", "/phpmyadmin/index.php", "/cgi-bin/luci"):
        client.get(path)
    assert logged.events("scanner").count("probe") == 5
    assert logged.events("attack") == []


def test_payloads_against_real_pages_are_targeted_attacks(client, species, logged):
    client.get("/search", params={"q": "' OR 1=1--"})
    line = logged.find("attack", "payload.sql_injection")
    assert "path=/search" in line
    client.get("/taxon/48715/amanita-muscaria", params={"sort": "<script>alert(1)</script>"})
    logged.find("attack", "payload.script_injection")
    # The same payload against a page that doesn't exist is just a scanner.
    client.get("/index.php", params={"id": "1 UNION SELECT password FROM users"})
    logged.find("scanner", "payload.sql_injection")


def test_attackers_are_watched_afterwards(client, species, logged):
    client.get("/search", params={"q": "../../etc/passwd"})
    client.get("/taxa")
    assert "path=/taxa" in logged.find("attack", "watched_ip.request")


def test_watch_list_is_bounded(monkeypatch):
    monkeypatch.setattr(logs, "MAX_WATCHED", 3)
    for ip in ("2001:db8::1", "2001:db8::2", "2001:db8::3", "2001:db8::4"):
        logs.watch(ip)
    assert len(logs._watched) == 3
    assert not logs.is_watched("2001:db8::1") and logs.is_watched("2001:db8::4")


def test_attack_tools_on_real_pages_are_targeted(client, logged):
    client.get("/", headers={"User-Agent": "sqlmap/1.8#stable (https://sqlmap.org)"})
    logged.find("attack", "attack_tool")


def test_form_field_payloads_are_attacks(client, logged):
    client.post("/login", data={"email": "admin' or '1'='1", "password": "x", "csrf_token": csrf(client)})
    assert "field=email" in logged.find("attack", "payload.sql_injection")


def test_attacks_by_signed_in_members_are_attributed(client, make, logged):
    make.user()
    login(client)
    client.get("/search", params={"q": "{{7*7}}"})
    assert "user=1:ranker@example.org" in logged.find("attack", "payload.template_injection")


def test_cross_site_forgery_is_an_attack_but_blind_posts_are_scanners(client, make, logged):
    make.user()
    client.post("/login", data={"email": "x@example.org", "password": "x", "csrf_token": csrf(client)},
                headers={"Origin": "https://evil.example"})
    assert "origin=https://evil.example" in logged.find("attack", "csrf.cross_site")
    client.cookies.clear()
    client.post("/login", data={"email": "x@example.org", "password": "x"})
    logged.find("scanner", "csrf.blind_post")


def test_password_guessing_is_an_attack(client, make, logged):
    make.user()
    for _ in range(11):
        client.post("/login", data={"email": "ranker@example.org", "password": "guess guess",
                                    "csrf_token": csrf(client)})
    logged.find("attack", "login.rate_limited")


def test_tampered_session_cookie_is_an_attack(client, logged):
    client.cookies.set("session", "admin", domain="testserver.local")
    client.get("/me", follow_redirects=False)
    logged.find("attack", "session.tampered_cookie")


def test_admin_probing(client, make, logged):
    client.get("/admin", follow_redirects=False)
    logged.find("scanner", "admin.probe_anonymous")
    make.user()
    login(client)
    client.get("/admin")
    assert "user=1:ranker@example.org" in logged.find("attack", "admin.probe_by_member")


def test_ranking_tampering_and_stale_pages(client, make, species, logged):
    _, images = species
    other = make.image(make.taxon(99, "Russula emetica"))
    gone = make.image(species[0], active=False)
    make.user()
    login(client)
    client.put("/api/taxa/48715/ranking", headers={"X-CSRF-Token": csrf(client)},
               json={"image_ids": [images[0].id, other.id, gone.id, 987654]})
    assert f"foreign_image_ids={other.id},987654" in logged.find("attack", "ranking.tampering")
    assert f"image_ids={gone.id}" in logged.find("activity", "ranking.stale_photos")


def test_malformed_api_calls_and_open_redirects(client, make, logged):
    make.user()
    login(client)
    client.put("/api/taxa/48715/ranking", headers={"X-CSRF-Token": csrf(client)}, json={"image_ids": "all"})
    logged.find("attack", "api.malformed_request")
    client.get("/login", params={"next": "https://evil.example/"})
    assert "target=https://evil.example/" in logged.find("attack", "open_redirect.attempt")


def test_normal_browsing_logs_no_threats(client, make, species, logged):
    make.user()
    login(client)
    for url in ("/", "/taxa?rank=genus&sort=photos&letter=A", "/taxon/48715/amanita-muscaria?sort=random&page=2",
                "/taxon/48715/amanita-muscaria/photos?sort=faves&source=all&page=2", "/api/search?q=fly agaric",
                "/search?q=Amanita muscaria var. guessowii", "/about", "/me", "/static/site.css?v=1"):
        client.get(url)
    assert logged.events("attack") == [] and logged.events("scanner") == []


def test_log_lines_cannot_be_forged():
    line = []
    handler_messages = []

    class Grab:
        def log(self, level, message):
            handler_messages.append(message)

    logs.event(Grab(), "search", None, q='x\nevent=login.ok user=1:admin@example.org "quoted"')
    line = handler_messages[0]
    assert "\n" not in line
    assert line.startswith('event=search q="x\\nevent=login.ok')


@pytest.mark.parametrize("text", ["Amanita muscaria", "fly agaric", "O'Brien's bolete", "1 + 1 = 2",
                                  "select good photos from the forest", "where & when"])
def test_ordinary_text_is_not_flagged(text):
    assert threats.find_payload(text) is None


@pytest.mark.parametrize("text", ["1; select password from users where id=1",
                                  "x\ny select name FROM taxa WHERE 1"])
def test_select_from_where_is_flagged(text):
    assert threats.find_payload(text)[0] == "sql_injection"


def test_select_from_where_must_share_a_line():
    assert threats.find_payload("select\nfrom\nwhere") is None


@pytest.mark.parametrize("text", ["select from " * 700, "{{" * 4000, "{%" * 4000, "${" * 4000,
                                  "#{" * 4000, "union" + " " * 8000],
                         ids=["select-from", "braces", "brace-percent", "dollar-brace", "hash-brace", "union"])
def test_hostile_input_is_scanned_quickly(text):
    # These once backtracked for seconds, stalling every request on the event loop.
    started = time.perf_counter()
    threats.find_payload(text, text + "x")
    assert time.perf_counter() - started < 0.5

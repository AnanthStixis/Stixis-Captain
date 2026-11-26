"""Emailed sign-in links for companies without SSO."""

from __future__ import annotations

from fastapi.testclient import TestClient

from captain.api.admin_http import create_admin_app
from captain.api.admin_queries import InMemoryAdminQueries
from captain.api.org import SIGNIN_LINK_TTL_S, InMemoryOrganizationStore
from captain.api.signin_mail import FileSignInMailer, mailer_from_env
from tests.unit.test_org_users import ORG_ADMIN, Env, H, StaticAuth

URL = "https://captain-admin.abc.example"


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, to: str, link: str, minutes: int) -> None:
        self.sent.append((to, link))


def setup(env: Env) -> tuple[TestClient, Outbox]:
    box = Outbox()
    env.app = create_admin_app(
        env.h.built, _auth(),
        InMemoryAdminQueries(env.h.built.infra), org_store=InMemoryOrganizationStore(),
        invites=env.invites, signin_mailer=box, console_url=URL + "/",
    )  # fmt: skip
    env.c = TestClient(env.app)
    env.c.post("/admin/v1/users", json={"email": "ravi@abc.example", "name": "Ravi",
               "roles": ["finops"]}, headers=H()).raise_for_status()  # fmt: skip
    return TestClient(env.app), box


def _auth() -> StaticAuth:
    from captain.core.admin import AdminActor

    return StaticAuth({ORG_ADMIN: AdminActor("boss@abc.example", frozenset({"org-admin"}))})


def token_of(link: str) -> str:
    assert link.startswith(URL + "/accept-invite#token=")
    return link.split("token=", 1)[1]


def test_a_user_gets_a_link_that_signs_them_in_once_with_their_own_roles() -> None:
    env = Env()
    browser, box = setup(env)
    assert browser.get("/admin/v1/auth/config").json()["email_link"] is True
    r = browser.post("/admin/v1/auth/email-link", json={"email": "Ravi@ABC.example"})
    assert r.status_code == 200 and box.sent[0][0] == "ravi@abc.example"
    token = token_of(box.sent[0][1])
    s = browser.post("/admin/v1/auth/invite/redeem", json={"token": token})
    assert s.status_code == 200 and s.json()["roles"] == ["finops"]  # not org-admin
    me = browser.get("/admin/v1/me").json()
    assert me["id"] == "ravi@abc.example" and me["roles"] == ["finops"]
    again = TestClient(env.app).post("/admin/v1/auth/invite/redeem", json={"token": token})
    assert again.status_code == 409  # single use
    assert env.events("invite.redeemed")[-1] == {"how": "email link"}


def test_unknown_or_deactivated_people_get_the_same_answer_and_no_email() -> None:
    env = Env()
    browser, box = setup(env)
    known = browser.post("/admin/v1/auth/email-link", json={"email": "ravi@abc.example"})
    unknown = browser.post("/admin/v1/auth/email-link", json={"email": "nobody@abc.example"})
    assert known.json() == unknown.json() and len(box.sent) == 1
    env.c.put("/admin/v1/users/ravi@abc.example", json={"active": False}, headers=H())
    browser.post("/admin/v1/auth/email-link", json={"email": "ravi@abc.example"})
    assert len(box.sent) == 1


def test_link_expires_and_deactivation_ends_the_session() -> None:
    env = Env()
    browser, box = setup(env)
    browser.post("/admin/v1/auth/email-link", json={"email": "ravi@abc.example"})
    env.clock.t += SIGNIN_LINK_TTL_S + 1
    r = browser.post("/admin/v1/auth/invite/redeem", json={"token": token_of(box.sent[0][1])})
    assert r.status_code == 409
    env.clock.t += SIGNIN_LINK_TTL_S  # past the rate-limit window
    browser.post("/admin/v1/auth/email-link", json={"email": "ravi@abc.example"})
    browser.post("/admin/v1/auth/invite/redeem", json={"token": token_of(box.sent[-1][1])})
    assert browser.get("/admin/v1/me").status_code == 200
    env.c.put("/admin/v1/users/ravi@abc.example", json={"active": False}, headers=H())
    assert browser.get("/admin/v1/me").status_code == 401


def test_at_most_three_links_per_person_per_15_minutes() -> None:
    env = Env()
    browser, box = setup(env)
    for _ in range(5):
        browser.post("/admin/v1/auth/email-link", json={"email": "ravi@abc.example"})
    assert len(box.sent) == 3


def test_no_email_links_when_sso_is_on_or_mail_is_not_set_up() -> None:
    env = Env()
    browser, box = setup(env)
    env.sso_on = True
    assert (
        browser.post("/admin/v1/auth/email-link", json={"email": "ravi@abc.example"}).status_code
        == 409
    )  # noqa: E501
    assert box.sent == []
    plain = Env()
    assert plain.c.get("/admin/v1/auth/config").json()["email_link"] is False
    assert (
        plain.c.post("/admin/v1/auth/email-link", json={"email": "a@b.example"}).status_code == 501
    )  # noqa: E501
    assert mailer_from_env({}) is None


def test_dev_mailer_writes_the_email_to_a_file(tmp_path) -> None:  # type: ignore[no-untyped-def]
    m = mailer_from_env({"CAPTAIN_DEV_MAIL_DIR": str(tmp_path)})
    assert isinstance(m, FileSignInMailer)
    m.send("ravi@abc.example", URL + "/accept-invite#token=abc", 15)
    (f,) = tmp_path.iterdir()
    text = f.read_text()
    assert "ravi@abc.example" in text and "token=abc" in text and "15 minutes" in text

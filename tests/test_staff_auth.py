"""Staff sign-in with email and password.

Risk: a person reaching a shop that isn't theirs; a password or session token that can be read
back from the database; passwords that can be guessed by trying; a session that outlives
signing out, a password change, or the account being disabled.
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

from riverbend.api import create_app
from riverbend.staff_auth import DEV_PASSWORD
from tests.conftest import FakeProvider, offline_output, payload

ADMIN = {"Authorization": "Bearer dev-admin-token"}
INGEST = {"X-Ingest-Key": "dev-ingest-key"}
PASSWORD = "correct horse battery"
PAT = "pat@riverbend.example"


@pytest.fixture
def client(make_services):
    # No dev accounts: each test creates the accounts it needs, as an admin would.
    s = make_services(provider=FakeProvider(default=lambda r: offline_output("m-1010#1")), dev_mode=False)
    with TestClient(create_app(s.settings, s)) as c:
        c.services = s
        yield c


def make_account(client, email=PAT, shops=("rb-lakeshore",), password=PASSWORD) -> dict:
    r = client.post(
        "/v1/admin/staff-accounts",
        headers=ADMIN,
        json={"email": email, "name": "Pat", "password": password, "shops": list(shops)},
    )
    assert r.status_code == 201, r.text
    return r.json()


def login(client, email=PAT, password=PASSWORD):
    return client.post("/v1/auth/login", json={"email": email, "password": password})


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def status_of(client, token: str, shop="rb-lakeshore") -> int:
    return client.get(f"/v1/shops/{shop}/work-orders", headers=bearer(token)).status_code


def test_signing_in_opens_the_accounts_shops_and_no_others(client):
    make_account(client, shops=["rb-lakeshore", "rb-northside"])
    r = login(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"].startswith("rbs_") and body["token_type"] == "bearer"
    assert [s["id"] for s in body["shops"]] == ["rb-lakeshore", "rb-northside"]
    token = body["token"]
    assert status_of(client, token, "rb-lakeshore") == 200
    assert status_of(client, token, "rb-northside") == 200
    assert status_of(client, token, "rb-downtown") == 404  # not 403: don't confirm the shop exists
    me = client.get("/v1/me", headers=bearer(token)).json()
    assert me["account"]["email"] == PAT and len(me["shops"]) == 2
    # The email is matched without regard to case or stray spaces.
    assert login(client, email="  PAT@Riverbend.Example ").status_code == 200


def test_wrong_password_and_unknown_email_get_the_same_answer(client):
    make_account(client)
    wrong = login(client, password="not the password")
    unknown = login(client, email="nobody@riverbend.example")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() == {"detail": "Email or password is incorrect."}


def test_repeated_wrong_passwords_lock_the_account_until_reset(client):
    make_account(client)
    other = make_account(client, email="lee@riverbend.example")
    for _ in range(5):
        assert login(client, password="guess guess guess").status_code == 401
    locked = login(client)  # even the right password, while locked
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) > 0
    assert login(client, email=other["email"]).status_code == 200  # nobody else is affected
    pat = next(
        a for a in client.get("/v1/admin/staff-accounts", headers=ADMIN).json()["items"] if a["email"] == PAT
    )
    assert pat["locked"] is True
    reset = {"password": "a brand new password"}
    r = client.patch(f"/v1/admin/staff-accounts/{pat['id']}", headers=ADMIN, json=reset)
    assert r.status_code == 200 and r.json()["locked"] is False
    assert login(client, password=reset["password"]).status_code == 200


def test_signing_out_ends_the_session_at_once(client):
    make_account(client)
    token = login(client).json()["token"]
    assert client.post("/v1/auth/logout", headers=bearer(token)).status_code == 200
    assert status_of(client, token) == 401
    assert client.post("/v1/auth/logout", headers=bearer(token)).status_code == 200  # twice is harmless


def test_an_expired_session_is_refused(client, admin):
    make_account(client)
    token = login(client).json()["token"]
    admin.execute(
        "UPDATE staff_sessions SET created_at = now() - interval '13 hours', expires_at = now() - interval '1 hour'"
    )
    assert status_of(client, token) == 401


def test_disabling_an_account_signs_it_out_everywhere(client):
    pat = make_account(client)
    first, second = login(client).json()["token"], login(client).json()["token"]
    r = client.patch(f"/v1/admin/staff-accounts/{pat['id']}", headers=ADMIN, json={"disabled": True})
    assert r.status_code == 200 and r.json()["active_sessions"] == 0
    assert status_of(client, first) == status_of(client, second) == 401
    assert login(client).json() == {"detail": "Email or password is incorrect."}
    client.patch(f"/v1/admin/staff-accounts/{pat['id']}", headers=ADMIN, json={"disabled": False})
    assert login(client).status_code == 200


def test_changing_your_password_keeps_this_session_and_ends_the_others(client):
    make_account(client)
    this, other = login(client).json()["token"], login(client).json()["token"]
    change = lambda current, new: client.post(  # noqa: E731
        "/v1/auth/password", headers=bearer(this), json={"current_password": current, "new_password": new}
    )
    assert change("wrong current", "another long password").status_code == 400
    assert change(PASSWORD, "short").status_code == 422
    assert change(PASSWORD, "another long password").status_code == 200
    assert status_of(client, this) == 200
    assert status_of(client, other) == 401
    assert login(client).status_code == 401
    assert login(client, password="another long password").status_code == 200
    # A shop's static token isn't a person, so it has no password to change.
    r = client.post(
        "/v1/auth/password",
        headers={"Authorization": "Bearer dev-lakeshore-token"},
        json={"current_password": PASSWORD, "new_password": "another long password"},
    )
    assert r.status_code == 401


def test_neither_the_password_nor_the_token_is_stored(client, admin):
    make_account(client)
    token = login(client).json()["token"]
    acct = admin.execute("SELECT * FROM staff_accounts").fetchone()
    assert acct["password_hash"].startswith("scrypt$") and PASSWORD not in acct["password_hash"]
    session = admin.execute("SELECT * FROM staff_sessions").fetchone()
    assert bytes(session["token_sha256"]) == hashlib.sha256(token.encode()).digest()
    assert token not in str(dict(session))
    listing = client.get("/v1/admin/staff-accounts", headers=ADMIN).text
    assert "scrypt" not in listing and "password" not in listing


def test_only_an_admin_manages_accounts_and_input_is_checked(client):
    make_account(client)
    token = login(client).json()["token"]
    new = {"email": "kim@riverbend.example", "password": "long enough pw", "shops": ["rb-downtown"]}

    def create(headers=ADMIN, **changes) -> int:
        return client.post("/v1/admin/staff-accounts", headers=headers, json={**new, **changes}).status_code

    def rename(account_id: str) -> int:
        return client.patch(
            f"/v1/admin/staff-accounts/{account_id}", headers=ADMIN, json={"name": "x"}
        ).status_code

    assert create(headers={}) == 401
    assert create(headers=bearer(token)) == 401  # a staff session is not an admin
    assert create(email=PAT) == 409
    assert create(shops=["rb-mars"]) == 422
    assert create(shops=[]) == 422
    assert create(password="short") == 422
    assert create(email="kim at x") == 422
    assert rename("00000000-0000-0000-0000-000000000000") == 404
    assert rename("not-a-uuid") == 404
    assert create() == 201


def test_moving_an_account_to_another_shop_applies_to_its_open_session(client):
    pat = make_account(client)
    token = login(client).json()["token"]
    client.patch(f"/v1/admin/staff-accounts/{pat['id']}", headers=ADMIN, json={"shops": ["rb-downtown"]})
    assert status_of(client, token, "rb-lakeshore") == 404
    assert status_of(client, token, "rb-downtown") == 200


def test_staff_actions_record_who_did_them(client, admin):
    make_account(client)
    token = login(client).json()["token"]
    body = "Need new chain, old one is rusty. Road bike. Wednesday."
    r = client.post("/v1/shops/rb-lakeshore/inbound-messages", headers=INGEST, json=payload("m-1010", body))
    assert r.status_code == 202
    client.services.worker().drain()
    order = client.get("/v1/shops/rb-lakeshore/work-orders", headers=bearer(token)).json()["work_orders"][0]
    status = lambda headers, value: client.post(  # noqa: E731
        f"/v1/shops/rb-lakeshore/work-orders/{order['id']}/status", headers=headers, json={"status": value}
    )
    assert status(bearer(token), "completed").status_code == 200
    assert status({"Authorization": "Bearer dev-lakeshore-token"}, "open").status_code == 200
    rows = admin.execute("SELECT detail FROM events WHERE type = 'work_order_status' ORDER BY id").fetchall()
    assert [r["detail"]["by"] for r in rows] == [PAT, "rb-lakeshore staff token"]


def test_dev_mode_creates_the_demo_accounts(make_services):
    s = make_services(dev_mode=True)
    with TestClient(create_app(s.settings, s)) as c:
        manager = login(c, email="manager@riverbend.test", password=DEV_PASSWORD)
        downtown = login(c, email="downtown@riverbend.test", password=DEV_PASSWORD)
    assert len(manager.json()["shops"]) == 3
    assert [x["id"] for x in downtown.json()["shops"]] == ["rb-downtown"]

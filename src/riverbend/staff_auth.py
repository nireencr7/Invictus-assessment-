"""Staff sign-in: email and password in, a personal session token out.

The session token is used exactly like a shop's staff token (`Authorization: Bearer …`), so
nothing downstream changes. It differs in what matters for people:
- it belongs to one person, so it can be revoked without touching anyone else's;
- it expires (`staff_session_hours`), and signing out revokes it at once;
- one account may open several shops (a manager), each still checked per request.

Stored: a scrypt hash of each password and a SHA-256 of each token, never either value.
Wrong password and unknown email get the same answer in about the same time. After
`login_max_failures` wrong passwords in a row the account locks for `login_lockout_minutes`;
the count lives in the database, so it holds across API instances.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime

from riverbend.db import Database

TOKEN_PREFIX = "rbs_"
EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
MIN_PASSWORD = 10

# scrypt at N=2^15, r=8: about 32 MiB and tens of milliseconds per hash. Slow enough to make a
# stolen hash expensive to crack, cheap enough for a 512 MiB Cloud Run instance.
_N, _R, _P = 2**15, 8, 1
_MAXMEM = 64 * 1024 * 1024


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32)
    return f"scrypt${_N}${_R}${_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, key = stored.split("$")
        if algo != "scrypt":
            return False
        expected = base64.b64decode(key)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            maxmem=_MAXMEM,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Checked when the email is unknown, so that case takes as long as a wrong password.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    name: str | None
    shops: tuple[str, ...]


@dataclass(frozen=True)
class SignedIn:
    token: str
    expires_at: datetime
    account: Account


class LoginFailed(Exception):
    """Wrong email or password, or a disabled account. One message for all three."""


class LoginLocked(Exception):
    def __init__(self, retry_after_seconds: int):
        super().__init__("too many failed sign-in attempts")
        self.retry_after_seconds = retry_after_seconds


def _account(row: dict) -> Account:
    return Account(str(row["id"]), row["email"], row["display_name"], tuple(row["shops"]))


class StaffAuth:
    def __init__(self, db: Database, session_hours: float, max_failures: int, lockout_minutes: float):
        self.db = db
        self.session_hours = session_hours
        self.max_failures = max_failures
        self.lockout_minutes = lockout_minutes

    # ------------------------------------------------------------------ sign in / out
    def login(self, email: str, password: str) -> SignedIn:
        email = normalize_email(email)
        outcome: SignedIn | Exception
        with self.db.system_tx() as conn:
            row = conn.execute(
                "SELECT *, now() AS now FROM staff_accounts WHERE email = %s FOR UPDATE", (email,)
            ).fetchone()
            if row is None:
                verify_password(password, _DUMMY_HASH)
                raise LoginFailed()
            if row["locked_until"] and row["locked_until"] > row["now"]:
                raise LoginLocked(int((row["locked_until"] - row["now"]).total_seconds()) + 1)
            ok = verify_password(password, row["password_hash"])
            if row["disabled"]:
                raise LoginFailed()
            if not ok:
                # Record the failure before raising: raising inside the block would roll it back.
                failures = row["failed_logins"] + 1
                if failures >= self.max_failures:
                    conn.execute(
                        "UPDATE staff_accounts SET failed_logins = 0, "
                        "locked_until = now() + make_interval(secs => %s) WHERE id = %s",
                        (self.lockout_minutes * 60, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE staff_accounts SET failed_logins = %s WHERE id = %s", (failures, row["id"])
                    )
                outcome = LoginFailed()
            else:
                token = TOKEN_PREFIX + secrets.token_urlsafe(32)
                expires = conn.execute(
                    """INSERT INTO staff_sessions (token_sha256, account_id, expires_at)
                       VALUES (%s, %s, now() + make_interval(secs => %s)) RETURNING expires_at""",
                    (_digest(token), row["id"], self.session_hours * 3600),
                ).fetchone()["expires_at"]
                conn.execute(
                    "UPDATE staff_accounts SET failed_logins = 0, locked_until = NULL, last_login_at = now() "
                    "WHERE id = %s",
                    (row["id"],),
                )
                outcome = SignedIn(token, expires, _account(row))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def account_for(self, token: str) -> tuple[Account, bytes] | None:
        """The account behind a live session token, or None (unknown, expired, revoked, disabled)."""
        if not token.startswith(TOKEN_PREFIX):
            return None
        digest = _digest(token)
        with self.db.system_tx() as conn:
            row = conn.execute(
                """SELECT a.* FROM staff_sessions s JOIN staff_accounts a ON a.id = s.account_id
                   WHERE s.token_sha256 = %s AND s.revoked_at IS NULL AND s.expires_at > now()
                     AND NOT a.disabled""",
                (digest,),
            ).fetchone()
        return (_account(row), digest) if row else None

    def logout(self, token: str) -> bool:
        with self.db.system_tx() as conn:
            cur = conn.execute(
                "UPDATE staff_sessions SET revoked_at = now() WHERE token_sha256 = %s AND revoked_at IS NULL",
                (_digest(token),),
            )
            return cur.rowcount > 0

    def change_password(self, token: str, current: str, new: str) -> bool:
        """True if `current` was right. Every other session of the account is signed out."""
        found = self.account_for(token)
        if found is None:
            return False
        account, digest = found
        with self.db.system_tx() as conn:
            row = conn.execute(
                "SELECT password_hash FROM staff_accounts WHERE id = %s FOR UPDATE", (account.id,)
            ).fetchone()
            if not verify_password(current, row["password_hash"]):
                return False
            conn.execute(
                "UPDATE staff_accounts SET password_hash = %s, password_changed_at = now() WHERE id = %s",
                (hash_password(new), account.id),
            )
            conn.execute(
                "UPDATE staff_sessions SET revoked_at = now() "
                "WHERE account_id = %s AND token_sha256 <> %s AND revoked_at IS NULL",
                (account.id, digest),
            )
        return True

    # ------------------------------------------------------------------ admin
    def list_accounts(self) -> list[dict]:
        with self.db.system_tx() as conn:
            rows = conn.execute(
                """SELECT a.id, a.email, a.display_name, a.shops, a.disabled, a.last_login_at, a.created_at,
                          a.locked_until > now() AS locked,
                          (SELECT count(*) FROM staff_sessions s WHERE s.account_id = a.id
                             AND s.revoked_at IS NULL AND s.expires_at > now()) AS active_sessions
                   FROM staff_accounts a ORDER BY a.email"""
            ).fetchall()
        return [_admin_json(r) for r in rows]

    def create_account(self, email: str, name: str | None, password: str, shops: list[str]) -> dict | None:
        """The new account, or None if the email is taken."""
        with self.db.system_tx() as conn:
            row = conn.execute(
                """INSERT INTO staff_accounts (email, display_name, password_hash, shops)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING
                   RETURNING id, email, display_name, shops, disabled, last_login_at, created_at,
                             false AS locked, 0 AS active_sessions""",
                (normalize_email(email), name, hash_password(password), shops),
            ).fetchone()
        return _admin_json(row) if row else None

    def update_account(
        self,
        account_id: str,
        *,
        name: str | None = None,
        password: str | None = None,
        shops: list[str] | None = None,
        disabled: bool | None = None,
    ) -> dict | None:
        """Changes what's given. A new password or disabling signs the person out everywhere."""
        with self.db.system_tx() as conn:
            exists = conn.execute(
                "SELECT 1 FROM staff_accounts WHERE id = %s FOR UPDATE", (account_id,)
            ).fetchone()
            if not exists:
                return None
            if name is not None:
                conn.execute("UPDATE staff_accounts SET display_name = %s WHERE id = %s", (name, account_id))
            if shops is not None:
                conn.execute("UPDATE staff_accounts SET shops = %s WHERE id = %s", (shops, account_id))
            if password is not None:
                conn.execute(
                    "UPDATE staff_accounts SET password_hash = %s, password_changed_at = now(), "
                    "failed_logins = 0, locked_until = NULL WHERE id = %s",
                    (hash_password(password), account_id),
                )
            if disabled is not None:
                conn.execute("UPDATE staff_accounts SET disabled = %s WHERE id = %s", (disabled, account_id))
            if password is not None or disabled:
                conn.execute(
                    "UPDATE staff_sessions SET revoked_at = now() WHERE account_id = %s AND revoked_at IS NULL",
                    (account_id,),
                )
        return next((a for a in self.list_accounts() if a["id"] == account_id), None)


# Local development only (DEV_MODE=true), like the dev tokens: obvious values, never on GCP.
DEV_PASSWORD = "riverbend-dev"
DEV_ACCOUNTS = [
    {"email": "manager@riverbend.test", "name": "Sam (manager)", "shops": None},  # None = every shop
    {"email": "downtown@riverbend.test", "name": "Alex (Downtown)", "shops": ["rb-downtown"]},
]
_dev_hash: str | None = None


def seed_dev_accounts(db: Database, all_shops: list[str]) -> None:
    """Creates the dev accounts if they're missing. Never changes an existing account."""
    global _dev_hash
    with db.system_tx() as conn:
        present = {
            r["email"]
            for r in conn.execute(
                "SELECT email FROM staff_accounts WHERE email = ANY(%s)",
                ([a["email"] for a in DEV_ACCOUNTS],),
            ).fetchall()
        }
        for a in DEV_ACCOUNTS:
            if a["email"] in present:
                continue
            _dev_hash = _dev_hash or hash_password(DEV_PASSWORD)
            conn.execute(
                """INSERT INTO staff_accounts (email, display_name, password_hash, shops)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (email) DO NOTHING""",
                (a["email"], a["name"], _dev_hash, a["shops"] or all_shops),
            )


def _admin_json(r: dict) -> dict:
    return {
        "id": str(r["id"]),
        "email": r["email"],
        "name": r["display_name"],
        "shops": list(r["shops"]),
        "disabled": r["disabled"],
        "locked": bool(r["locked"]),
        "active_sessions": r["active_sessions"],
        "last_login_at": r["last_login_at"],
        "created_at": r["created_at"],
    }


def is_uuid(value: str) -> bool:
    return bool(
        re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value)
    )

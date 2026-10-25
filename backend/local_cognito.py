"""A small stand-in for the Cognito user pool API, saved in a SQLite file.

Local development only (used by local_dev.py). It answers the calls the
website makes, with Cognito's request/response shapes and error names, so the
site's sign-up, sign-in and password-reset pages work unchanged. Accounts
survive restarts. Passwords are stored as salted PBKDF2 hashes. Confirmation
and reset codes are printed to the server's terminal instead of emailed.

Not a security boundary: tokens are signed with a local HS256 secret stored
next to the accounts, which is fine on your own computer and nowhere else.
"""

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid

PBKDF2_ROUNDS = 200_000
TOKEN_SECONDS = 3600
REFRESH_SECONDS = 30 * 24 * 3600
CONFIRM_CODE_SECONDS = 24 * 3600
RESET_CODE_SECONDS = 3600
MAX_FAILED_SIGN_INS = 5
LOCKOUT_SECONDS = 5 * 60

# Mirrors the user pool's password policy in infra/recipe_stack.py.
PASSWORD_RULES = [
    (lambda p: len(p) >= 10, "Password must have length greater than or equal to 10"),
    (lambda p: re.search(r"[a-z]", p), "Password must have lowercase characters"),
    (lambda p: re.search(r"[A-Z]", p), "Password must have uppercase characters"),
    (lambda p: re.search(r"[0-9]", p), "Password must have numeric characters"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY,
    sub TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    confirm_code TEXT, confirm_expires REAL,
    reset_code TEXT, reset_expires REAL,
    failed_sign_ins INTEGER NOT NULL DEFAULT 0, last_failed REAL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    email TEXT NOT NULL REFERENCES users(email) ON DELETE CASCADE,
    expires REAL NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""


class CognitoError(Exception):
    """Carries a Cognito error name (e.g. NotAuthorizedException) and message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${_b64(salt)}${_b64(digest)}"


def _check_password(password, stored):
    _, rounds, salt, digest = stored.split("$")
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), _unb64(salt), int(rounds))
    return hmac.compare_digest(candidate, _unb64(digest))


def _mask(email):
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain[:1]}***"


def _delivery(email):
    return {"Destination": _mask(email), "DeliveryMedium": "EMAIL", "AttributeName": "email"}


def _required(body, *names):
    missing = [n for n in names if not isinstance(body.get(n), str) or not body[n]]
    if missing:
        raise CognitoError("InvalidParameterException", f"Missing required parameter {missing[0]}")


class LocalUserPool:
    ACTIONS = {
        "SignUp": "sign_up",
        "ConfirmSignUp": "confirm_sign_up",
        "ResendConfirmationCode": "resend_confirmation_code",
        "InitiateAuth": "initiate_auth",
        "ForgotPassword": "forgot_password",
        "ConfirmForgotPassword": "confirm_forgot_password",
        "RevokeToken": "revoke_token",
    }

    def __init__(self, path, region="us-east-2", announce=print):
        self.region = region
        self.announce = announce  # where codes go instead of email
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self.pool_id = self._meta("pool_id", lambda: f"{region}_local{secrets.token_hex(4)}")
        self.client_id = self._meta("client_id", lambda: secrets.token_hex(13))
        self.secret = _unb64(self._meta("signing_secret", lambda: _b64(secrets.token_bytes(32))))
        self.issuer = f"https://cognito-idp.{region}.amazonaws.com/{self.pool_id}"

    def _meta(self, key, make):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row:
            return row["value"]
        value = make()
        self.db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))
        return value

    # ---------- entry point used by the /local-cognito proxy ----------

    def handle(self, action, body):
        method = self.ACTIONS.get(action)
        if not method:
            raise CognitoError("InvalidAction", f"{action or 'Missing action'} isn't supported by the local user pool.")
        if not isinstance(body, dict) or body.get("ClientId") != self.client_id:
            raise CognitoError("ResourceNotFoundException", "User pool client does not exist.")
        with self.lock:
            return getattr(self, method)(body)

    # ---------- helpers ----------

    def _user(self, username):
        email = (username or "").strip().lower()
        return self.db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    def _check_policy(self, password):
        for rule, message in PASSWORD_RULES:
            if not rule(password):
                raise CognitoError("InvalidPasswordException", f"Password did not conform with policy: {message}")

    def _new_code(self, email, kind):
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.announce(f"[local-cognito] {kind} code for {email}: {code}")
        return code

    def _sign(self, claims):
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "local"}).encode())
        payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
        signature = hmac.new(self.secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
        return f"{header}.{payload}.{_b64(signature)}"

    def _tokens(self, user, with_refresh):
        now = int(time.time())
        common = {"sub": user["sub"], "iss": self.issuer, "iat": now, "auth_time": now,
                  "exp": now + TOKEN_SECONDS, "jti": str(uuid.uuid4())}
        result = {
            "IdToken": self._sign({**common, "aud": self.client_id, "token_use": "id", "email": user["email"],
                                   "email_verified": True, "name": user["name"],
                                   "cognito:username": user["sub"]}),
            "AccessToken": self._sign({**common, "client_id": self.client_id, "token_use": "access",
                                       "scope": "aws.cognito.signin.user.admin", "username": user["sub"]}),
            "ExpiresIn": TOKEN_SECONDS,
            "TokenType": "Bearer",
        }
        if with_refresh:
            refresh = secrets.token_urlsafe(48)
            self.db.execute("INSERT INTO refresh_tokens (token_hash, email, expires) VALUES (?, ?, ?)",
                            (hashlib.sha256(refresh.encode()).hexdigest(), user["email"], now + REFRESH_SECONDS))
            result["RefreshToken"] = refresh
        return {"AuthenticationResult": result, "ChallengeParameters": {}}

    # ---------- Cognito actions ----------

    def sign_up(self, body):
        _required(body, "Username", "Password")
        email = body["Username"].strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise CognitoError("InvalidParameterException", "Invalid email address format.")
        self._check_policy(body["Password"])
        if self._user(email):
            raise CognitoError("UsernameExistsException", "An account with the given email already exists.")
        attributes = {a.get("Name"): a.get("Value", "") for a in body.get("UserAttributes") or []}
        sub = str(uuid.uuid4())
        self.db.execute(
            "INSERT INTO users (email, sub, name, password_hash, confirm_code, confirm_expires, created) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, sub, str(attributes.get("name", ""))[:40], _hash_password(body["Password"]),
             self._new_code(email, "Confirmation"), time.time() + CONFIRM_CODE_SECONDS, time.time()))
        return {"UserConfirmed": False, "UserSub": sub, "CodeDeliveryDetails": _delivery(email)}

    def confirm_sign_up(self, body):
        _required(body, "Username", "ConfirmationCode")
        user = self._user(body["Username"])
        if not user or not user["confirm_code"]:
            if user and user["confirmed"]:
                raise CognitoError("NotAuthorizedException", "User cannot be confirmed. Current status is CONFIRMED")
            raise CognitoError("CodeMismatchException", "Invalid verification code provided, please try again.")
        if not hmac.compare_digest(user["confirm_code"], body["ConfirmationCode"].strip()):
            raise CognitoError("CodeMismatchException", "Invalid verification code provided, please try again.")
        if user["confirm_expires"] < time.time():
            raise CognitoError("ExpiredCodeException", "Invalid code provided, please request a code again.")
        self.db.execute("UPDATE users SET confirmed = 1, confirm_code = NULL, confirm_expires = NULL "
                        "WHERE email = ?", (user["email"],))
        return {}

    def resend_confirmation_code(self, body):
        _required(body, "Username")
        user = self._user(body["Username"])
        if not user:
            return {"CodeDeliveryDetails": _delivery(body["Username"].strip().lower())}  # don't reveal accounts
        if user["confirmed"]:
            raise CognitoError("InvalidParameterException", "User is already confirmed.")
        self.db.execute("UPDATE users SET confirm_code = ?, confirm_expires = ? WHERE email = ?",
                        (self._new_code(user["email"], "Confirmation"), time.time() + CONFIRM_CODE_SECONDS,
                         user["email"]))
        return {"CodeDeliveryDetails": _delivery(user["email"])}

    def initiate_auth(self, body):
        flow = body.get("AuthFlow")
        params = body.get("AuthParameters") or {}
        if flow == "REFRESH_TOKEN_AUTH":
            token_hash = hashlib.sha256(str(params.get("REFRESH_TOKEN", "")).encode()).hexdigest()
            row = self.db.execute("SELECT * FROM refresh_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
            if not row or row["revoked"] or row["expires"] < time.time():
                raise CognitoError("NotAuthorizedException", "Invalid Refresh Token")
            return self._tokens(self._user(row["email"]), with_refresh=False)
        if flow != "USER_PASSWORD_AUTH":
            raise CognitoError("InvalidParameterException", f"{flow} isn't supported by the local user pool.")

        user = self._user(params.get("USERNAME"))
        if not user:
            raise CognitoError("NotAuthorizedException", "Incorrect username or password.")
        if user["failed_sign_ins"] >= MAX_FAILED_SIGN_INS and time.time() - user["last_failed"] < LOCKOUT_SECONDS:
            raise CognitoError("NotAuthorizedException", "Password attempts exceeded")
        if not _check_password(str(params.get("PASSWORD", "")), user["password_hash"]):
            self.db.execute("UPDATE users SET failed_sign_ins = failed_sign_ins + 1, last_failed = ? WHERE email = ?",
                            (time.time(), user["email"]))
            raise CognitoError("NotAuthorizedException", "Incorrect username or password.")
        self.db.execute("UPDATE users SET failed_sign_ins = 0, last_failed = NULL WHERE email = ?", (user["email"],))
        if not user["confirmed"]:
            raise CognitoError("UserNotConfirmedException", "User is not confirmed.")
        return self._tokens(user, with_refresh=True)

    def forgot_password(self, body):
        _required(body, "Username")
        user = self._user(body["Username"])
        if not user:
            return {"CodeDeliveryDetails": _delivery(body["Username"].strip().lower())}  # don't reveal accounts
        self.db.execute("UPDATE users SET reset_code = ?, reset_expires = ? WHERE email = ?",
                        (self._new_code(user["email"], "Password reset"), time.time() + RESET_CODE_SECONDS,
                         user["email"]))
        return {"CodeDeliveryDetails": _delivery(user["email"])}

    def confirm_forgot_password(self, body):
        _required(body, "Username", "ConfirmationCode", "Password")
        user = self._user(body["Username"])
        if not user or not user["reset_code"] or not hmac.compare_digest(user["reset_code"],
                                                                         body["ConfirmationCode"].strip()):
            raise CognitoError("CodeMismatchException", "Invalid verification code provided, please try again.")
        if user["reset_expires"] < time.time():
            raise CognitoError("ExpiredCodeException", "Invalid code provided, please request a code again.")
        self._check_policy(body["Password"])
        self.db.execute("UPDATE users SET password_hash = ?, reset_code = NULL, reset_expires = NULL, "
                        "failed_sign_ins = 0, confirmed = 1 WHERE email = ?",
                        (_hash_password(body["Password"]), user["email"]))
        return {}

    def revoke_token(self, body):
        _required(body, "Token")
        token_hash = hashlib.sha256(body["Token"].encode()).hexdigest()
        self.db.execute("UPDATE refresh_tokens SET revoked = 1 WHERE token_hash = ?", (token_hash,))
        return {}

    # ---------- used by the local API server and seeding ----------

    def verify_id_token(self, token):
        """Returns the claims of a valid, unexpired ID token from this pool, else None."""
        try:
            header, payload, signature = token.split(".")
            if json.loads(_unb64(header)).get("alg") != "HS256":
                return None
            expected = hmac.new(self.secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
            if not hmac.compare_digest(expected, _unb64(signature)):
                return None
            claims = json.loads(_unb64(payload))
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        if (claims.get("iss") != self.issuer or claims.get("aud") != self.client_id
                or claims.get("token_use") != "id" or claims.get("exp", 0) < time.time()):
            return None
        return claims

    def admin_create_confirmed_user(self, email, name, password):
        with self.lock:
            self.db.execute(
                "INSERT INTO users (email, sub, name, password_hash, confirmed, created) VALUES (?, ?, ?, ?, 1, ?)",
                (email.lower(), str(uuid.uuid4()), name, _hash_password(password), time.time()))

    def user_count(self):
        return self.db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def close(self):
        self.db.close()

"""The local dev server: SQLite accounts, saved recipes, and restarts."""

import os
import re
import sqlite3
import sys
import warnings

from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import devtools  # noqa: E402

devtools.use_fake_aws("recipes-local")
import local_cognito  # noqa: E402
import local_dev  # noqa: E402

with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient  # noqa: E402


class LocalServer:
    """One run of the local dev server against a data folder; codes are captured instead of printed."""

    def __init__(self, data_dir, seed=True):
        self.codes = []
        self.mock = mock_aws()
        self.mock.start()
        app, self.pool = local_dev.build_local_app(str(data_dir), seed_first_run=seed, announce=self.codes.append)
        self.client = TestClient(app)
        self.client_id = self.client.get("/config.json").json()["clientId"]

    def stop(self):
        self.pool.close()
        self.mock.stop()

    def cognito(self, action, body):
        return self.client.post("/local-cognito", json={"ClientId": self.client_id, **body},
                                headers={"X-Amz-Target": f"AWSCognitoIdentityProviderService.{action}"})

    def sign_in(self, email, password):
        return self.cognito("InitiateAuth", {"AuthFlow": "USER_PASSWORD_AUTH",
                                             "AuthParameters": {"USERNAME": email, "PASSWORD": password}})

    def token(self, email, password):
        return self.sign_in(email, password).json()["AuthenticationResult"]["IdToken"]

    def last_code(self):
        return re.search(r"(\d{6})$", self.codes[-1]).group(1)

    def api(self, method, path, token, **kwargs):
        return self.client.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)


def test_sign_up_with_emailed_code_then_use_the_api(tmp_path):
    server = LocalServer(tmp_path)
    try:
        assert server.cognito("SignUp", {"Username": "dana@example.com", "Password": "short"}).json()["__type"] == \
            "InvalidPasswordException"
        signup = server.cognito("SignUp", {"Username": "Dana@Example.com", "Password": "Recipes4Dana",
                                           "UserAttributes": [{"Name": "email", "Value": "dana@example.com"},
                                                              {"Name": "name", "Value": "Dana"}]})
        assert signup.status_code == 200 and signup.json()["UserConfirmed"] is False
        assert server.cognito("SignUp", {"Username": "dana@example.com", "Password": "Recipes4Dana"}).json()[
            "__type"] == "UsernameExistsException"
        assert server.sign_in("dana@example.com", "Recipes4Dana").json()["__type"] == "UserNotConfirmedException"

        # Codes are real: a wrong one fails, the printed one works.
        assert server.cognito("ConfirmSignUp", {"Username": "dana@example.com", "ConfirmationCode": "000000"}).json()[
            "__type"] == "CodeMismatchException"
        assert server.cognito("ConfirmSignUp", {"Username": "dana@example.com",
                                                "ConfirmationCode": server.last_code()}).status_code == 200

        token = server.token("dana@example.com", "Recipes4Dana")
        me = server.api("GET", "/me", token).json()
        assert me["email"] == "dana@example.com" and me["displayName"] == "Dana"

        tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
        assert server.api("GET", "/me", tampered).status_code == 401
        assert server.cognito("AdminDeleteUser", {"Username": "dana@example.com"}).json()["__type"] == "InvalidAction"
    finally:
        server.stop()


def test_accounts_and_recipes_survive_a_restart(tmp_path):
    first = LocalServer(tmp_path)
    try:
        alice = first.token("alice@example.com", "Recipes4Alice")
        alice_id = first.api("GET", "/me", alice).json()["userId"]
        recipe = first.api("POST", "/recipes", alice, json={
            "title": "Cranberry Sauce", "ingredients": ["cranberries", "sugar"], "steps": ["simmer"]}).json()
        first.api("PUT", "/me/preferences", alice, json={"season": "winter", "mode": "dark"})
        first.cognito("SignUp", {"Username": "erin@example.com", "Password": "Recipes4Erin"})
        first.cognito("ConfirmSignUp", {"Username": "erin@example.com", "ConfirmationCode": first.last_code()})
    finally:
        first.stop()

    second = LocalServer(tmp_path)  # a fresh in-memory DynamoDB, loaded from the saved files
    try:
        # Tokens from before the restart still work, and the account keeps its id.
        assert second.api("GET", "/me", alice).json()["userId"] == alice_id
        alice_again = second.token("alice@example.com", "Recipes4Alice")
        me = second.api("GET", "/me", alice_again).json()
        assert me["userId"] == alice_id and me["preferences"]["season"] == "winter"
        assert second.api("GET", f"/recipes/{recipe['recipeId']}", alice_again).json()["authorName"] == "Alice"
        feed = second.api("GET", "/recipes", alice_again).json()
        # Her pie (shared with both her families, listed once) and the Riveras' pozole;
        # sample data was not re-seeded.
        assert len(feed["family"]) == 2 and len(feed["public"]) == 3
        pie = next(r for r in feed["family"] if r["title"] == "Grandma's Sweet Potato Pie")
        assert len(pie["familyIds"]) == 2
        # Her sample draft is in neither feed, only in her own list.
        mine = second.api("GET", "/me/recipes", alice_again).json()["recipes"]
        assert [r["title"] for r in mine if r["visibility"] == "draft"] == ["Cornbread Dressing"]
        assert "Cornbread Dressing" not in [r["title"] for r in feed["family"] + feed["public"]]
        assert second.sign_in("erin@example.com", "Recipes4Erin").status_code == 200
    finally:
        second.stop()


def test_password_reset_and_lockout(tmp_path):
    server = LocalServer(tmp_path)
    try:
        for _ in range(local_cognito.MAX_FAILED_SIGN_INS):
            assert server.sign_in("bob@example.com", "WrongPassword1").json()["message"] == \
                "Incorrect username or password."
        assert server.sign_in("bob@example.com", "Recipes4Bobby").json()["message"] == "Password attempts exceeded"

        # Unknown accounts get the same answer as real ones, so the form doesn't reveal who has an account.
        assert server.cognito("ForgotPassword", {"Username": "nobody@example.com"}).status_code == 200
        assert server.cognito("ForgotPassword", {"Username": "bob@example.com"}).status_code == 200
        assert server.cognito("ConfirmForgotPassword", {"Username": "bob@example.com", "ConfirmationCode":
                                                        server.last_code(), "Password": "NewRecipes4Bob"}).status_code == 200
        assert server.sign_in("bob@example.com", "NewRecipes4Bob").status_code == 200  # reset also clears the lockout
    finally:
        server.stop()


def test_passwords_are_hashed_and_refresh_tokens_can_be_revoked(tmp_path):
    server = LocalServer(tmp_path)
    try:
        auth = server.sign_in("carol@example.com", "Recipes4Carol").json()["AuthenticationResult"]
        refresh = {"AuthFlow": "REFRESH_TOKEN_AUTH", "AuthParameters": {"REFRESH_TOKEN": auth["RefreshToken"]}}
        assert server.cognito("InitiateAuth", refresh).status_code == 200
        assert server.cognito("RevokeToken", {"Token": auth["RefreshToken"]}).status_code == 200
        assert server.cognito("InitiateAuth", refresh).json()["__type"] == "NotAuthorizedException"
    finally:
        server.stop()

    db = sqlite3.connect(tmp_path / "accounts.sqlite3")
    stored = [row[0] for row in db.execute("SELECT password_hash FROM users")]
    tokens = [row[0] for row in db.execute("SELECT token_hash FROM refresh_tokens")]
    db.close()
    assert stored and all(h.startswith("pbkdf2_sha256$") and "Recipes4" not in h for h in stored)
    assert auth["RefreshToken"] not in tokens

"""Run the whole app on your computer, with accounts and recipes saved between restarts.

    infra\\.venv\\Scripts\\python.exe backend\\local_dev.py            # start (or resume)
    infra\\.venv\\Scripts\\python.exe backend\\local_dev.py --reset    # wipe local data first
    infra\\.venv\\Scripts\\python.exe backend\\local_dev.py --empty    # first run without sample data

Then open http://127.0.0.1:8001/ for the website or /docs for the API. Nothing
touches AWS. Local data lives in backend/.localdata/:

  accounts.sqlite3  accounts (local_cognito.py; passwords are hashed)
  recipes.json      recipes, comments, families and preferences

Recipes run on moto's in-memory DynamoDB, which is saved to recipes.json after
every successful change and loaded back on startup. On the very first run, the
test accounts in TEST_ACCOUNTS and some sample recipes are created.

No email is sent: sign-up and password-reset codes are printed in this
terminal. In /docs or curl, an `X-Dev-User: someone` header acts as that
user without signing in.
"""

import argparse
import importlib
import json
import os
import shutil
import sys
import warnings

import boto3
import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from moto import mock_aws
from starlette.concurrency import run_in_threadpool

import devtools
from local_cognito import CognitoError, LocalUserPool

TABLE_NAME = "recipes-local"
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(BACKEND_DIR, ".localdata")
FRONTEND_DIR = os.path.join(BACKEND_DIR, "..", "frontend")

# Local-only test accounts, created on the first run: (email, display name, password).
TEST_ACCOUNTS = [
    ("alice@example.com", "Alice", "Recipes4Alice"),
    ("bob@example.com", "Bob", "Recipes4Bobby"),
    ("carol@example.com", "Carol", "Recipes4Carol"),
]

SAMPLE_RECIPES = [
    ("bob@example.com", {
        "title": "Brown Butter Pumpkin Bread",
        "description": "Moist, warmly spiced and nutty from browned butter. Makes one loaf.",
        "ingredients": ["1/2 cup butter", "1 3/4 cups flour", "1 cup pumpkin purée", "3/4 cup brown sugar",
                        "2 eggs", "2 tsp pumpkin pie spice", "1 tsp baking soda", "1/2 tsp salt"],
        "steps": ["Brown the butter in a small pan until nutty; cool 10 minutes.",
                  "Whisk the flour, spice, baking soda and salt.",
                  "Beat the butter, sugar, eggs and pumpkin, then fold in the dry ingredients.",
                  "Bake in a lined loaf pan at 350°F for 55–60 minutes."],
    }),
    ("alice@example.com", {
        "title": "Apple Cider Glazed Chicken",
        "description": "A weeknight skillet dinner with a sticky, tangy cider glaze.",
        "ingredients": ["4 chicken thighs", "1 cup apple cider", "2 tbsp Dijon mustard", "1 tbsp maple syrup",
                        "2 sprigs thyme"],
        "steps": ["Sear the chicken skin-side down until golden.",
                  "Add the cider, mustard, maple and thyme; simmer until thick and glossy.",
                  "Spoon the glaze over the chicken and serve."],
    }),
    ("alice@example.com", {
        "title": "Grandma's Sweet Potato Pie",
        "description": "The family Thanksgiving pie. Shared with family only.",
        "ingredients": ["2 cups mashed sweet potato", "1 cup evaporated milk", "3/4 cup sugar", "2 eggs",
                        "1 tsp vanilla", "1 unbaked pie crust"],
        "steps": ["Blend everything except the crust until smooth.",
                  "Pour into the crust and bake at 350°F for 50 minutes, until just set."],
        "visibility": "family",
    }),
]


# ---------- recipe data snapshot ----------

def save_snapshot(path):
    """Writes every item except rate-limit counters to `path` (DynamoDB JSON), atomically."""
    client = boto3.client("dynamodb")
    items = []
    for page in client.get_paginator("scan").paginate(TableName=TABLE_NAME):
        items += [item for item in page["Items"] if not item["pk"]["S"].startswith("RATE#")]
    temp_path = f"{path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "table": TABLE_NAME, "items": items}, handle, ensure_ascii=False)
    os.replace(temp_path, path)  # never leaves a half-written file behind


def load_snapshot(path):
    """Loads a snapshot written by save_snapshot. Returns how many items were restored."""
    if not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as handle:
        items = json.load(handle)["items"]
    client = boto3.client("dynamodb")
    for start in range(0, len(items), 25):  # BatchWriteItem takes up to 25 items
        request = {TABLE_NAME: [{"PutRequest": {"Item": item}} for item in items[start:start + 25]]}
        while request:
            request = client.batch_write_item(RequestItems=request).get("UnprocessedItems") or None
    return len(items)


# ---------- first-run sample data ----------

def seed(app, pool):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    as_user = {}
    for email, name, password in TEST_ACCOUNTS:
        pool.admin_create_confirmed_user(email, name, password)
        auth = pool.handle("InitiateAuth", {"ClientId": pool.client_id, "AuthFlow": "USER_PASSWORD_AUTH",
                                            "AuthParameters": {"USERNAME": email, "PASSWORD": password}})
        as_user[email] = {"Authorization": f"Bearer {auth['AuthenticationResult']['IdToken']}"}
    client = TestClient(app)
    for email, _, _ in TEST_ACCOUNTS:
        client.get("/me", headers=as_user[email])  # creates each profile from the token's name
    family = client.post("/families", json={"name": "The Washingtons"}, headers=as_user["alice@example.com"]).json()
    client.post("/families/join", json={"inviteCode": family["inviteCode"]}, headers=as_user["bob@example.com"])
    recipe_ids = {}
    for email, recipe in SAMPLE_RECIPES:
        created = client.post("/recipes", json=recipe, headers=as_user[email]).json()
        recipe_ids[recipe["title"]] = created["recipeId"]
        if email == "bob@example.com":
            client.post(f"/recipes/{created['recipeId']}/comments", headers=as_user["alice@example.com"],
                        json={"text": "Made this twice already. The brown butter is everything."})
            client.post(f"/recipes/{created['recipeId']}/comments", headers=as_user["carol@example.com"],
                        json={"text": "Can I swap the eggs for flax?"})
    # A second family, so Alice is in two (accounts can belong to several).
    riveras = client.post("/families", json={"name": "The Rivera Kitchen"}, headers=as_user["carol@example.com"]).json()
    client.post("/families/join", json={"inviteCode": riveras["inviteCode"]}, headers=as_user["alice@example.com"])
    # Alice shares her pie with both of her families (one recipe can go to several).
    pie_id = recipe_ids["Grandma's Sweet Potato Pie"]
    client.put(f"/recipes/{pie_id}/sharing", headers=as_user["alice@example.com"],
               json={"visibility": "family", "familyIds": [family["familyId"], riveras["familyId"]]})
    client.post("/recipes", headers=as_user["carol@example.com"], json={
        "title": "Abuela's Pozole Rojo",
        "description": "Sunday pozole for the whole family.",
        "ingredients": ["2 lb pork shoulder", "1 can hominy", "4 guajillo chiles", "1 onion", "4 garlic cloves"],
        "steps": ["Simmer the pork with onion and garlic until tender.",
                  "Blend the soaked chiles and stir them in with the hominy.",
                  "Serve with cabbage, radish and lime."],
        "visibility": "family",
    })


# ---------- app wiring ----------

def build_local_app(data_dir=DEFAULT_DATA_DIR, seed_first_run=True, announce=print):
    """Returns the FastAPI app wired to the local user pool and saved recipe data.

    Must be called inside moto's mock_aws(). Returns (app, pool).
    """
    devtools.use_fake_aws(TABLE_NAME)
    os.environ["LOCAL_DEV_HEADERS"] = "1"
    os.makedirs(data_dir, exist_ok=True)
    snapshot_path = os.path.join(data_dir, "recipes.json")

    devtools.create_table(TABLE_NAME)
    restored = load_snapshot(snapshot_path)
    pool = LocalUserPool(os.path.join(data_dir, "accounts.sqlite3"),
                         region=os.environ["AWS_DEFAULT_REGION"], announce=announce)
    first_run = restored == 0 and pool.user_count() == 0

    main = importlib.reload(sys.modules["main"]) if "main" in sys.modules else importlib.import_module("main")
    main.local_token_verifier = pool.verify_id_token
    app = main.app

    @app.get("/config.json", include_in_schema=False)
    def local_config():
        # Same-origin API and a same-origin stand-in for the Cognito endpoint.
        return {"apiUrl": "", "cognitoEndpoint": "/local-cognito", "clientId": pool.client_id, "localDev": True}

    @app.post("/local-cognito", include_in_schema=False)
    async def local_cognito(request: Request):
        """Speaks the Cognito user pool JSON protocol, answered by the local SQLite pool."""
        action = request.headers.get("x-amz-target", "").rsplit(".", 1)[-1]
        try:
            body = await request.json()
            return await run_in_threadpool(pool.handle, action, body)
        except CognitoError as error:
            return JSONResponse({"__type": error.code, "message": error.message}, status_code=400)
        except ValueError:
            return JSONResponse({"__type": "SerializationException", "message": "Body must be JSON."},
                                status_code=400)

    @app.middleware("http")
    async def save_after_changes(request: Request, call_next):
        response = await call_next(request)
        if (request.method in ("POST", "PUT", "DELETE") and response.status_code < 400
                and request.url.path != "/local-cognito"):
            await run_in_threadpool(save_snapshot, snapshot_path)
        return response

    if first_run and seed_first_run:
        seed(app, pool)
        save_snapshot(snapshot_path)
    # Mounted last so the API routes above take priority over static files.
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="site")
    app.state.local_summary = {"first_run": first_run, "restored_items": restored, "accounts": pool.user_count()}
    return app, pool


def main_cli():
    parser = argparse.ArgumentParser(description="Run Recipe Book locally.")
    parser.add_argument("--reset", action="store_true", help="delete saved local accounts and recipes first")
    parser.add_argument("--empty", action="store_true", help="on a first run, skip the sample accounts and recipes")
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR, help="where local data is saved")
    args = parser.parse_args()

    if args.reset and os.path.isdir(args.data_dir):
        shutil.rmtree(args.data_dir)
        print(f"Deleted local data in {args.data_dir}")
    with mock_aws():
        app, pool = build_local_app(args.data_dir, seed_first_run=not args.empty,
                                    announce=lambda line: print(line, flush=True))
        summary = app.state.local_summary
        if summary["first_run"]:
            print("\nFirst run: created local data in", args.data_dir)
            if not args.empty:
                print("Test accounts:")
                for email, name, password in TEST_ACCOUNTS:
                    print(f"  {name:<6} {email:<20} password: {password}")
        else:
            print(f"\nLoaded {summary['accounts']} accounts and {summary['restored_items']} saved items "
                  f"from {args.data_dir} (use --reset to start over)")
        print()
        try:
            uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8001")))
        finally:
            pool.close()


if __name__ == "__main__":
    main_cli()

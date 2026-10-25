"""Recipe Book API (FastAPI): accounts, recipes, comments and families.

On AWS this runs in Lambda behind an API Gateway HTTP API; Mangum turns the
API Gateway event into an ASGI request. Routes other than /public/* are
protected by a Cognito JWT authorizer at API Gateway, so a verified token's
claims arrive in the event at requestContext.authorizer.jwt.claims.

Single-table layout (pk / sk):
  USER#<sub>        PROFILE              account profile, holds familyId
  RECIPE#<id>       META                 recipe; gsi1pk = PUBLIC | FAMILYFEED#<familyId>
  RECIPE#<id>       COMMENT#<commentId>  comment on a recipe
  FAMILY#<id>       META                 family name, owner, invite code
  FAMILY#<id>       MEMBER#<sub>         family membership
  INVITE#<code>     META                 invite code -> familyId
  RATE#...          W#<window>           rate limiter counters (TTL)
"""

import datetime
import os
import secrets
import time
import uuid
from typing import Annotated, Literal

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from mangum import Mangum
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints
from starlette.exceptions import HTTPException as StarletteHTTPException

import ratelimit

IN_LAMBDA = "AWS_LAMBDA_FUNCTION_NAME" in os.environ
TABLE = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
FEED_LIMIT = 50
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

app = FastAPI(
    title="Recipe Book API",
    version="1.0.0",
    # API Gateway only forwards the routes defined in infra/recipe_stack.py, so
    # the docs are only reachable when running locally.
    docs_url=None if IN_LAMBDA else "/docs",
    redoc_url=None,
    openapi_url=None if IN_LAMBDA else "/openapi.json",
)


# ---------- errors ----------

class ApiError(Exception):
    def __init__(self, status, message, headers=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.headers = headers


@app.exception_handler(ApiError)
def api_error_handler(request, error: ApiError):
    return JSONResponse({"message": error.message}, status_code=error.status, headers=error.headers)


@app.exception_handler(StarletteHTTPException)
def http_error_handler(request, error: StarletteHTTPException):
    return JSONResponse({"message": str(error.detail)}, status_code=error.status_code)


@app.exception_handler(RequestValidationError)
def validation_error_handler(request, error: RequestValidationError):
    first = error.errors()[0]
    field = next((part for part in reversed(first["loc"]) if isinstance(part, str)), "request")
    if field == "body":
        return JSONResponse({"message": "Request body must be a valid JSON object."}, status_code=400)
    return JSONResponse({"message": f"'{field}': {first['msg']}"}, status_code=400)


# ---------- request / response models ----------

def non_empty_lines(lines):
    lines = [line for line in lines if line]
    if not lines:
        raise ValueError("needs at least one line")
    return lines


def text(max_length, required=True):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=1 if required else 0,
                                            max_length=max_length)]


def lines(max_line_length):
    return Annotated[list[text(max_line_length, required=False)], Field(max_length=100),
                     AfterValidator(non_empty_lines)]


class ProfileUpdate(BaseModel):
    displayName: text(40)


HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9a-fA-F]{6}$")]


class SeasonAccents(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fall: HexColor | None = None
    winter: HexColor | None = None
    spring: HexColor | None = None
    summer: HexColor | None = None


class Preferences(BaseModel):
    """How the site looks for this account (applied by frontend/theme.js)."""
    model_config = ConfigDict(extra="forbid")
    season: Literal["auto", "fall", "winter", "spring", "summer"] = "auto"
    # "auto" season flips for the Southern Hemisphere. Only the hemisphere is
    # stored; the device's coordinates never leave the browser.
    hemisphere: Literal["north", "south"] = "north"
    followLocation: bool = Field(False, description="Re-detect the hemisphere from the device's location.")
    mode: Literal["system", "light", "dark"] = "system"
    font: Literal["serif", "sans"] = "serif"
    textSize: Literal["normal", "large"] = "normal"
    decorations: bool = True
    accents: SeasonAccents = Field(default_factory=SeasonAccents,
                                   description="Optional custom accent color per season.")


class Profile(BaseModel):
    userId: str
    email: str
    displayName: str
    familyId: str | None = None
    preferences: Preferences | None = None
    createdAt: str


class RecipeIn(BaseModel):
    title: text(120)
    description: text(2000, required=False) = ""
    ingredients: lines(500)
    steps: lines(2000)
    visibility: Literal["public", "family"] = "public"


class Recipe(BaseModel):
    recipeId: str
    title: str
    description: str = ""
    ingredients: list[str]
    steps: list[str]
    visibility: Literal["public", "family"]
    familyId: str | None = None
    authorId: str
    authorName: str
    createdAt: str
    updatedAt: str


class RecipeList(BaseModel):
    recipes: list[Recipe]


class Feed(BaseModel):
    family: list[Recipe] = Field(description="Your family's family-only recipes (empty if not in a family).")
    public: list[Recipe] = Field(description="Public recipes from everyone.")


class CommentIn(BaseModel):
    text: text(2000)


class Comment(BaseModel):
    commentId: str
    recipeId: str
    text: str
    authorId: str
    authorName: str
    createdAt: str


class CommentList(BaseModel):
    comments: list[Comment]


class FamilyCreate(BaseModel):
    name: text(60)


class FamilyJoin(BaseModel):
    inviteCode: text(20)


class Member(BaseModel):
    userId: str
    displayName: str
    joinedAt: str


class Family(BaseModel):
    familyId: str
    name: str
    ownerId: str
    createdAt: str
    members: list[Member]
    inviteCode: str | None = Field(None, description="Only returned to the family owner.")


class MyFamily(BaseModel):
    family: Family | None


# ---------- identity and rate limiting ----------

# Local development only: backend/local_dev.py sets this to a function that
# verifies tokens from its fake Cognito. In Lambda, API Gateway verifies tokens.
local_token_verifier = None


def claims_from(request: Request):
    event = request.scope.get("aws.event") or {}
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims")
    if claims or IN_LAMBDA:
        return claims
    authorization = request.headers.get("authorization", "")
    if local_token_verifier and authorization.lower().startswith("bearer "):
        return local_token_verifier(authorization[7:])
    if os.environ.get("LOCAL_DEV_HEADERS") and request.headers.get("x-dev-user"):
        # Shortcut for /docs and curl on the local dev server.
        user = request.headers["x-dev-user"]
        return {"sub": user, "email": f"{user}@example.com"}
    return None


def enforce_limit(bucket, identity, response: Response):
    decision = ratelimit.check(TABLE, bucket, identity)
    headers = {"X-RateLimit-Limit": str(decision.limit), "X-RateLimit-Remaining": str(decision.remaining)}
    if not decision.allowed:
        raise ApiError(429, "Too many requests. Slow down and try again shortly.",
                       {**headers, "Retry-After": str(decision.retry_after)})
    response.headers.update(headers)


def signed_in(bucket):
    def dependency(request: Request, response: Response) -> dict:
        claims = claims_from(request)
        if not claims:
            raise ApiError(401, "Sign in required.")
        enforce_limit(bucket, claims["sub"], response)
        return get_or_create_profile(claims)
    return dependency


def anonymous(request: Request, response: Response) -> None:
    enforce_limit("anonymous", request.client.host if request.client else "unknown", response)


Reader = Annotated[dict, Depends(signed_in("read"))]
Writer = Annotated[dict, Depends(signed_in("write"))]
Commenter = Annotated[dict, Depends(signed_in("comment"))]


# ---------- helpers ----------

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def is_conditional_failure(error):
    return error.response["Error"]["Code"] in ("ConditionalCheckFailedException", "TransactionCanceledException")


def with_current_names(items):
    """Replace each item's stored authorName with the author's current display name.

    Recipes and comments keep the name used when they were written as a
    fallback (e.g. for a deleted account); this looks up every distinct
    author's profile in one batched read so renames show up everywhere.
    """
    author_ids = list({item["authorId"] for item in items})
    names = {}
    for start in range(0, len(author_ids), 100):  # BatchGetItem takes up to 100 keys
        request = {TABLE.name: {
            "Keys": [{"pk": f"USER#{a}", "sk": "PROFILE"} for a in author_ids[start:start + 100]],
            "ProjectionExpression": "userId, displayName",
        }}
        while request:
            response = TABLE.meta.client.batch_get_item(RequestItems=request)
            names.update({p["userId"]: p["displayName"] for p in response["Responses"].get(TABLE.name, [])})
            request = response.get("UnprocessedKeys") or None
    return [{**item, "authorName": names.get(item["authorId"], item["authorName"])} for item in items]


def transact(items, conflict_message):
    try:
        TABLE.meta.client.transact_write_items(TransactItems=items)
    except ClientError as error:
        if is_conditional_failure(error):
            raise ApiError(409, conflict_message)
        raise


# ---------- profiles ----------

def get_or_create_profile(claims):
    key = {"pk": f"USER#{claims['sub']}", "sk": "PROFILE"}
    item = TABLE.get_item(Key=key).get("Item")
    if item:
        return item
    email = claims.get("email", "")
    # The name chosen on the sign-up page arrives as Cognito's standard `name` claim.
    name = (claims.get("name") or "").strip() or email.split("@")[0] or "cook"
    item = {
        **key,
        "userId": claims["sub"],
        "email": email,
        "displayName": name[:40],
        "createdAt": now_iso(),
    }
    try:
        TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(pk)")
    except ClientError as error:
        if not is_conditional_failure(error):
            raise
        item = TABLE.get_item(Key=key)["Item"]
    return item


@app.get("/me", response_model=Profile, tags=["profile"])
def get_me(profile: Reader):
    return profile


@app.put("/me", response_model=Profile, tags=["profile"])
def update_me(update: ProfileUpdate, profile: Writer):
    TABLE.update_item(
        Key={"pk": profile["pk"], "sk": "PROFILE"},
        UpdateExpression="SET displayName = :n",
        ExpressionAttributeValues={":n": update.displayName},
    )
    if profile.get("familyId"):
        TABLE.update_item(
            Key={"pk": f"FAMILY#{profile['familyId']}", "sk": f"MEMBER#{profile['userId']}"},
            UpdateExpression="SET displayName = :n",
            ExpressionAttributeValues={":n": update.displayName},
        )
    return {**profile, "displayName": update.displayName}


@app.put("/me/preferences", response_model=Profile, tags=["profile"],
         summary="Save how the site looks for this account (season, dark mode, colors)")
def update_preferences(preferences: Preferences, profile: Writer):
    stored = preferences.model_dump(exclude_none=True)
    TABLE.update_item(
        Key={"pk": profile["pk"], "sk": "PROFILE"},
        UpdateExpression="SET preferences = :p",
        ExpressionAttributeValues={":p": stored},
    )
    return {**profile, "preferences": stored}


# ---------- recipes ----------

def load_recipe(recipe_id):
    item = TABLE.get_item(Key={"pk": f"RECIPE#{recipe_id}", "sk": "META"}).get("Item")
    if not item:
        raise ApiError(404, "Recipe not found.")
    return item


def can_view(recipe, profile):
    if recipe["visibility"] == "public":
        return True
    if profile is None:
        return False
    return recipe["authorId"] == profile["userId"] or (
        recipe.get("familyId") and recipe["familyId"] == profile.get("familyId"))


def visible_recipe(recipe_id, profile):
    recipe = load_recipe(recipe_id)
    if not can_view(recipe, profile):
        # 404 rather than 403 so family-only recipes are not discoverable
        raise ApiError(404, "Recipe not found.")
    return recipe


def check_visibility(recipe_in: RecipeIn, profile):
    if recipe_in.visibility == "family" and not profile.get("familyId"):
        raise ApiError(400, "Join or create a family before sharing a recipe with family only.")


def feed_key(visibility, family_id):
    return "PUBLIC" if visibility == "public" else f"FAMILYFEED#{family_id}"


def query_feed(partition):
    return TABLE.query(
        IndexName="gsi1",
        KeyConditionExpression=Key("gsi1pk").eq(partition),
        ScanIndexForward=False,
        Limit=FEED_LIMIT,
    )["Items"]


@app.post("/recipes", response_model=Recipe, status_code=201, tags=["recipes"])
def create_recipe(recipe_in: RecipeIn, profile: Writer):
    check_visibility(recipe_in, profile)
    recipe_id = uuid.uuid4().hex
    created = now_iso()
    item = {
        "pk": f"RECIPE#{recipe_id}",
        "sk": "META",
        "recipeId": recipe_id,
        **recipe_in.model_dump(),
        "authorId": profile["userId"],
        "authorName": profile["displayName"],
        "createdAt": created,
        "updatedAt": created,
        "gsi1pk": feed_key(recipe_in.visibility, profile.get("familyId")),
        "gsi1sk": f"{created}#{recipe_id}",
    }
    if recipe_in.visibility == "family":
        item["familyId"] = profile["familyId"]
    TABLE.put_item(Item=item)
    return item


@app.get("/recipes", response_model=Feed, tags=["recipes"],
         summary="Your family's recipes and public recipes as separate lists, newest first")
def list_recipes(profile: Reader):
    family_id = profile.get("familyId")
    family = query_feed(f"FAMILYFEED#{family_id}") if family_id else []
    public = query_feed("PUBLIC")
    named = with_current_names(family + public)
    return {"family": named[:len(family)], "public": named[len(family):]}


@app.get("/recipes/{id}", response_model=Recipe, tags=["recipes"])
def get_recipe(id: str, profile: Reader):
    return with_current_names([visible_recipe(id, profile)])[0]


@app.put("/recipes/{id}", response_model=Recipe, tags=["recipes"])
def update_recipe(id: str, recipe_in: RecipeIn, profile: Writer):
    recipe = load_recipe(id)
    if recipe["authorId"] != profile["userId"]:
        raise ApiError(403, "Only the author can edit this recipe.")
    check_visibility(recipe_in, profile)
    family_id = profile.get("familyId") if recipe_in.visibility == "family" else None
    recipe.update(recipe_in.model_dump(), updatedAt=now_iso(), authorName=profile["displayName"],
                  gsi1pk=feed_key(recipe_in.visibility, family_id))
    if family_id:
        recipe["familyId"] = family_id
    else:
        recipe.pop("familyId", None)
    TABLE.put_item(Item=recipe)
    return recipe


@app.delete("/recipes/{id}", status_code=204, response_class=Response, tags=["recipes"])
def delete_recipe(id: str, profile: Writer):
    recipe = load_recipe(id)
    if recipe["authorId"] != profile["userId"]:
        raise ApiError(403, "Only the author can delete this recipe.")
    keys = TABLE.query(
        KeyConditionExpression=Key("pk").eq(recipe["pk"]),
        ProjectionExpression="pk, sk",
    )["Items"]
    with TABLE.batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key={"pk": key["pk"], "sk": key["sk"]})


# ---------- comments ----------

def comments_for(recipe):
    return {"comments": with_current_names(TABLE.query(
        KeyConditionExpression=Key("pk").eq(recipe["pk"]) & Key("sk").begins_with("COMMENT#"),
    )["Items"])}


@app.get("/recipes/{id}/comments", response_model=CommentList, tags=["comments"])
def list_comments(id: str, profile: Reader):
    return comments_for(visible_recipe(id, profile))


@app.post("/recipes/{id}/comments", response_model=Comment, status_code=201, tags=["comments"])
def add_comment(id: str, comment_in: CommentIn, profile: Commenter):
    recipe = visible_recipe(id, profile)
    comment_id = f"{int(time.time() * 1000):013d}{uuid.uuid4().hex[:8]}"
    item = {
        "pk": recipe["pk"],
        "sk": f"COMMENT#{comment_id}",
        "commentId": comment_id,
        "recipeId": recipe["recipeId"],
        "text": comment_in.text,
        "authorId": profile["userId"],
        "authorName": profile["displayName"],
        "createdAt": now_iso(),
    }
    TABLE.put_item(Item=item)
    return item


@app.delete("/recipes/{id}/comments/{commentId}", status_code=204, response_class=Response, tags=["comments"],
            summary="Delete a comment (its author or the recipe's author)")
def delete_comment(id: str, commentId: str, profile: Writer):
    recipe = visible_recipe(id, profile)
    key = {"pk": recipe["pk"], "sk": f"COMMENT#{commentId}"}
    comment = TABLE.get_item(Key=key).get("Item")
    if not comment:
        raise ApiError(404, "Comment not found.")
    if profile["userId"] not in (comment["authorId"], recipe["authorId"]):
        raise ApiError(403, "Only the comment's author or the recipe's author can delete it.")
    TABLE.delete_item(Key=key)


# ---------- families ----------

def new_invite_code():
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))


def family_view(family_id, user_id):
    family = TABLE.get_item(Key={"pk": f"FAMILY#{family_id}", "sk": "META"}).get("Item")
    if not family:
        raise ApiError(404, "Family not found.")
    members = TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"FAMILY#{family_id}") & Key("sk").begins_with("MEMBER#"),
    )["Items"]
    return {
        **family,
        "members": sorted(members, key=lambda m: m["joinedAt"]),
        "inviteCode": family["inviteCode"] if family["ownerId"] == user_id else None,
    }


def member_item(family_id, profile, joined):
    return {
        "pk": f"FAMILY#{family_id}",
        "sk": f"MEMBER#{profile['userId']}",
        "userId": profile["userId"],
        "displayName": profile["displayName"],
        "joinedAt": joined,
    }


def set_family_on_profile(profile, family_id):
    return {"Update": {
        "TableName": TABLE.name,
        "Key": {"pk": profile["pk"], "sk": "PROFILE"},
        "UpdateExpression": "SET familyId = :f",
        "ConditionExpression": "attribute_exists(pk) AND attribute_not_exists(familyId)",
        "ExpressionAttributeValues": {":f": family_id},
    }}


def clear_family_on_profile(user_id, family_id):
    return {"Update": {
        "TableName": TABLE.name,
        "Key": {"pk": f"USER#{user_id}", "sk": "PROFILE"},
        "UpdateExpression": "REMOVE familyId",
        "ConditionExpression": "familyId = :f",
        "ExpressionAttributeValues": {":f": family_id},
    }}


def require_family(profile, owner_only=False):
    if not profile.get("familyId"):
        raise ApiError(404, "You are not in a family.")
    family = TABLE.get_item(Key={"pk": f"FAMILY#{profile['familyId']}", "sk": "META"}).get("Item")
    if not family:
        raise ApiError(404, "Family not found.")
    if owner_only and family["ownerId"] != profile["userId"]:
        raise ApiError(403, "Only the family owner can do that.")
    return family


@app.post("/families", response_model=Family, status_code=201, tags=["families"])
def create_family(family_in: FamilyCreate, profile: Writer):
    if profile.get("familyId"):
        raise ApiError(409, "You are already in a family. Leave it first.")
    family_id, code, created = uuid.uuid4().hex, new_invite_code(), now_iso()
    transact([
        {"Put": {"TableName": TABLE.name, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
            "pk": f"FAMILY#{family_id}", "sk": "META", "familyId": family_id, "name": family_in.name,
            "ownerId": profile["userId"], "inviteCode": code, "createdAt": created}}},
        {"Put": {"TableName": TABLE.name, "Item": member_item(family_id, profile, created)}},
        {"Put": {"TableName": TABLE.name, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
            "pk": f"INVITE#{code}", "sk": "META", "familyId": family_id}}},
        set_family_on_profile(profile, family_id),
    ], "You are already in a family. Leave it first.")
    return family_view(family_id, profile["userId"])


@app.get("/families/mine", response_model=MyFamily, tags=["families"])
def my_family(profile: Reader):
    if not profile.get("familyId"):
        return {"family": None}
    return {"family": family_view(profile["familyId"], profile["userId"])}


@app.post("/families/join", response_model=Family, tags=["families"])
def join_family(join: FamilyJoin, profile: Writer):
    if profile.get("familyId"):
        raise ApiError(409, "You are already in a family. Leave it first.")
    code = join.inviteCode.upper()
    invite = TABLE.get_item(Key={"pk": f"INVITE#{code}", "sk": "META"}).get("Item")
    if not invite:
        raise ApiError(404, "That invite code is not valid.")
    family_id = invite["familyId"]
    transact([
        {"ConditionCheck": {"TableName": TABLE.name, "Key": {"pk": f"FAMILY#{family_id}", "sk": "META"},
                            "ConditionExpression": "inviteCode = :c",
                            "ExpressionAttributeValues": {":c": code}}},
        {"Put": {"TableName": TABLE.name, "Item": member_item(family_id, profile, now_iso())}},
        set_family_on_profile(profile, family_id),
    ], "Could not join: you may already be in a family, or the code was just changed.")
    return family_view(family_id, profile["userId"])


@app.post("/families/leave", response_model=MyFamily, tags=["families"],
          summary="Leave your family (ownership passes on; the last member out deletes it)")
def leave_family(profile: Writer):
    family = require_family(profile)
    family_id, user_id = family["familyId"], profile["userId"]
    members = TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"FAMILY#{family_id}") & Key("sk").begins_with("MEMBER#"),
    )["Items"]
    others = sorted((m for m in members if m["userId"] != user_id), key=lambda m: m["joinedAt"])
    items = [
        {"Delete": {"TableName": TABLE.name, "Key": {"pk": f"FAMILY#{family_id}", "sk": f"MEMBER#{user_id}"}}},
        clear_family_on_profile(user_id, family_id),
    ]
    if not others:
        # Last member out: remove the family and its invite code.
        items += [
            {"Delete": {"TableName": TABLE.name, "Key": {"pk": f"FAMILY#{family_id}", "sk": "META"}}},
            {"Delete": {"TableName": TABLE.name, "Key": {"pk": f"INVITE#{family['inviteCode']}", "sk": "META"}}},
        ]
    elif family["ownerId"] == user_id:
        # Hand ownership to the longest-standing remaining member.
        items.append({"Update": {
            "TableName": TABLE.name, "Key": {"pk": f"FAMILY#{family_id}", "sk": "META"},
            "UpdateExpression": "SET ownerId = :o", "ExpressionAttributeValues": {":o": others[0]["userId"]}}})
    transact(items, "Your family changed while leaving. Try again.")
    return {"family": None}


@app.post("/families/mine/invite-code", response_model=Family, tags=["families"],
          summary="Replace the invite code (owner only; the old code stops working)")
def rotate_invite_code(profile: Writer):
    family = require_family(profile, owner_only=True)
    code = new_invite_code()
    transact([
        {"Delete": {"TableName": TABLE.name, "Key": {"pk": f"INVITE#{family['inviteCode']}", "sk": "META"}}},
        {"Put": {"TableName": TABLE.name, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
            "pk": f"INVITE#{code}", "sk": "META", "familyId": family["familyId"]}}},
        {"Update": {"TableName": TABLE.name, "Key": {"pk": family["pk"], "sk": "META"},
                    "UpdateExpression": "SET inviteCode = :c", "ExpressionAttributeValues": {":c": code}}},
    ], "Could not create a new code. Try again.")
    return family_view(family["familyId"], profile["userId"])


@app.delete("/families/mine/members/{userId}", response_model=Family, tags=["families"],
            summary="Remove a member (owner only)")
def remove_member(userId: str, profile: Writer):
    family = require_family(profile, owner_only=True)
    if userId == profile["userId"]:
        raise ApiError(400, "Use 'leave family' to remove yourself.")
    transact([
        {"Delete": {"TableName": TABLE.name, "Key": {"pk": family["pk"], "sk": f"MEMBER#{userId}"},
                    "ConditionExpression": "attribute_exists(pk)"}},
        clear_family_on_profile(userId, family["familyId"]),
    ], "That account is not in your family.")
    return family_view(family["familyId"], profile["userId"])


# ---------- public (signed-out) routes ----------

@app.get("/public/recipes", response_model=RecipeList, tags=["public"], dependencies=[Depends(anonymous)])
def public_list():
    return {"recipes": with_current_names(query_feed("PUBLIC"))}


@app.get("/public/recipes/{id}", response_model=Recipe, tags=["public"], dependencies=[Depends(anonymous)])
def public_get(id: str):
    return with_current_names([visible_recipe(id, None)])[0]


@app.get("/public/recipes/{id}/comments", response_model=CommentList, tags=["public"],
         dependencies=[Depends(anonymous)])
def public_comments(id: str):
    return comments_for(visible_recipe(id, None))


# Lambda entry point (handler = "main.handler").
handler = Mangum(app, lifespan="off")

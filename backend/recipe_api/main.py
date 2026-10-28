"""Recipe Book API (FastAPI): accounts, recipes, comments and families.

On AWS this runs in Lambda behind an API Gateway HTTP API; Mangum turns the
API Gateway event into an ASGI request. Routes other than /public/* are
protected by a Cognito JWT authorizer at API Gateway, so a verified token's
claims arrive in the event at requestContext.authorizer.jwt.claims.

Single-table layout (pk / sk):
  USER#<sub>        PROFILE              account profile, holds familyIds (a string set)
  RECIPE#<id>       META                 recipe; family-only ones hold familyIds; gsi1pk = PUBLIC if public
  RECIPE#<id>       SHARE#<familyId>     puts a family-only recipe in one family's feed (gsi1pk = FAMILYFEED#<id>)
  RECIPE#<id>       COMMENT#<commentId>  comment on a recipe
  FAMILY#<id>       META                 family name, owner, invite code
  FAMILY#<id>       MEMBER#<sub>         family membership
  INVITE#<code>     META                 invite code -> familyId
  RATE#...          W#<window>           rate limiter counters (TTL)

Indexes: gsi1 (gsi1pk, gsi1sk) holds the feeds; byAuthor (authorId, createdAt)
finds everything one account wrote (recipes, and comments, which are skipped).
"""

import datetime
import os
import secrets
import time
import uuid
from typing import Annotated, Literal

import boto3
from boto3.dynamodb.conditions import Attr, Key
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
MY_RECIPES_LIMIT = 500  # "My recipes" has no paging yet
MAX_FAMILIES = 10  # per account; keeps the home feed to a handful of queries
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


# A recipe can be shared with at most as many families as one account can be in.
FamilyChoice = Annotated[list[text(64)], Field(max_length=MAX_FAMILIES)] | None


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
    familyIds: list[str] = Field(default_factory=list, description="The families this account belongs to.")
    preferences: Preferences | None = None
    createdAt: str


class RecipeIn(BaseModel):
    title: text(120)
    description: text(2000, required=False) = ""
    ingredients: lines(500)
    steps: lines(2000)
    visibility: Literal["public", "family"] = "public"
    familyIds: FamilyChoice = Field(None, description="Which of your families can see a family-only recipe. "
                                                      "Optional when you're in exactly one family, and when "
                                                      "editing (it then stays shared as it is).")
    familyId: str | None = Field(None, description="Deprecated: one family, the same as familyIds=[familyId].")


class Sharing(BaseModel):
    visibility: Literal["public", "family"]
    familyIds: FamilyChoice = Field(None, description="For family-only: the families that can see it.")


class Recipe(BaseModel):
    recipeId: str
    title: str
    description: str = ""
    ingredients: list[str]
    steps: list[str]
    visibility: Literal["public", "family"]
    familyIds: list[str] = Field(default_factory=list,
                                 description="The families a family-only recipe is shared with: all of them for "
                                             "its author, only your own for everyone else.")
    authorId: str
    authorName: str
    createdAt: str
    updatedAt: str


class RecipeList(BaseModel):
    recipes: list[Recipe]


class Feed(BaseModel):
    family: list[Recipe] = Field(description="Family-only recipes from all your families, newest first.")
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


class MyFamilies(BaseModel):
    families: list[Family] = Field(description="Your families, sorted by name.")


class FamilyRename(BaseModel):
    name: text(60)


class InvitePreview(BaseModel):
    familyId: str
    name: str
    memberCount: int
    alreadyMember: bool


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
    keys = [{"pk": f"USER#{a}", "sk": "PROFILE"} for a in {item["authorId"] for item in items}]
    names = {p["userId"]: p["displayName"] for p in batch_get(keys, ProjectionExpression="userId, displayName")}
    return [{**item, "authorName": names.get(item["authorId"], item["authorName"])} for item in items]


def transact(items, conflict_message):
    try:
        TABLE.meta.client.transact_write_items(TransactItems=items)
    except ClientError as error:
        if is_conditional_failure(error):
            raise ApiError(409, conflict_message)
        raise


# ---------- profiles ----------

def family_ids(profile):
    return set(profile.get("familyIds") or ())


def upgrade_single_family(item):
    """Profiles saved before accounts could join several families hold one
    `familyId`; move it into the `familyIds` set the first time it's read."""
    try:
        return TABLE.update_item(
            Key={"pk": item["pk"], "sk": "PROFILE"},
            UpdateExpression="ADD familyIds :s REMOVE familyId",
            ConditionExpression="attribute_exists(familyId)",
            ExpressionAttributeValues={":s": {item["familyId"]}},
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as error:
        if not is_conditional_failure(error):
            raise
        return TABLE.get_item(Key={"pk": item["pk"], "sk": "PROFILE"})["Item"]  # another request upgraded it


def get_or_create_profile(claims):
    key = {"pk": f"USER#{claims['sub']}", "sk": "PROFILE"}
    item = TABLE.get_item(Key=key).get("Item")
    if item:
        if "familyId" in item:
            item = upgrade_single_family(item)
        return {**item, "familyIds": sorted(family_ids(item))}
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
    for family_id in family_ids(profile):
        TABLE.update_item(
            Key={"pk": f"FAMILY#{family_id}", "sk": f"MEMBER#{profile['userId']}"},
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


def own_recipe(recipe_id, profile, action):
    recipe = load_recipe(recipe_id)
    if recipe["authorId"] != profile["userId"]:
        raise ApiError(403, f"Only the author can {action} this recipe.")
    return recipe


def recipe_family_ids(recipe):
    """The families a family-only recipe is shared with. Recipes saved before
    recipes could be shared with several families hold one `familyId` instead."""
    shared = set(recipe.get("familyIds") or ())
    if recipe.get("familyId"):
        shared.add(recipe["familyId"])
    return shared


def can_view(recipe, profile):
    if recipe["visibility"] == "public":
        return True
    if profile is None:
        return False
    return recipe["authorId"] == profile["userId"] or bool(recipe_family_ids(recipe) & family_ids(profile))


def visible_recipe(recipe_id, profile):
    recipe = load_recipe(recipe_id)
    if not can_view(recipe, profile):
        # 404 rather than 403 so family-only recipes are not discoverable
        raise ApiError(404, "Recipe not found.")
    return recipe


def present(recipes, profile):
    """Recipes as `profile` may see them, with current author names. Only the author
    learns every family a recipe is shared with; other members see only their own."""
    viewer_families = family_ids(profile) if profile else set()
    shown = []
    for recipe in with_current_names(recipes):
        shared = recipe_family_ids(recipe)
        if not profile or recipe["authorId"] != profile["userId"]:
            shared &= viewer_families
        shown.append({**recipe, "familyIds": sorted(shared)})
    return shown


def chosen_families(visibility, requested, profile, recipe=None):
    """The families a recipe will be shared with: none for public, else some of the author's.

    `requested` None keeps the families an existing recipe is shared with, or picks the
    author's only family. Families the recipe is already shared with may be kept (or
    dropped) even after the author has left them; new ones must be the author's own.
    """
    if visibility == "public":
        return set()
    mine = family_ids(profile)
    current = recipe_family_ids(recipe) if recipe and recipe["visibility"] == "family" else set()
    if requested is None:
        if current:
            return current
        if not mine:
            raise ApiError(400, "Join or create a family before sharing a recipe with family only.")
        if len(mine) > 1:
            raise ApiError(400, "Choose which of your families can see this recipe.")
        return set(mine)
    requested = set(requested)
    if not requested:
        raise ApiError(400, "Choose at least one family to share this recipe with.")
    if not requested <= mine | current:
        raise ApiError(400, "You can only share family recipes with families you're in.")
    return requested


def requested_families(recipe_in: RecipeIn):
    if recipe_in.familyIds is None and recipe_in.familyId:
        return [recipe_in.familyId]  # the older one-family form
    return recipe_in.familyIds


def with_sharing(item, visibility, families):
    """`item` with its visibility set: public recipes are in the PUBLIC feed; family-only
    ones list their families (each family's feed entry is a separate SHARE item)."""
    item = {k: v for k, v in item.items() if k not in ("familyId", "familyIds", "gsi1pk", "gsi1sk")}
    item["visibility"] = visibility
    if families:
        item["familyIds"] = set(families)
    else:
        item.update(gsi1pk="PUBLIC", gsi1sk=f"{item['createdAt']}#{item['recipeId']}")
    return item


def share_item(recipe, family_id):
    # Holds no recipe data: family feeds read the recipe itself (see family_feed).
    return {"pk": recipe["pk"], "sk": f"SHARE#{family_id}", "recipeId": recipe["recipeId"], "familyId": family_id,
            "gsi1pk": f"FAMILYFEED#{family_id}", "gsi1sk": f"{recipe['createdAt']}#{recipe['recipeId']}"}


def save_recipe(item, previous_families, condition):
    """Writes the recipe and its SHARE items in one transaction. Every current share is
    (re)written, which also upgrades recipes saved before SHARE items existed."""
    families = item.get("familyIds", set())
    transact([
        {"Put": {"TableName": TABLE.name, "Item": item, **condition}},
        *[{"Put": {"TableName": TABLE.name, "Item": share_item(item, f)}} for f in sorted(families)],
        *[{"Delete": {"TableName": TABLE.name, "Key": {"pk": item["pk"], "sk": f"SHARE#{f}"}}}
          for f in sorted(previous_families - families)],
    ], "This recipe changed while you were saving it. Try again.")


def query_feed(partition):
    return TABLE.query(
        IndexName="gsi1",
        KeyConditionExpression=Key("gsi1pk").eq(partition),
        ScanIndexForward=False,
        Limit=FEED_LIMIT,
    )["Items"]


def batch_get(keys, **options):
    """BatchGetItem for any number of keys (it takes up to 100 at a time), retrying unprocessed ones."""
    items = []
    for start in range(0, len(keys), 100):
        request = {TABLE.name: {"Keys": keys[start:start + 100], **options}}
        while request:
            response = TABLE.meta.client.batch_get_item(RequestItems=request)
            items += response["Responses"].get(TABLE.name, [])
            request = response.get("UnprocessedKeys") or None
    return items


def family_feed(family_id):
    """Newest family-only recipes shared with one family.

    The feed index holds SHARE items (and, for recipes saved before those existed, the
    recipe itself). Each recipe is read and checked again, so a share left behind by
    an interrupted change can never show a recipe to a family it isn't shared with.
    """
    ids = list(dict.fromkeys(entry["recipeId"] for entry in query_feed(f"FAMILYFEED#{family_id}")))
    found = {r["recipeId"]: r for r in batch_get([{"pk": f"RECIPE#{i}", "sk": "META"} for i in ids])}
    return [found[i] for i in ids
            if i in found and found[i]["visibility"] == "family" and family_id in recipe_family_ids(found[i])]


def newest_first(recipes):
    return sorted(recipes, key=lambda r: (r["createdAt"], r["recipeId"]), reverse=True)


@app.post("/recipes", response_model=Recipe, status_code=201, tags=["recipes"])
def create_recipe(recipe_in: RecipeIn, profile: Writer):
    families = chosen_families(recipe_in.visibility, requested_families(recipe_in), profile)
    recipe_id = uuid.uuid4().hex
    created = now_iso()
    item = with_sharing({
        "pk": f"RECIPE#{recipe_id}",
        "sk": "META",
        "recipeId": recipe_id,
        **recipe_in.model_dump(exclude={"visibility", "familyIds", "familyId"}),
        "authorId": profile["userId"],
        "authorName": profile["displayName"],
        "createdAt": created,
        "updatedAt": created,
    }, recipe_in.visibility, families)
    save_recipe(item, set(), {"ConditionExpression": "attribute_not_exists(pk)"})
    return present([item], profile)[0]


@app.get("/recipes", response_model=Feed, tags=["recipes"],
         summary="Family recipes from all your families, and public recipes, as separate lists, newest first")
def list_recipes(profile: Reader):
    # A recipe shared with several of your families is listed once.
    family = {r["recipeId"]: r for family_id in family_ids(profile) for r in family_feed(family_id)}
    family = newest_first(family.values())[:FEED_LIMIT]
    public = query_feed("PUBLIC")
    shown = present(family + public, profile)
    return {"family": shown[:len(family)], "public": shown[len(family):]}


@app.get("/me/recipes", response_model=RecipeList, tags=["recipes"],
         summary="Every recipe you've written, public and family-only, newest first")
def my_recipes(profile: Reader):
    recipes, page = [], {}
    while len(recipes) < MY_RECIPES_LIMIT:
        result = TABLE.query(
            IndexName="byAuthor",
            KeyConditionExpression=Key("authorId").eq(profile["userId"]),
            FilterExpression=Attr("sk").eq("META"),  # the index also holds your comments
            ScanIndexForward=False,
            **page,
        )
        recipes += result["Items"]
        if "LastEvaluatedKey" not in result:
            break
        page = {"ExclusiveStartKey": result["LastEvaluatedKey"]}
    return {"recipes": present(newest_first(recipes)[:MY_RECIPES_LIMIT], profile)}


@app.get("/recipes/{id}", response_model=Recipe, tags=["recipes"])
def get_recipe(id: str, profile: Reader):
    return present([visible_recipe(id, profile)], profile)[0]


@app.put("/recipes/{id}", response_model=Recipe, tags=["recipes"])
def update_recipe(id: str, recipe_in: RecipeIn, profile: Writer):
    recipe = own_recipe(id, profile, "edit")
    families = chosen_families(recipe_in.visibility, requested_families(recipe_in), profile, recipe)
    item = with_sharing({
        **recipe,
        **recipe_in.model_dump(exclude={"visibility", "familyIds", "familyId"}),
        "authorName": profile["displayName"],
        "updatedAt": now_iso(),
    }, recipe_in.visibility, families)
    save_recipe(item, recipe_family_ids(recipe), {"ConditionExpression": "authorId = :a",
                                                  "ExpressionAttributeValues": {":a": profile["userId"]}})
    return present([item], profile)[0]


@app.put("/recipes/{id}/sharing", response_model=Recipe, tags=["recipes"],
         summary="Share a recipe with everyone, or with some of your families (author only)")
def share_recipe(id: str, sharing: Sharing, profile: Writer):
    # Sharing "public" is how an author publishes a family recipe to everyone; it then
    # leaves its families' feeds (their members still see it under Public recipes).
    recipe = own_recipe(id, profile, "share")
    families = chosen_families(sharing.visibility, sharing.familyIds, profile, recipe)
    item = with_sharing(recipe, sharing.visibility, families)
    save_recipe(item, recipe_family_ids(recipe), {"ConditionExpression": "authorId = :a",
                                                  "ExpressionAttributeValues": {":a": profile["userId"]}})
    return present([item], profile)[0]


@app.delete("/recipes/{id}", status_code=204, response_class=Response, tags=["recipes"])
def delete_recipe(id: str, profile: Writer):
    # Removes the recipe with everything under it: comments and SHARE items.
    recipe = own_recipe(id, profile, "delete")
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


def add_family_to_profile(profile, family_id):
    # Fails the transaction if the account is already in this family or in MAX_FAMILIES families.
    return {"Update": {
        "TableName": TABLE.name,
        "Key": {"pk": profile["pk"], "sk": "PROFILE"},
        "UpdateExpression": "ADD familyIds :s",
        "ConditionExpression": "attribute_exists(pk) AND (attribute_not_exists(familyIds) OR "
                               "(NOT contains(familyIds, :f) AND size(familyIds) < :max))",
        "ExpressionAttributeValues": {":s": {family_id}, ":f": family_id, ":max": MAX_FAMILIES},
    }}


def remove_family_from_profile(user_id, family_id):
    return {"Update": {
        "TableName": TABLE.name,
        "Key": {"pk": f"USER#{user_id}", "sk": "PROFILE"},
        "UpdateExpression": "DELETE familyIds :s",
        "ConditionExpression": "contains(familyIds, :f)",
        "ExpressionAttributeValues": {":s": {family_id}, ":f": family_id},
    }}


def check_room_for_another_family(profile):
    if len(family_ids(profile)) >= MAX_FAMILIES:
        raise ApiError(409, f"You can be in up to {MAX_FAMILIES} families. Leave one first.")


def require_family(profile, family_id, owner_only=False):
    if family_id not in family_ids(profile):
        raise ApiError(404, "You are not in that family.")
    family = TABLE.get_item(Key={"pk": f"FAMILY#{family_id}", "sk": "META"}).get("Item")
    if not family:
        raise ApiError(404, "Family not found.")
    if owner_only and family["ownerId"] != profile["userId"]:
        raise ApiError(403, "Only the family owner can do that.")
    return family


def lookup_invite(invite_code):
    code = invite_code.strip().upper()
    invite = TABLE.get_item(Key={"pk": f"INVITE#{code}", "sk": "META"}).get("Item")
    if not invite:
        raise ApiError(404, "That invite code is not valid.")
    return code, invite["familyId"]


def my_families_view(profile):
    families = [family_view(family_id, profile["userId"]) for family_id in family_ids(profile)]
    return {"families": sorted(families, key=lambda f: (f["name"].lower(), f["familyId"]))}


@app.post("/families", response_model=Family, status_code=201, tags=["families"])
def create_family(family_in: FamilyCreate, profile: Writer):
    check_room_for_another_family(profile)
    family_id, code, created = uuid.uuid4().hex, new_invite_code(), now_iso()
    transact([
        {"Put": {"TableName": TABLE.name, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
            "pk": f"FAMILY#{family_id}", "sk": "META", "familyId": family_id, "name": family_in.name,
            "ownerId": profile["userId"], "inviteCode": code, "createdAt": created}}},
        {"Put": {"TableName": TABLE.name, "Item": member_item(family_id, profile, created)}},
        {"Put": {"TableName": TABLE.name, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
            "pk": f"INVITE#{code}", "sk": "META", "familyId": family_id}}},
        add_family_to_profile(profile, family_id),
    ], f"You can be in up to {MAX_FAMILIES} families. Leave one first.")
    return family_view(family_id, profile["userId"])


@app.get("/families/mine", response_model=MyFamilies, tags=["families"])
def my_families(profile: Reader):
    return my_families_view(profile)


@app.get("/families/invite/{code}", response_model=InvitePreview, tags=["families"],
         summary="Which family an invite code is for, so a join link can say what you're joining")
def preview_invite(code: str, profile: Reader):
    _, family_id = lookup_invite(code)
    family = TABLE.get_item(Key={"pk": f"FAMILY#{family_id}", "sk": "META"}).get("Item")
    if not family:
        raise ApiError(404, "That invite code is not valid.")
    members = TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"FAMILY#{family_id}") & Key("sk").begins_with("MEMBER#"),
        Select="COUNT",
    )["Count"]
    return {"familyId": family_id, "name": family["name"], "memberCount": members,
            "alreadyMember": family_id in family_ids(profile)}


@app.post("/families/join", response_model=Family, tags=["families"])
def join_family(join: FamilyJoin, profile: Writer):
    code, family_id = lookup_invite(join.inviteCode)
    if family_id in family_ids(profile):
        raise ApiError(409, "You're already in this family.")
    check_room_for_another_family(profile)
    transact([
        {"ConditionCheck": {"TableName": TABLE.name, "Key": {"pk": f"FAMILY#{family_id}", "sk": "META"},
                            "ConditionExpression": "inviteCode = :c",
                            "ExpressionAttributeValues": {":c": code}}},
        {"Put": {"TableName": TABLE.name, "Item": member_item(family_id, profile, now_iso())}},
        add_family_to_profile(profile, family_id),
    ], "Could not join: you may already be in this family, or the code was just changed.")
    return family_view(family_id, profile["userId"])


@app.put("/families/{familyId}", response_model=Family, tags=["families"],
         summary="Rename a family (owner only)")
def rename_family(familyId: str, rename: FamilyRename, profile: Writer):
    family = require_family(profile, familyId, owner_only=True)
    TABLE.update_item(Key={"pk": family["pk"], "sk": "META"}, UpdateExpression="SET #n = :n",
                      ExpressionAttributeNames={"#n": "name"}, ExpressionAttributeValues={":n": rename.name})
    return family_view(familyId, profile["userId"])


@app.get("/families/{familyId}/recipes", response_model=RecipeList, tags=["families"],
         summary="The family-only recipes shared with one family, newest first (members only)")
def family_recipes(familyId: str, profile: Reader):
    require_family(profile, familyId)
    return {"recipes": present(family_feed(familyId), profile)}


@app.post("/families/{familyId}/leave", response_model=MyFamilies, tags=["families"],
          summary="Leave a family (ownership passes on; the last member out deletes it)")
def leave_family(familyId: str, profile: Writer):
    family = require_family(profile, familyId)
    user_id = profile["userId"]
    members = TABLE.query(
        KeyConditionExpression=Key("pk").eq(f"FAMILY#{familyId}") & Key("sk").begins_with("MEMBER#"),
    )["Items"]
    others = sorted((m for m in members if m["userId"] != user_id), key=lambda m: m["joinedAt"])
    items = [
        {"Delete": {"TableName": TABLE.name, "Key": {"pk": f"FAMILY#{familyId}", "sk": f"MEMBER#{user_id}"}}},
        remove_family_from_profile(user_id, familyId),
    ]
    if not others:
        # Last member out: remove the family and its invite code.
        items += [
            {"Delete": {"TableName": TABLE.name, "Key": {"pk": f"FAMILY#{familyId}", "sk": "META"}}},
            {"Delete": {"TableName": TABLE.name, "Key": {"pk": f"INVITE#{family['inviteCode']}", "sk": "META"}}},
        ]
    elif family["ownerId"] == user_id:
        # Hand ownership to the longest-standing remaining member.
        items.append({"Update": {
            "TableName": TABLE.name, "Key": {"pk": f"FAMILY#{familyId}", "sk": "META"},
            "UpdateExpression": "SET ownerId = :o", "ExpressionAttributeValues": {":o": others[0]["userId"]}}})
    transact(items, "Your family changed while leaving. Try again.")
    return my_families_view({**profile, "familyIds": sorted(family_ids(profile) - {familyId})})


@app.post("/families/{familyId}/invite-code", response_model=Family, tags=["families"],
          summary="Replace the invite code (owner only; the old code stops working)")
def rotate_invite_code(familyId: str, profile: Writer):
    family = require_family(profile, familyId, owner_only=True)
    code = new_invite_code()
    transact([
        {"Delete": {"TableName": TABLE.name, "Key": {"pk": f"INVITE#{family['inviteCode']}", "sk": "META"}}},
        {"Put": {"TableName": TABLE.name, "ConditionExpression": "attribute_not_exists(pk)", "Item": {
            "pk": f"INVITE#{code}", "sk": "META", "familyId": familyId}}},
        {"Update": {"TableName": TABLE.name, "Key": {"pk": family["pk"], "sk": "META"},
                    "UpdateExpression": "SET inviteCode = :c", "ExpressionAttributeValues": {":c": code}}},
    ], "Could not create a new code. Try again.")
    return family_view(familyId, profile["userId"])


@app.delete("/families/{familyId}/members/{userId}", response_model=Family, tags=["families"],
            summary="Remove a member (owner only)")
def remove_member(familyId: str, userId: str, profile: Writer):
    family = require_family(profile, familyId, owner_only=True)
    if userId == profile["userId"]:
        raise ApiError(400, "Use 'leave family' to remove yourself.")
    # The removed account may not have been upgraded from a single familyId yet.
    upgrade_profile_by_id(userId)
    transact([
        {"Delete": {"TableName": TABLE.name, "Key": {"pk": family["pk"], "sk": f"MEMBER#{userId}"},
                    "ConditionExpression": "attribute_exists(pk)"}},
        remove_family_from_profile(userId, familyId),
    ], "That account is not in your family.")
    return family_view(familyId, profile["userId"])


def upgrade_profile_by_id(user_id):
    item = TABLE.get_item(Key={"pk": f"USER#{user_id}", "sk": "PROFILE"}).get("Item")
    if item and "familyId" in item:
        upgrade_single_family(item)


# ---------- public (signed-out) routes ----------

@app.get("/public/recipes", response_model=RecipeList, tags=["public"], dependencies=[Depends(anonymous)])
def public_list():
    return {"recipes": present(query_feed("PUBLIC"), None)}


@app.get("/public/recipes/{id}", response_model=Recipe, tags=["public"], dependencies=[Depends(anonymous)])
def public_get(id: str):
    return present([visible_recipe(id, None)], None)[0]


@app.get("/public/recipes/{id}/comments", response_model=CommentList, tags=["public"],
         dependencies=[Depends(anonymous)])
def public_comments(id: str):
    return comments_for(visible_recipe(id, None))


# Lambda entry point (handler = "main.handler").
handler = Mangum(app, lifespan="off")

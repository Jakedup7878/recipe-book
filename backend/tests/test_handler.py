import importlib
import json
import os
import sys

import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import devtools  # noqa: E402

devtools.use_fake_aws("recipes-test")
os.environ.pop("LOCAL_DEV_USER", None)


@pytest.fixture
def api():
    devtools.use_fake_aws("recipes-test")  # other test modules point TABLE_NAME elsewhere
    with mock_aws():
        devtools.create_table("recipes-test")
        import main
        yield importlib.reload(main)


class FakeLambdaContext:
    function_name = "recipe-api-test"
    aws_request_id = "test-request"


def call(api, route, user=None, body=None, ip="203.0.113.9", raw_body=None, **path):
    """Send an API Gateway HTTP API (payload 2.0) event through the Lambda handler."""
    method, template = route.split(" ", 1)
    raw_path = template.format(**path)
    event = {
        "version": "2.0",
        "routeKey": route,
        "rawPath": raw_path,
        "rawQueryString": "",
        "headers": {"host": "api.example.com", "content-type": "application/json"},
        "requestContext": {
            "http": {"method": method, "path": raw_path, "sourceIp": ip,
                     "protocol": "HTTP/1.1", "userAgent": "pytest"},
            "stage": "$default",
            "routeKey": route,
            "requestId": "test",
        },
        "pathParameters": path,
        "isBase64Encoded": False,
    }
    if user:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": {"sub": user, "email": f"{user}@example.com"}}}
    if body is not None or raw_body is not None:
        event["body"] = raw_body if raw_body is not None else json.dumps(body)
    response = api.handler(event, FakeLambdaContext())
    return response["statusCode"], json.loads(response.get("body") or "{}")


RECIPE = {"title": "Pancakes", "ingredients": ["flour", "milk"], "steps": ["mix", "fry"]}


def test_profile_created_on_first_call(api):
    status, me = call(api, "GET /me", "alice")
    assert status == 200 and me["displayName"] == "alice" and me["familyIds"] == []
    status, me = call(api, "PUT /me", "alice", {"displayName": "Alice W"})
    assert me["displayName"] == "Alice W"


def test_appearance_preferences_are_saved_to_the_account(api):
    assert call(api, "GET /me", "alice")[1]["preferences"] is None
    prefs = {"season": "fall", "mode": "dark", "font": "sans", "textSize": "large",
             "decorations": False, "accents": {"fall": "#B5542D", "winter": "#2f6690"}}
    status, me = call(api, "PUT /me/preferences", "alice", prefs)
    assert status == 200 and me["preferences"]["accents"]["fall"] == "#B5542D"
    saved = call(api, "GET /me", "alice")[1]["preferences"]
    assert saved["mode"] == "dark" and saved["accents"]["spring"] is None
    # Only known options and #rrggbb colors are accepted.
    assert call(api, "PUT /me/preferences", "alice", {"season": "monsoon"})[0] == 400
    assert call(api, "PUT /me/preferences", "alice", {"accents": {"fall": "red; background:url(x)"}})[0] == 400
    assert call(api, "PUT /me/preferences", "alice", {"extra": 1})[0] == 400


def test_location_preference_stores_only_the_hemisphere(api):
    status, me = call(api, "PUT /me/preferences", "alice", {"hemisphere": "south", "followLocation": True})
    assert status == 200 and me["preferences"]["hemisphere"] == "south" and me["preferences"]["followLocation"]
    assert call(api, "GET /me", "alice")[1]["preferences"]["hemisphere"] == "south"
    # Coordinates are never accepted, so they can't end up stored on the server.
    assert call(api, "PUT /me/preferences", "alice", {"hemisphere": "south", "latitude": -33.9})[0] == 400
    assert call(api, "PUT /me/preferences", "alice", {"hemisphere": "equator"})[0] == 400


def test_renamed_accounts_show_their_current_name_everywhere(api):
    _, recipe = call(api, "POST /recipes", "alice", RECIPE)
    rid = recipe["recipeId"]
    call(api, "POST /recipes/{id}/comments", "bob", {"text": "Great"}, id=rid)
    call(api, "PUT /me", "bob", {"displayName": "Bobby"})
    call(api, "PUT /me", "alice", {"displayName": "Chef Alice"})

    _, comments = call(api, "GET /recipes/{id}/comments", "alice", id=rid)
    assert [c["authorName"] for c in comments["comments"]] == ["Bobby"]
    assert call(api, "GET /public/recipes/{id}/comments", id=rid)[1]["comments"][0]["authorName"] == "Bobby"
    assert call(api, "GET /recipes/{id}", "bob", id=rid)[1]["authorName"] == "Chef Alice"
    assert call(api, "GET /recipes", "bob")[1]["public"][0]["authorName"] == "Chef Alice"
    assert call(api, "GET /public/recipes")[1]["recipes"][0]["authorName"] == "Chef Alice"


def test_sign_up_name_becomes_display_name(api):
    # The `name` chosen at sign-up arrives as a token claim; without one, the email prefix is used.
    assert api.get_or_create_profile({"sub": "dana", "email": "dana@example.com", "name": " Dana W "})[
        "displayName"] == "Dana W"
    assert api.get_or_create_profile({"sub": "eli", "email": "eli@example.com"})["displayName"] == "eli"


def test_signed_in_routes_reject_missing_claims(api):
    # API Gateway blocks these first; the app double-checks in case a route is misconfigured.
    status, body = call(api, "GET /me")
    assert status == 401 and body["message"] == "Sign in required."


def test_public_recipe_and_comments_from_other_account(api):
    status, recipe = call(api, "POST /recipes", "alice", RECIPE)
    assert status == 201 and recipe["visibility"] == "public"
    assert "pk" not in recipe  # internal keys are filtered out by the response model
    status, comment = call(api, "POST /recipes/{id}/comments", "bob", {"text": "Lovely!"}, id=recipe["recipeId"])
    assert status == 201 and comment["authorName"] == "bob"
    _, listed = call(api, "GET /public/recipes/{id}/comments", id=recipe["recipeId"])
    assert [c["text"] for c in listed["comments"]] == ["Lovely!"]
    # Bob cannot edit Alice's recipe, but Alice can remove Bob's comment on it.
    assert call(api, "PUT /recipes/{id}", "bob", RECIPE, id=recipe["recipeId"])[0] == 403
    assert call(api, "DELETE /recipes/{id}/comments/{commentId}", "alice",
                id=recipe["recipeId"], commentId=comment["commentId"])[0] == 204


def test_family_only_recipes_are_private_to_the_family(api):
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    code = family["inviteCode"]
    status, joined = call(api, "POST /families/join", "bob", {"inviteCode": code.lower()})
    assert status == 200 and {m["userId"] for m in joined["members"]} == {"alice", "bob"}
    assert joined["inviteCode"] is None  # only the owner sees the code

    _, secret = call(api, "POST /recipes", "alice", {**RECIPE, "visibility": "family"})
    rid = secret["recipeId"]
    assert call(api, "GET /recipes/{id}", "bob", id=rid)[0] == 200
    assert call(api, "GET /recipes/{id}", "carol", id=rid)[0] == 404
    assert call(api, "GET /public/recipes/{id}", id=rid)[0] == 404
    assert call(api, "POST /recipes/{id}/comments", "carol", {"text": "hi"}, id=rid)[0] == 404
    _, public = call(api, "POST /recipes", "carol", RECIPE)
    _, feed = call(api, "GET /recipes", "bob")
    assert [r["recipeId"] for r in feed["family"]] == [rid]
    assert [r["recipeId"] for r in feed["public"]] == [public["recipeId"]]
    _, carol_feed = call(api, "GET /recipes", "carol")
    assert carol_feed["family"] == [] and len(carol_feed["public"]) == 1


def test_family_membership_rules(api):
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    fid = family["familyId"]
    call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})
    assert call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})[0] == 409
    assert call(api, "POST /families/join", "carol", {"inviteCode": "NOPE1234"})[0] == 404

    # Rotating the code invalidates the old one.
    _, rotated = call(api, "POST /families/{familyId}/invite-code", "alice", familyId=fid)
    assert call(api, "POST /families/join", "carol", {"inviteCode": family["inviteCode"]})[0] == 404
    assert call(api, "POST /families/{familyId}/invite-code", "bob", familyId=fid)[0] == 403
    assert call(api, "POST /families/{familyId}/invite-code", "carol", familyId=fid)[0] == 404

    # Owner leaving hands ownership to Bob; last member leaving deletes the family.
    status, left = call(api, "POST /families/{familyId}/leave", "alice", familyId=fid)
    assert status == 200 and left["families"] == []
    _, mine = call(api, "GET /families/mine", "bob")
    assert mine["families"][0]["ownerId"] == "bob" and len(mine["families"][0]["members"]) == 1
    call(api, "POST /families/{familyId}/leave", "bob", familyId=fid)
    assert call(api, "POST /families/join", "carol", {"inviteCode": rotated["inviteCode"]})[0] == 404


def test_owner_can_remove_member(api):
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    fid = family["familyId"]
    call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})
    assert call(api, "DELETE /families/{familyId}/members/{userId}", "bob", familyId=fid, userId="alice")[0] == 403
    status, after = call(api, "DELETE /families/{familyId}/members/{userId}", "alice", familyId=fid, userId="bob")
    assert status == 200 and [m["userId"] for m in after["members"]] == ["alice"]
    assert call(api, "GET /me", "bob")[1]["familyIds"] == []


def test_accounts_can_join_several_families(api, monkeypatch):
    # Timestamps have one-second resolution; tick a fake clock so "newest first" is well defined.
    ticks = iter(range(10, 60))
    monkeypatch.setattr(api, "now_iso", lambda: f"2026-10-25T12:00:{next(ticks)}+00:00")
    _, washingtons = call(api, "POST /families", "alice", {"name": "Washingtons"})
    _, riveras = call(api, "POST /families", "carol", {"name": "Riveras"})
    for family in (washingtons, riveras):
        assert call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})[0] == 200
    # A member can also start a family of their own.
    assert call(api, "POST /families", "bob", {"name": "Bob's Bakes"})[0] == 201

    _, mine = call(api, "GET /families/mine", "bob")
    assert [f["name"] for f in mine["families"]] == ["Bob's Bakes", "Riveras", "Washingtons"]
    assert sorted(call(api, "GET /me", "bob")[1]["familyIds"]) == sorted(f["familyId"] for f in mine["families"])

    # In one family, a family recipe goes to it; Bob sees both on his home feed, newest first.
    _, pie = call(api, "POST /recipes", "alice", {**RECIPE, "title": "Pie", "visibility": "family"})
    _, tamales = call(api, "POST /recipes", "carol", {**RECIPE, "title": "Tamales", "visibility": "family"})
    assert pie["familyIds"] == [washingtons["familyId"]] and tamales["familyIds"] == [riveras["familyId"]]
    _, feed = call(api, "GET /recipes", "bob")
    assert [r["title"] for r in feed["family"]] == ["Tamales", "Pie"]
    assert [r["title"] for r in call(api, "GET /recipes", "alice")[1]["family"]] == ["Pie"]
    assert call(api, "GET /recipes/{id}", "alice", id=tamales["recipeId"])[0] == 404

    # One family's page lists only its recipes, and only for members.
    _, listed = call(api, "GET /families/{familyId}/recipes", "bob", familyId=riveras["familyId"])
    assert [r["title"] for r in listed["recipes"]] == ["Tamales"]
    assert call(api, "GET /families/{familyId}/recipes", "alice", familyId=riveras["familyId"])[0] == 404

    # In several families, a family recipe must say which ones; they must be yours.
    family_recipe = {**RECIPE, "visibility": "family"}
    assert call(api, "POST /recipes", "bob", family_recipe)[0] == 400
    assert call(api, "POST /recipes", "bob", {**family_recipe, "familyIds": ["not-mine"]})[0] == 400
    assert call(api, "POST /recipes", "bob", {**family_recipe, "familyIds": []})[0] == 400
    status, shared = call(api, "POST /recipes", "bob", {**family_recipe, "familyIds": [riveras["familyId"]]})
    assert status == 201 and shared["familyIds"] == [riveras["familyId"]]
    # The older one-family field still works.
    status, older = call(api, "POST /recipes", "bob", {**family_recipe, "familyId": riveras["familyId"]})
    assert status == 201 and older["familyIds"] == [riveras["familyId"]]
    call(api, "DELETE /recipes/{id}", "bob", id=older["recipeId"])
    # Editing it without naming families keeps it where it is.
    _, edited = call(api, "PUT /recipes/{id}", "bob", {**family_recipe, "title": "Better"}, id=shared["recipeId"])
    assert edited["familyIds"] == [riveras["familyId"]] and edited["title"] == "Better"

    # Leaving one family keeps the others.
    call(api, "POST /families/{familyId}/leave", "bob", familyId=washingtons["familyId"])
    assert [r["title"] for r in call(api, "GET /recipes", "bob")[1]["family"]] == ["Better", "Tamales"]


def test_family_limit_per_account(api, monkeypatch):
    monkeypatch.setattr(api, "MAX_FAMILIES", 2)
    call(api, "POST /families", "bob", {"name": "One"})
    _, two = call(api, "POST /families", "alice", {"name": "Two"})
    call(api, "POST /families/join", "bob", {"inviteCode": two["inviteCode"]})
    assert call(api, "POST /families", "bob", {"name": "Three"})[0] == 409
    _, three = call(api, "POST /families", "carol", {"name": "Three"})
    status, body = call(api, "POST /families/join", "bob", {"inviteCode": three["inviteCode"]})
    assert status == 409 and "up to 2" in body["message"]


def test_owner_can_rename_family(api):
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    fid = family["familyId"]
    call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})
    status, renamed = call(api, "PUT /families/{familyId}", "alice", {"name": "  The Washington Kitchen "}, familyId=fid)
    assert status == 200 and renamed["name"] == "The Washington Kitchen"
    assert call(api, "GET /families/mine", "bob")[1]["families"][0]["name"] == "The Washington Kitchen"
    assert call(api, "PUT /families/{familyId}", "bob", {"name": "Bob's"}, familyId=fid)[0] == 403
    assert call(api, "PUT /families/{familyId}", "carol", {"name": "Carol's"}, familyId=fid)[0] == 404
    assert call(api, "PUT /families/{familyId}", "alice", {"name": "   "}, familyId=fid)[0] == 400


def test_invite_preview(api):
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    code = family["inviteCode"]
    status, preview = call(api, "GET /families/invite/{code}", "bob", code=code.lower())
    assert status == 200 and preview == {"familyId": family["familyId"], "name": "Washingtons",
                                         "memberCount": 1, "alreadyMember": False}
    assert call(api, "GET /families/invite/{code}", "alice", code=code)[1]["alreadyMember"] is True
    assert call(api, "GET /families/invite/{code}", "bob", code="NOPE1234")[0] == 404
    assert call(api, "GET /families/invite/{code}", code=code)[0] == 401


def test_author_can_publish_a_family_recipe(api):
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})
    _, pie = call(api, "POST /recipes", "alice", {**RECIPE, "visibility": "family"})
    rid = pie["recipeId"]
    assert call(api, "PUT /recipes/{id}", "bob", {**RECIPE, "visibility": "public"}, id=rid)[0] == 403

    status, published = call(api, "PUT /recipes/{id}", "alice", {**RECIPE, "visibility": "public"}, id=rid)
    assert status == 200 and published["visibility"] == "public" and published["familyIds"] == []
    assert [r["recipeId"] for r in call(api, "GET /public/recipes")[1]["recipes"]] == [rid]
    _, feed = call(api, "GET /recipes", "bob")
    assert feed["family"] == [] and [r["recipeId"] for r in feed["public"]] == [rid]
    assert call(api, "GET /families/{familyId}/recipes", "bob", familyId=family["familyId"])[1]["recipes"] == []


def test_author_shares_a_recipe_with_several_families(api, monkeypatch):
    ticks = iter(range(10, 60))
    monkeypatch.setattr(api, "now_iso", lambda: f"2026-10-27T12:00:{next(ticks)}+00:00")
    _, washingtons = call(api, "POST /families", "alice", {"name": "Washingtons"})
    _, riveras = call(api, "POST /families", "carol", {"name": "Riveras"})
    _, bakers = call(api, "POST /families", "dana", {"name": "Bakers"})
    w, r, b = washingtons["familyId"], riveras["familyId"], bakers["familyId"]
    for code in (riveras["inviteCode"], bakers["inviteCode"]):
        call(api, "POST /families/join", "alice", {"inviteCode": code})
    call(api, "POST /families/join", "bob", {"inviteCode": washingtons["inviteCode"]})
    call(api, "POST /families/join", "bob", {"inviteCode": riveras["inviteCode"]})

    _, pie = call(api, "POST /recipes", "alice", {**RECIPE, "title": "Pie", "visibility": "family", "familyIds": [w]})
    rid = pie["recipeId"]
    sharing = lambda user, body: call(api, "PUT /recipes/{id}/sharing", user, body, id=rid)  # noqa: E731

    # Only the author can share, and only with families they're in.
    assert sharing("bob", {"visibility": "family", "familyIds": [w, r]})[0] == 403
    assert sharing("alice", {"visibility": "family", "familyIds": [w, "not-mine"]})[0] == 400
    assert sharing("alice", {"visibility": "family", "familyIds": []})[0] == 400
    status, shared = sharing("alice", {"visibility": "family", "familyIds": [w, r]})
    assert status == 200 and shared["familyIds"] == sorted([w, r])

    # Each family sees it, and Bob (in both) gets it once on his home feed.
    for family_id in (w, r):
        listed = call(api, "GET /families/{familyId}/recipes", "bob", familyId=family_id)[1]["recipes"]
        assert [x["recipeId"] for x in listed] == [rid]
    assert [x["recipeId"] for x in call(api, "GET /recipes", "bob")[1]["family"]] == [rid]
    assert call(api, "GET /recipes/{id}", "carol", id=rid)[0] == 200  # a Rivera, not a Washington
    assert call(api, "GET /recipes/{id}", "dana", id=rid)[0] == 404  # Bakers weren't picked
    assert call(api, "GET /families/{familyId}/recipes", "dana", familyId=b)[1]["recipes"] == []

    # Members only learn about their own families; the author sees them all.
    assert call(api, "GET /recipes/{id}", "carol", id=rid)[1]["familyIds"] == [r]
    assert call(api, "GET /recipes/{id}", "bob", id=rid)[1]["familyIds"] == sorted([w, r])
    assert call(api, "GET /recipes/{id}", "alice", id=rid)[1]["familyIds"] == sorted([w, r])

    # Unsharing removes it from that family's feed.
    sharing("alice", {"visibility": "family", "familyIds": [r]})
    assert call(api, "GET /families/{familyId}/recipes", "bob", familyId=w)[1]["recipes"] == []
    assert call(api, "GET /recipes/{id}", "carol", id=rid)[0] == 200
    stored = {i["sk"] for i in api.TABLE.query(KeyConditionExpression=api.Key("pk").eq(f"RECIPE#{rid}"))["Items"]}
    assert stored == {"META", f"SHARE#{r}"}

    # After the author leaves a family, the share can stay or be dropped, but not be added back.
    call(api, "POST /families/{familyId}/leave", "alice", familyId=r)
    assert sharing("alice", {"visibility": "family", "familyIds": [r, b]})[1]["familyIds"] == sorted([r, b])
    assert sharing("alice", {"visibility": "family", "familyIds": [b]})[1]["familyIds"] == [b]
    assert sharing("alice", {"visibility": "family", "familyIds": [r]})[0] == 400

    # Sharing to public takes it out of every family feed and into the public one.
    status, public = sharing("alice", {"visibility": "public"})
    assert status == 200 and public["visibility"] == "public" and public["familyIds"] == []
    assert call(api, "GET /families/{familyId}/recipes", "dana", familyId=b)[1]["recipes"] == []
    assert [x["recipeId"] for x in call(api, "GET /public/recipes")[1]["recipes"]] == [rid]
    # Deleting removes the recipe with its comments and shares.
    sharing("alice", {"visibility": "family", "familyIds": [w, b]})
    call(api, "POST /recipes/{id}/comments", "bob", {"text": "Yum"}, id=rid)
    assert call(api, "DELETE /recipes/{id}", "alice", id=rid)[0] == 204
    assert api.TABLE.query(KeyConditionExpression=api.Key("pk").eq(f"RECIPE#{rid}"))["Items"] == []


def test_my_recipes_lists_everything_i_wrote(api, monkeypatch):
    ticks = iter(range(10, 60))
    monkeypatch.setattr(api, "now_iso", lambda: f"2026-10-27T12:00:{next(ticks)}+00:00")
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})
    _, soup = call(api, "POST /recipes", "alice", {**RECIPE, "title": "Soup"})
    _, pie = call(api, "POST /recipes", "alice", {**RECIPE, "title": "Pie", "visibility": "family"})
    call(api, "POST /recipes", "bob", {**RECIPE, "title": "Bob's bread"})
    call(api, "POST /recipes/{id}/comments", "alice", {"text": "Mine!"}, id=soup["recipeId"])  # not a recipe

    status, mine = call(api, "GET /me/recipes", "alice")
    assert status == 200
    assert [(x["title"], x["visibility"]) for x in mine["recipes"]] == [("Pie", "family"), ("Soup", "public")]
    assert mine["recipes"][0]["familyIds"] == [family["familyId"]]
    # Still listed after leaving the family it was shared with.
    call(api, "POST /families/{familyId}/leave", "alice", familyId=family["familyId"])
    assert [x["recipeId"] for x in call(api, "GET /me/recipes", "alice")[1]["recipes"]] == [pie["recipeId"],
                                                                                          soup["recipeId"]]
    assert [x["title"] for x in call(api, "GET /me/recipes", "bob")[1]["recipes"]] == ["Bob's bread"]
    assert call(api, "GET /me/recipes", "carol")[1]["recipes"] == []


def test_drafts_are_private_to_their_author(api):
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})

    # A draft needs only a title.
    status, draft = call(api, "POST /recipes", "alice", {"title": "Gumbo", "visibility": "draft"})
    assert status == 201 and draft["visibility"] == "draft"
    assert draft["ingredients"] == [] and draft["steps"] == [] and draft["familyIds"] == []
    assert call(api, "POST /recipes", "alice", {"title": " ", "visibility": "draft"})[0] == 400
    gumbo = draft["recipeId"]

    # Only its author can see it: not a family member, not a signed-out visitor, and it's in no feed.
    assert call(api, "GET /recipes/{id}", "alice", id=gumbo)[0] == 200
    assert call(api, "GET /recipes/{id}", "bob", id=gumbo)[0] == 404
    assert call(api, "GET /recipes/{id}/comments", "bob", id=gumbo)[0] == 404
    assert call(api, "GET /public/recipes/{id}", id=gumbo)[0] == 404
    assert call(api, "GET /public/recipes/{id}/comments", id=gumbo)[0] == 404
    for user in ("alice", "bob"):
        assert call(api, "GET /recipes", user)[1] == {"family": [], "public": []}
    assert call(api, "GET /public/recipes")[1]["recipes"] == []
    assert call(api, "GET /families/{familyId}/recipes", "bob", familyId=family["familyId"])[1]["recipes"] == []
    mine = call(api, "GET /me/recipes", "alice")[1]["recipes"]
    assert [(x["title"], x["visibility"]) for x in mine] == [("Gumbo", "draft")]
    assert call(api, "GET /me/recipes", "bob")[1]["recipes"] == []

    # Nobody else can change, share or delete it, and nobody comments on a draft.
    assert call(api, "PUT /recipes/{id}", "bob", {**RECIPE, "visibility": "draft"}, id=gumbo)[0] == 403
    assert call(api, "PUT /recipes/{id}/sharing", "bob", {"visibility": "public"}, id=gumbo)[0] == 403
    assert call(api, "DELETE /recipes/{id}", "bob", id=gumbo)[0] == 403
    assert call(api, "POST /recipes/{id}/comments", "bob", {"text": "hi"}, id=gumbo)[0] == 404
    status, body = call(api, "POST /recipes/{id}/comments", "alice", {"text": "note to self"}, id=gumbo)
    assert status == 400 and "Share" in body["message"]

    # Saving it as a draft again keeps it private, with what's written so far.
    status, draft = call(api, "PUT /recipes/{id}", "alice",
                         {"title": "Gumbo", "ingredients": ["okra"], "visibility": "draft"}, id=gumbo)
    assert status == 200 and draft["visibility"] == "draft" and draft["ingredients"] == ["okra"]

    # It can't be shared, from the form or the recipe page, until it has ingredients and steps.
    status, body = call(api, "PUT /recipes/{id}", "alice",
                        {"title": "Gumbo", "ingredients": ["okra"], "visibility": "public"}, id=gumbo)
    assert status == 400 and "steps" in body["message"]
    status, body = call(api, "PUT /recipes/{id}/sharing", "alice", {"visibility": "public"}, id=gumbo)
    assert status == 400 and "steps" in body["message"]
    assert call(api, "GET /recipes/{id}", "alice", id=gumbo)[1]["visibility"] == "draft"
    assert call(api, "GET /public/recipes")[1]["recipes"] == []

    # Its author can delete it.
    assert call(api, "DELETE /recipes/{id}", "alice", id=gumbo)[0] == 204
    assert call(api, "GET /me/recipes", "alice")[1]["recipes"] == []


def test_sharing_a_draft_publishes_it_as_a_new_recipe(api, monkeypatch):
    ticks = iter(range(10, 60))
    monkeypatch.setattr(api, "now_iso", lambda: f"2026-11-11T12:00:{next(ticks)}+00:00")
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})
    _, gumbo = call(api, "POST /recipes", "alice", {**RECIPE, "title": "Gumbo", "visibility": "draft"})
    _, pie = call(api, "POST /recipes", "alice", {"title": "Pie", "visibility": "draft"})
    _, bread = call(api, "POST /recipes", "bob", {**RECIPE, "title": "Bread"})  # written after both drafts

    # From the recipe form: saving a draft as public. It's dated from now, so it tops the feed.
    status, shared = call(api, "PUT /recipes/{id}", "alice", {**RECIPE, "title": "Gumbo"}, id=gumbo["recipeId"])
    assert status == 200 and shared["visibility"] == "public"
    assert shared["createdAt"] > bread["createdAt"] > gumbo["createdAt"]
    assert shared["updatedAt"] == shared["createdAt"]
    gumbo_shared_at = shared["createdAt"]
    assert [r["title"] for r in call(api, "GET /public/recipes")[1]["recipes"]] == ["Gumbo", "Bread"]
    assert call(api, "POST /recipes/{id}/comments", "bob", {"text": "Yum"}, id=gumbo["recipeId"])[0] == 201

    # From the recipe page: finish the draft, then Share to Family.
    call(api, "PUT /recipes/{id}", "alice", {**RECIPE, "title": "Pie", "visibility": "draft"}, id=pie["recipeId"])
    status, shared = call(api, "PUT /recipes/{id}/sharing", "alice", {"visibility": "family"}, id=pie["recipeId"])
    assert status == 200 and shared["visibility"] == "family" and shared["familyIds"] == [family["familyId"]]
    assert shared["createdAt"] > pie["createdAt"]
    feed = call(api, "GET /recipes", "bob")[1]
    assert [r["title"] for r in feed["family"]] == ["Pie"] and [r["title"] for r in feed["public"]] == ["Gumbo", "Bread"]
    assert [(x["title"], x["visibility"]) for x in call(api, "GET /me/recipes", "alice")[1]["recipes"]] == [
        ("Pie", "family"), ("Gumbo", "public")]

    # Editing a shared recipe keeps its date, and it can't go back to being a draft.
    _, edited = call(api, "PUT /recipes/{id}", "alice", {**RECIPE, "title": "Gumbo!"}, id=gumbo["recipeId"])
    assert edited["createdAt"] == gumbo_shared_at and edited["updatedAt"] > gumbo_shared_at
    status, error = call(api, "PUT /recipes/{id}", "alice", {**RECIPE, "visibility": "draft"}, id=gumbo["recipeId"])
    assert status == 400 and "draft" in error["message"]
    assert call(api, "PUT /recipes/{id}/sharing", "alice", {"visibility": "draft"}, id=gumbo["recipeId"])[0] == 400
    status, still = call(api, "GET /public/recipes/{id}", id=gumbo["recipeId"])
    assert status == 200 and still["visibility"] == "public"


def test_recipes_saved_with_one_family_still_work_and_upgrade(api):
    # Before recipes could go to several families, a family recipe held one familyId and
    # sat in that family's feed itself (gsi1pk = FAMILYFEED#<id>), with no SHARE item.
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    fid = family["familyId"]
    call(api, "POST /families/join", "bob", {"inviteCode": family["inviteCode"]})
    _, other = call(api, "POST /families", "alice", {"name": "Riveras"})
    api.TABLE.put_item(Item={
        "pk": "RECIPE#old", "sk": "META", "recipeId": "old", **RECIPE, "description": "",
        "visibility": "family", "familyId": fid, "authorId": "alice", "authorName": "alice",
        "createdAt": "2026-10-01T00:00:00+00:00", "updatedAt": "2026-10-01T00:00:00+00:00",
        "gsi1pk": f"FAMILYFEED#{fid}", "gsi1sk": "2026-10-01T00:00:00+00:00#old"})

    assert [x["recipeId"] for x in call(api, "GET /families/{familyId}/recipes", "bob", familyId=fid)[1]["recipes"]] \
        == ["old"]
    assert call(api, "GET /recipes/{id}", "bob", id="old")[1]["familyIds"] == [fid]
    assert [x["recipeId"] for x in call(api, "GET /me/recipes", "alice")[1]["recipes"]] == ["old"]

    # Editing keeps it in its family and moves it to the new layout.
    _, edited = call(api, "PUT /recipes/{id}", "alice", {**RECIPE, "title": "Old pie", "visibility": "family"},
                     id="old")
    assert edited["familyIds"] == [fid]
    stored = api.TABLE.get_item(Key={"pk": "RECIPE#old", "sk": "META"})["Item"]
    assert "familyId" not in stored and "gsi1pk" not in stored and stored["familyIds"] == {fid}
    assert api.TABLE.get_item(Key={"pk": "RECIPE#old", "sk": f"SHARE#{fid}"})["Item"]["gsi1pk"] == \
        f"FAMILYFEED#{fid}"
    assert [x["title"] for x in call(api, "GET /recipes", "bob")[1]["family"]] == ["Old pie"]
    # Sharing it with a second family works like any other recipe.
    _, shared = call(api, "PUT /recipes/{id}/sharing", "alice",
                     {"visibility": "family", "familyIds": [fid, other["familyId"]]}, id="old")
    assert shared["familyIds"] == sorted([fid, other["familyId"]])


def test_single_family_profiles_are_upgraded(api):
    # Profiles written before multiple families held one familyId attribute.
    _, family = call(api, "POST /families", "alice", {"name": "Washingtons"})
    fid = family["familyId"]
    _, pie = call(api, "POST /recipes", "alice", {**RECIPE, "visibility": "family"})
    for user in ("bob", "carol"):
        api.TABLE.put_item(Item={"pk": f"USER#{user}", "sk": "PROFILE", "userId": user, "email": f"{user}@x.com",
                                 "displayName": user, "createdAt": "2026-10-01T00:00:00+00:00", "familyId": fid})
        api.TABLE.put_item(Item=api.member_item(fid, {"userId": user, "displayName": user}, "2026-10-01"))

    status, me = call(api, "GET /me", "bob")
    assert status == 200 and me["familyIds"] == [fid]
    stored = api.TABLE.get_item(Key={"pk": "USER#bob", "sk": "PROFILE"})["Item"]
    assert "familyId" not in stored and stored["familyIds"] == {fid}
    assert call(api, "GET /recipes/{id}", "bob", id=pie["recipeId"])[0] == 200

    # The owner can remove a member whose profile hasn't been upgraded yet.
    status, after = call(api, "DELETE /families/{familyId}/members/{userId}", "alice", familyId=fid, userId="carol")
    assert status == 200 and "carol" not in [m["userId"] for m in after["members"]]
    assert call(api, "GET /me", "carol")[1]["familyIds"] == []


def test_validation_errors(api):
    status, body = call(api, "POST /recipes", "alice", {"title": "x"})
    assert status == 400 and "ingredients" in body["message"]
    assert call(api, "POST /recipes", "alice", {**RECIPE, "ingredients": ["  ", ""]})[0] == 400
    assert call(api, "POST /recipes", "alice", {**RECIPE, "visibility": "family"})[0] == 400  # not in a family
    assert call(api, "POST /recipes", "alice", {**RECIPE, "visibility": "secret"})[0] == 400
    assert call(api, "POST /recipes", "alice", {**RECIPE, "title": "x" * 500})[0] == 400
    assert call(api, "POST /recipes", "alice", raw_body="not json")[0] == 400


def test_blank_lines_are_dropped(api):
    _, recipe = call(api, "POST /recipes", "alice", {**RECIPE, "ingredients": [" flour ", "", "milk"]})
    assert recipe["ingredients"] == ["flour", "milk"]


def test_rate_limit_returns_429(api):
    limit = api.ratelimit.LIMITS["comment"][0]
    _, recipe = call(api, "POST /recipes", "alice", RECIPE)
    statuses = [call(api, "POST /recipes/{id}/comments", "bob", {"text": "spam"}, id=recipe["recipeId"])[0]
                for _ in range(limit + 1)]
    assert statuses[:limit] == [201] * limit and statuses[-1] == 429
    # Limits are per account: Carol is unaffected.
    assert call(api, "POST /recipes/{id}/comments", "carol", {"text": "hi"}, id=recipe["recipeId"])[0] == 201


def test_anonymous_limit_is_per_ip(api):
    limit = api.ratelimit.LIMITS["anonymous"][0]
    statuses = [call(api, "GET /public/recipes")[0] for _ in range(limit + 1)]
    assert statuses[-1] == 429
    assert call(api, "GET /public/recipes", ip="198.51.100.7")[0] == 200

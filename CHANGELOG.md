# Changelog

Notable changes to Recipe Book. Newest first.

## 2026-10-25 (evening): Multiple families, invite links, renaming, publishing

### What changed for people using the site

1. **Join more than one family.** An account can now be in up to **10 families**.
   - The **Family** page lists your families as cards, showing the name, member count, who's in it and an Owner badge. Below the cards are **Join a family** and **Start a family** forms, which are always there until you reach the limit.
   - Click a family to open its own page (`#/family/<id>`), with its recipes, members, invite code and **Leave family**. Leaving one family keeps you in the others.
2. **New family recipes from all your families appear on Home.**
   - The Home page's "Family recipes" section now combines the recipes of every family you're in, newest first.
   - If you're in more than one family, each card shows which family it's from, and the heading says "From your N families".
   - Before, an account could only see one family's recipes. In the live data, members of the second family couldn't see the first family's recipe.
3. **One-click copying and invite links** (family owners).
   - Click the invite code to copy it. **Copy invite link** copies a link like `https://familyfoodiebook.com/#/join/ABCD2345`. On phones, **Share…** opens the system share sheet.
   - Clicking **Make a new code** also copies the new code. A message under the buttons confirms the copy, or shows the text to copy by hand if the browser blocked it.
   - Opening an invite link shows **"Join *Family name*?"** with the member count. If you're signed out, you go to sign in or create an account first, then come back to the invite. If you're already a member, you go straight to the family.
   - The **Join a family** form accepts a pasted invite link as well as a bare code.
4. **Owners can rename a family.** Click **Rename** next to the family name on its page, edit it, then Save or Cancel. Every member sees the new name.
5. **Publish a family recipe to everyone.**
   - On a family-only recipe you wrote, **Publish to public** moves it to Public recipes, after a confirmation that explains what changes. Your family can still see it, now with everyone else.
   - You can make it family-only again with **Edit**.
   - It's a move, not a copy, so comments stay in one place.

Also:
- **Choosing a family in the recipe form.** "Who can see it" now lists each of your families ("Family only: The Washingtons", …).
  - **Share a family recipe** on a family's page preselects that family (`#/new/family/<id>`).
  - Editing a recipe keeps it in its current family unless you change the choice.
- **Bug fix: a slow page could draw over a newer one.**
  - If you left a page while it was still loading (say, Home after signing out) and the next page drew quickly (say, sign in), the slow page could finish loading and replace it.
  - Every page change now has a number, and data loaded for an older page is thrown away (`routeSeq` and `StaleRoute` in `app.js`). This bug predates today's changes.

### API and data changes (`backend/recipe_api/main.py`, `infra/recipe_stack.py`)

| Before | Now |
|---|---|
| Profile `familyId` (one family) | Profile `familyIds` (a DynamoDB string set, up to `MAX_FAMILIES` = 10) |
| `GET /families/mine` → `{family}` | `GET /families/mine` → `{families: [...]}`, sorted by name |
| `POST /families/leave` | `POST /families/{familyId}/leave` |
| `POST /families/mine/invite-code` | `POST /families/{familyId}/invite-code` |
| `DELETE /families/mine/members/{userId}` | `DELETE /families/{familyId}/members/{userId}` |
| — | `PUT /families/{familyId}`: rename (owner only) |
| — | `GET /families/{familyId}/recipes`: one family's recipes (members only) |
| — | `GET /families/invite/{code}`: what an invite is for: name, member count, whether you're already in it (signed in only, so codes can't be probed anonymously) |
| Recipe body: `visibility` | Recipe body: `visibility` plus optional `familyId`. It's required for a family recipe when you're in more than one family, and must be one of yours. |
| `GET /recipes` `family`: one family's recipes | `family`: all your families' recipes merged, newest first (at most 50) |

- `POST /families` and `POST /families/join` no longer refuse people who are already in a family. They refuse with 409 if you're already in *that* family or at the limit. The rule is enforced in the same DynamoDB transaction that adds you, so two requests at once can't get past it.
- Publishing uses the existing `PUT /recipes/{id}` with `visibility: "public"`. Only the author can do it, as with any edit.

### Existing data is upgraded automatically

The live table already had accounts and families stored with the old single `familyId`. No migration script is needed:
- The first time an old profile is read, one conditional DynamoDB update moves the value into the new set: `ADD familyIds :s REMOVE familyId`, only `if attribute_exists(familyId)`. If two requests race, the second just re-reads the upgraded profile.
- An owner removing a member whose profile hasn't been upgraded yet upgrades that profile first.
- Families, members, invite codes and family recipes were already stored per family, so they're unchanged.

### Tests

- New backend tests in `backend/tests/test_handler.py`:
  - joining several families, and starting one while in another
  - a merged Home feed, newest first, and one family's recipe page (members only)
  - choosing a family for a recipe, and keeping its family on edit
  - the 10-family limit
  - rename (owner only, blank names rejected)
  - invite preview (including "already a member" and signed-out → 401)
  - publishing a family recipe
  - upgrading old single-family profiles, including removing a member who hasn't been upgraded
- Updated tests for the new routes and response shapes. The local sample data now has a second family, so alice is in two families.
- Full suite: 37 passed.
- Manual checks on the local dev server, using existing local data that still had old single-family profiles (they upgraded on first sign-in):
  - Start a family; copy the code and invite link.
  - Open the invite link signed out → sign in → "Join The Rivera Kitchen?" → join.
  - Home shows both families with family badges. A new family recipe shows up there first.
  - Publish to public; rename and cancel. Non-owners don't see Rename or the invite code.
  - Phone width in light mode, with no sideways scrolling.
- The in-app test browser blocks the clipboard. There the Clipboard API is refused and the fallback copy reports success, but the copied text couldn't be pasted back to confirm it. Check copying in a normal browser after deploying.

### Deploying

- This change includes the API (Lambda code and API Gateway routes), not only the site files. Expect the pipeline's diff to show the Lambda function updating, the 3 old `/families/mine/...` and `/families/leave` routes being removed, and 6 new routes being added.
- For a minute or so during the deploy, a browser still running the old page may hit a removed route and see an error. Refreshing the page fixes it.

## 2026-10-25 (later): Clickable recipe cards, "Family Foodie Book" name

### What changed

- **The whole recipe card is clickable** (`frontend/styles.css`), on Home and on the Family page. Before, only the title opened the recipe.
  - This is done in CSS with a "stretched link": the title link's `::after` covers the card. Each card is still **one** link, named by the recipe title, so screen readers and the Tab key behave the same as before. A JavaScript click handler on the card would have made it clickable for the mouse only.
  - Hovering anywhere on a card highlights its title in the accent color. When a card's link has keyboard focus, the focus outline goes around the whole card.
  - Trade-off: you can't select a card's text with the mouse any more, because clicks go to the link. The full text is on the recipe page.
- **The site is now called "Family Foodie Book"** (`frontend/index.html`, `frontend/app.js`), to match familyfoodiebook.com.
  - It's in the top-left of the header and in the browser tab title.
  - It's also in the text that used the old name: "Sign in to your … account", "Welcome to …, *name*!" after signing up, and "Change how … looks" on the profile page.
  - Not renamed: code comments, the repository and folder names, and AWS resource names such as the `RecipeBook` stack and `recipe-book` pipeline. Renaming those would replace live resources for no visible benefit. Cognito's sign-up emails don't include the site name, so they needed no change.

### Tests

- Full suite: 31 passed.
- Local dev server checks:
  - Clicking an empty corner of a card opens that recipe.
  - Every sampled point on a card lands on the one recipe link.
  - Tab focus outlines the whole card.
  - The new name fits on one line at 375px with no sideways scrolling.

## 2026-10-25: Simpler navigation, account menu, family recipes

### What changed

**Top navigation bar** (`frontend/app.js` `renderNav`, `accountMenu`; `frontend/styles.css`)

| Signed in, before | Signed in, now |
|---|---|
| Recipes · New recipe · Family · Appearance · *(your name)* · Sign out | Home · Family · *(avatar + your name ▾)* |

| Signed out, before | Signed out, now |
|---|---|
| Recipes · Appearance · Sign in · Create account | Home · Sign in · Create account (Appearance link in the page footer) |

- **"Recipes" is now "Home".** It's still the page that shows everything you can see: your family's recipes, then public recipes. Its heading is now "Home".
- **New account menu.** Clicking your avatar/name opens a menu with your name and email, **Your profile**, **Appearance** and **Sign out**. The button is highlighted when you're on the profile or appearance page.
  - It's a disclosure (a button that shows a list of links), so Tab and Enter work as usual. **Escape** closes it and returns focus to the button. Clicking anywhere else or moving focus out of it also closes it.
  - On phones (560px wide or less), only the avatar shows, and the menu opens across the full width under the header.
- **"New recipe" left the top bar.** It's still a button at the top of Home, and the Family page has its own **Share a family recipe** button. That button opens the new-recipe form with "Family only" already selected (`#/new/family`).
- **Appearance for signed-out visitors** moved to a small footer link, since there's no account menu without an account. Signed-in users find it in the account menu.

**Family page** (`frontend/app.js` `familyView`)

- It now lists the family's **family-only recipes** (the same list as the "Family recipes" section on Home), with a count, followed by the members. An empty family shows a "Share the first one" button.
- The header shows the member count and a **Share a family recipe** button.
- The member list marks **You** next to your own name, as well as **Owner**.
- The family recipes come from the existing `GET /recipes` endpoint (its `family` list), so there are no backend or API Gateway changes.

### Bug fix: the word "null" on the Family page

**Symptom:** after someone joined a family, the word `null` appeared under the family name on the Family page.

**Cause:** `render()` passed its arguments straight to the browser's `replaceChildren()`. The Family page passes `null` in place of the invite-code card for anyone who isn't the family owner (only the owner gets the code). `replaceChildren(null)` doesn't skip `null`: it inserts it as the text "null". So every non-owner member saw it, which is why it showed up right after joining. The same bug printed "null" on any recipe page whose recipe had no description.

**Fix:** `render()` now drops `null` and `false` arguments, the same way the `h()` element helper already did for children. The backend was never involved; the API data was correct.

### Tests

- New `backend/tests/test_frontend.py`:
  - **Every API call in `app.js` has an API Gateway route.** It finds all 20 `api(...)` calls and checks each path and method against `ROUTES` in `infra/recipe_stack.py`, so a frontend call to a route the gateway doesn't forward fails the build instead of failing in production.
  - **`app.js` and `theme.js` parse** (`node --check`). This catches JavaScript syntax errors before deploy.
- `pipeline/buildspec-test.yml` now installs Node.js 24 in the Test stage so the syntax checks run there instead of being skipped.
- Full suite: 31 passed (backend, frontend and infrastructure).
- Manual checks on the local dev server (`backend/local_dev.py`), desktop and phone width, light and dark:
  - A non-owner joining a family no longer sees "null", and neither does a recipe without a description.
  - The account menu opens, closes on Escape and on outside clicks, and its links work. Sign out works.
  - The Family page shows the family recipes, and Share a family recipe preselects "Family only".
  - The signed-out nav and footer are correct, there's no sideways scrolling at 375px, and there are no console errors.

### Deploying

Frontend-only change plus a test-stage tweak. Pushing to `main` runs the pipeline (Test, Synth with diff, manual approval, Deploy). The Deploy uploads the new site files to S3 and clears the CloudFront cache. The expected diff touches only the site deployment (new asset hash) and the Test CodeBuild project is unchanged, because its buildspec is read from the repository.

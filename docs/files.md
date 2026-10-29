# File-by-file reference

Every file in the repository: **what it is**, **what it does**, and **why it's built that way**. The files are grouped by folder, in the order a request touches them: website, API, local development, infrastructure, pipeline, project files.

For the big picture, read [architecture.md](architecture.md) first.

---

## `frontend/`: the website

These files are uploaded to S3 exactly as they are and served by CloudFront. There's no build step, so what's in the folder is what's in production.

### `frontend/index.html`
**What:** The app's page. It has a header (the brand link and an empty `<nav id="nav">`), a notice bar, an empty `<main id="view">` that the app fills in, and a footer nav with the Privacy Policy and Terms of Service links. It links the favicon files, `styles.css` and the two scripts.

**Why:**
- **One page with hash routing** means S3 and CloudFront never need rewrite rules, and any `#/…` link can be bookmarked.
- **`theme.js` loads without `defer`** in `<head>` on purpose: it applies your saved season and dark mode *before the first paint*, so there's no flash of the wrong colors. `app.js` is deferred so it runs after the page is parsed.
- **The nav is an empty container** because what's in it depends on whether you're signed in, and `app.js` fills it. The footer is the same except for the two **policy links, which are plain HTML**, so they're there before the script runs. The `#footer-links` span (`display: contents` in the CSS) holds the one dynamic link, Appearance, for signed-out visitors.
- **The three icon links** (`favicon.ico`, `favicon.svg`, `apple-touch-icon.png`) cover old browsers, modern browsers and iPhone home screens. The `.ico` also answers the request every browser makes to `/favicon.ico` on its own. Without it, the private S3 bucket answered 403.
- **`<meta name="theme-color">`** colors the phone browser's toolbar. `theme.js` updates it to match the season.

### `frontend/app.js`
**What:** The whole single-page app, about 1,300 lines in one script. In order:
- the **DOM helper** `h()`;
- `render()`, notices, and **`confirmDialog()`** (the site-styled confirmation);
- **auth** against the Cognito API: sign up, confirm, sign in, refresh, revoke, forgot password; plus token storage;
- the **API client** `api()`, with rate-limit and stale-page handling;
- the **nav and account menu**;
- **pages**: Home feed, My recipes, recipe page, recipe form, families list, family page, invite page, profile, appearance, and the auth pages;
- the **router**.

**Why it's built this way:**
- **No framework, as the owner asked.** A tiny `h(tag, attrs, …children)` helper builds elements and *always* sets text with `textContent`. User-written text can never become HTML, which is the main XSS defense, and it's why the strict Content Security Policy works without `unsafe-inline`.
- **`render()` filters `null`/`false`.** `replaceChildren(null)` prints the word "null". That was the "null on the Family page" bug.
- **Talking to Cognito directly** (`cognito(action, body)` posts JSON with an `X-Amz-Target` header). This is Cognito's public user-pool API, which is how the site can have its own sign-in pages. `AUTH_MESSAGES` turns Cognito error codes into plain-English messages.
- **Token storage (`tokenStore`)**: `sessionStorage` by default, cleared when the tab closes; `localStorage` only with "Keep me signed in". Tokens refresh automatically shortly before they expire, and signing out revokes the refresh token in the background.
- **`api()`** adds the ID token, turns `429` into a "wait N seconds" message using `Retry-After`, signs you out on `401`, and drops data loads for pages you've left (`routeSeq` / `StaleRoute`). That last part fixed a race where a slow page could draw over a newer one.
- **`PASSWORD_RULES`** mirrors the Cognito password policy, so the sign-up form can show a live checklist before submitting. It's kept in sync by hand with `infra/recipe_stack.py` and `backend/local_cognito.py`, with comments pointing to each other.
- **`confirmDialog()`** uses a modal `<dialog>` (focus stays inside, the page behind is inert). Every way of closing it reports the answer immediately, because the browser's own `close` event can be delayed in background tabs. Destructive dialogs put focus on Cancel.
- **The account menu is a disclosure** (a button plus a list of links), not an ARIA "menu", so Tab and Enter work like the rest of the page.
- **Families**: `myFamilies` caches your families for the page being shown, `namesOf()` maps IDs to names for badges, `familyList()` turns a recipe's `familyIds` into words ("The Washingtons and 2 more"), and `MAX_FAMILIES` mirrors the backend's limit.
- **Sharing** (`recipeView`): your own recipe shows a "Who can see this" panel with **Share to Public** and **Share to Family**.
  - Share to Family opens `confirmDialog()` with a `familyPicker()`: styled checkboxes, one per family, with any families you've left listed so they can be unticked.
  - The dialog's `content` option takes a function that receives the confirm button, so the picker can keep its label ("Share with 2 families") and disabled state in step with the ticks.
  - The recipe form reuses the same picker under a Public / Family-only choice.
- **My recipes** (`#/mine`) lists your family-only recipes first, then all of them, from `GET /me/recipes`.
- **Family-only cards are colored** wherever they appear (`recipeCard()` adds `family-recipe`); `recipeSection(..., { family: true })` colors a section's heading to match.
- **Invite links** are `#/join/<code>`. Signed-out visitors go to sign-in first and come back afterwards (`returnTo` in `sessionStorage`). The join form also accepts a pasted link.
- **Copy to clipboard** tries the Clipboard API, then falls back to copying from a hidden text field. Some in-app browsers block the Clipboard API.
- **The location feature** only ever stores `north` or `south`. It re-checks quietly when the app opens, and the permission prompt appears only when you turn it on.
- **Sign-up agreement checkboxes**: two required boxes, "I'm 13 or older and agree to the Terms of Service" and "I've read the Privacy Policy". They're separate because the Terms are an agreement and the Privacy Policy is a notice. `required` makes the browser stop the form. The submit handler checks again with the site's own error message, before calling Cognito. `policyLink()` opens each document in a new tab, so the half-filled form isn't lost, and adds "(opens in a new tab)" for screen readers.

### `frontend/theme.js`
**What:** The seasonal theme engine, exposed as `window.RecipeTheme`. It reads and saves appearance preferences (in `localStorage`, and in the account when signed in), works out "auto" season from the date and hemisphere, applies `data-season`, `data-mode`, `data-font`, `data-text-size` and `data-decorations` to `<html>`, handles the location-to-hemisphere lookup, and fixes custom accent colors for contrast.

**Why:**
- **A separate file loaded in `<head>`**, so the theme applies before anything draws (see `index.html`).
- **Palettes are CSS variables in `styles.css`.** The script only sets attributes, so all colors live in one place.
- **Meteorological seasons** (whole months), flipped for the Southern Hemisphere: simple and predictable.
- **WCAG contrast math** (`readableAccent`) nudges a custom accent lighter or darker until it reaches 3:1 against the page background, the WCAG minimum for interface elements. People can pick any color without making the site unreadable.
- **Privacy**: `detectHemisphere()` asks for a *coarse*, cached position and immediately reduces it to `south` or `north`.
- **Every `localStorage` access is in `try/catch`**, so private browsing or blocked storage never breaks the page.

### `frontend/styles.css`
**What:** All styling, in sections:
- the **season palettes** (light and dark for each of fall, winter, spring and summer) as CSS variables, plus derived variables;
- base typography;
- the **header, account menu and footer**;
- shared components: cards, badges, buttons, forms, notices;
- the recipe, family, auth and appearance pages, including the sign-up agreement checkboxes;
- the **Privacy Policy and Terms of Service** pages (`.legal`);
- the **confirm dialog**;
- reduced-motion rules.

**Why:**
- **Design tokens as variables** (`--bg`, `--surface`, `--text`, `--accent`…), keyed on `data-season` and `data-mode`. Switching theme is just changing two attributes, and custom accents flow through `color-mix()` to the derived colors (`--accent-soft`, `--accent-border`).
- **No inline styles anywhere**, because the CSP blocks them. That's why, for example, the copy fallback's hidden field uses the `.copy-buffer` class.
- **The "stretched link" pattern** makes whole recipe cards clickable while keeping one real link per card for keyboards and screen readers.
- **A family color (`--family`) per season and mode**, a hue apart from that season's accent (green in fall, plum in winter, teal-blue in spring, berry in summer), checked to at least 4.5:1 contrast for small text on its tinted background. `--family-soft` and `--family-border` are derived from it like the accent ones.
- **Choice cards (`.choice`)** are real checkboxes and radios with `appearance: none`, drawn larger. They keep keyboard use, focus rings and screen-reader labels for free. The CSS variable `--choice` switches them between the family color and the accent (for Public).
- **Mobile rules**: the nav wraps under the brand, the account menu goes full width, and dialog buttons stretch. Everything was checked at 375 px with no sideways scrolling.
- **`prefers-reduced-motion`** turns off transitions and the dialog's fade-in.
- **`[hidden] { display: none !important }`** stops button `display` rules from un-hiding hidden elements, which was an early bug.

### `frontend/favicon.svg`, `frontend/favicon.ico`, `frontend/apple-touch-icon.png`
**What:** The site icon, an open cream recipe book with an autumn leaf on the header's rust gradient, in three formats.

**Why:** Modern browsers use the SVG, which stays sharp at any size. Browsers request `/favicon.ico` on their own, and older ones only understand `.ico`, which holds 16, 32 and 48 px images. iOS uses the 180 px PNG for home-screen icons. The `.ico` and `.png` were rasterized from the same shapes as the SVG so all three match.

### `frontend/privacy.html`, `frontend/terms.html`
**What:** The Privacy Policy and Terms of Service, served at `/privacy.html` and `/terms.html`. Each starts with a plain-language "short version", then a contents list and numbered sections, and the footer links both. Contact: `support@familyfoodiebook.com`. Minimum age: 13, with a parent's permission under 18. There's no governing-law clause yet, by the owner's choice.

**Why:**
- **Standalone pages, not `#/` routes.** They have stable URLs that search engines, app stores and payment providers (Stripe, for example) can read. They work without JavaScript. Opening them in a new tab from sign-up leaves the form alone. They load `styles.css` and `theme.js` (but not `app.js`), so they still follow the visitor's season and dark mode.
- **Written from what the code actually does**, not a generic template:
  - passwords go only to Cognito (`USER_PASSWORD_AUTH`), never to the API;
  - request logs (IP address, route, status, latency) are kept 30 days;
  - for signed-out visitors, rate-limit counters are kept per IP address, with a 2-minute TTL;
  - there are no cookies, analytics or ads;
  - only `north`/`south` is kept from location;
  - email addresses are never shown to other members;
  - family recipes stay with the family when their author leaves;
  - deleting a recipe deletes its comments.

  **When one of those changes, update the pages and their "Effective" date**; for example, adding photos, SES email, account deletion or payments.
- **Account deletion is by email** for now, because there's no self-service option yet. The Privacy Policy promises an answer within 30 days, and that's a manual job until the "delete my account" feature in [plans/monetization.md](plans/monetization.md) is built.
- **No inline styles or scripts** (the CSP blocks them), which `test_frontend.py` checks.

---

## `backend/recipe_api/`: the API that runs in Lambda

Only this folder is packaged into the Lambda function.

### `backend/recipe_api/main.py`
**What:** The FastAPI application: models, identity, rate limiting, and every route.
- **Errors**: one JSON shape, `{"message": …}`, for application errors, HTTP errors and validation errors.
- **Models** (pydantic): `Profile`, `Preferences`, `RecipeIn`/`Recipe`, `Sharing`, `Feed`, `Comment`, `Family`, `MyFamilies`, `InvitePreview`, `FamilyRename`… with length limits on every text field. `RecipeIn` still accepts the older one-family `familyId` field, in case a browser has a cached copy of the old app.
- **Identity**: `claims_from()` takes the verified JWT claims API Gateway put in the event. Locally it falls back to the local token verifier, or to an `X-Dev-User` header when local dev headers are enabled.
- **Dependencies**: `Reader`, `Writer` and `Commenter` (signed-in, each with its own rate-limit bucket) and `anonymous` (per-IP limit for public routes).
- **Profiles**: `get_or_create_profile()` creates a profile on first use, with the display name taken from the sign-up `name` claim. It also upgrades old single-family profiles.
- **Recipes**: create, list feeds, **my recipes** (`GET /me/recipes`), get, update, **share** (`PUT /recipes/{id}/sharing`: public, or a set of your families), delete; with visibility and family checks.
- **Comments**: list, add, delete.
- **Families**: create, list mine, invite preview, join, rename, family recipes, leave, rotate the invite code, remove a member.
- **Public routes** for signed-out visitors.
- `handler = Mangum(app)`: the Lambda entry point.

**Why:**
- **FastAPI and pydantic** (the owner's choice) give free, consistent validation. A bad body gets a clear 400 before any logic runs. Response models filter out internal keys like `pk` and `gsi1pk`.
- **API Gateway verifies tokens, not the Lambda.** That's cheaper and faster: invalid requests never invoke the function. The Lambda still refuses a request with no claims, as a safety net. `IN_LAMBDA` makes sure the local-only fallbacks can never apply in AWS.
- **404 instead of 403** for family recipes you can't see, so their IDs can't be probed.
- **`with_current_names()`** does one BatchGetItem per 100 authors, so names stay current without a write fan-out on every rename.
- **Transactions for families.** Membership changes touch several items: the family, the member, the invite code and your profile. They all succeed or all fail. Conditions inside the transaction enforce "not already a member", "fewer than 10 families" and "the code wasn't just changed".
- **`MAX_FAMILIES = 10`** keeps Home to a bounded number of queries.
- **`upgrade_single_family()`** moves old data forward on read, in one conditional update, so no migration job was needed.
- **Sharing has its own route, `PUT /recipes/{id}/sharing`**, so the recipe page's Share buttons don't have to send the whole recipe back. `PUT /recipes/{id}` can still change visibility too, for the edit form. Both go through the same helpers, so the rules live in one place:
  - `chosen_families()` holds the rules: only the author, only your own families (families you've since left may be kept or dropped), at least one, at most 10.
  - `with_sharing()` shapes the recipe item.
  - `save_recipe()` writes the recipe and its `SHARE` items in one transaction.
- **`family_feed()` re-reads and re-checks each recipe** behind a family's `SHARE` items. Share items hold no recipe data, so there's nothing to keep in sync on edits, and a leftover share can never leak a recipe.
- **`present()`** is the one place recipes are shaped for a viewer: current author names, and `familyIds` trimmed to the viewer's own families unless they wrote the recipe.
- **`recipe_family_ids()`** reads both the new `familyIds` set and the old single `familyId`, so recipes saved before multi-family sharing keep working; they're upgraded the next time they're saved.
- **`batch_get()`** wraps BatchGetItem's 100-key limit and its "unprocessed keys" retry, used by both names and feeds.
- **Docs are off in Lambda** (`docs_url=None`). API Gateway only forwards the listed routes anyway, but `/docs` stays useful locally.

### `backend/recipe_api/ratelimit.py`
**What:** The fixed-window rate limiter. `LIMITS` holds each bucket's maximum and window, and `check(table, bucket, identity)` does one atomic `ADD` on a counter item and returns a `Decision` (allowed, limit, remaining, retry-after).

**Why:**
- **DynamoDB instead of an in-memory counter**, because each Lambda invocation can be a different instance. Memory isn't shared, but the table is.
- **An atomic `ADD`** means simultaneous requests can't miscount.
- **The TTL attribute** (`expiresAt`) lets DynamoDB delete old counters for free.
- **Kept small and separate** so it's easy to tune and test. See [architecture.md §5](architecture.md#5-rate-limiting-two-layers) for the trade-offs.

### `backend/recipe_api/requirements.txt`
**What:** The packages bundled into Lambda: `fastapi`, `mangum`, `pydantic`, at exact versions.

**Why:** **Exact versions**, so every deploy is reproducible. **boto3 is left out** because the Lambda runtime already includes it, which keeps the package smaller and the cold starts faster.

---

## `backend/mail_forwarder/`: the support@ mail forwarder

### `backend/mail_forwarder/forwarder.py`
**What:** A small Lambda that SES runs for each message to `support@familyfoodiebook.com`, after storing it in the mail bucket. It:
- skips mail SES marks as a virus or spam, and mail failing its sender's DMARC `reject` policy (those stay in the bucket);
- reads the stored message, sets From to `"<sender> via Family Foodie Book" <support@…>` and Reply-To to the original sender, and drops the old DKIM signature, Message-ID, Sender and Return-Path;
- sends it with SES to the inbox named in Parameter Store (`/recipe-book/support-forward-to`).

**Why:**
- **SES only sends from verified identities**, so the copy can't keep the sender's own From. Reply-To makes the inbox's Reply button answer the sender.
- **The address is read from Parameter Store for each message**, so it's not in the repository or the template, and changing it needs no deploy.
- **Only the standard library and boto3**, so there's nothing to bundle. `compat32` parsing writes the body back exactly as it arrived.
- **Logs hold the message ID and outcome only**, never the sender or the content.

---

## `backend/`: local development and shared helpers

### `backend/local_dev.py`
**What:** Runs the **whole app on your computer** in one process:
- the real FastAPI app;
- moto's in-memory fake DynamoDB, saved to `backend/.localdata/recipes.json` after every successful change and reloaded on start;
- a fake Cognito (`local_cognito.py`, SQLite) at `/local-cognito`;
- a local `/config.json` that points the site at those;
- the `frontend/` folder served as static files.

On the first run it creates three test accounts (alice, bob and carol), sample recipes, and two families, so that alice is in both. Alice's family pie is shared with both of her families. Flags: `--reset` (wipe local data), `--empty` (no sample data), `--data-dir`, and the `PORT` environment variable.

**Why:**
- **Nothing touches AWS.** It's free, works offline, and it's safe to break things.
- **The site code is unchanged.** Locally, `config.json` points `cognitoEndpoint` at `/local-cognito`, which speaks the same request and response shapes as Cognito.
- **A JSON snapshot after each write** keeps accounts and recipes between restarts without needing a real database. Rate-limit counters aren't saved.
- **Confirmation and reset codes are printed in the terminal** instead of being emailed.
- **The `X-Dev-User` header** lets `/docs` and `curl` act as any user without signing in. It's local only.

### `backend/local_cognito.py`
**What:** `LocalUserPool`, a stand-in for the Cognito user-pool API saved in SQLite. It covers sign up and confirm (with codes), resending the code, sign in (password and refresh token), forgot and reset password, and revoking tokens. Passwords are stored as **PBKDF2-SHA256 hashes, 200,000 rounds, with a random salt per account**. After 5 failed sign-ins, the account is locked for 5 minutes. Tokens are HS256-signed with a local secret.

**Why:**
- **It's faithful enough to test the real sign-in pages**: the same action names, error codes (`UsernameExistsException`, `CodeMismatchException`…) and response shapes.
- **Hashed passwords even locally**: there's no reason to keep plain text anywhere, and the tests check it.
- **Clearly not a security boundary.** The docstring says so, because the signing secret sits next to the database. That's fine on your own PC and nowhere else.
- **Why not moto's Cognito?** It needed extra dependencies, and its default password policy rejected the test passwords. A small purpose-built stand-in was simpler and fully under our control.

### `backend/devtools.py`
**What:** Two helpers shared by the tests and local dev: `use_fake_aws()` (fake credentials, the Region, and the API folder on the import path) and `create_table()` (a table shaped exactly like the real one, including the `gsi1` and `byAuthor` indexes).

**Why:** One definition of the table shape outside the CDK. If tests and local dev each built their own table, they could drift apart.

### `backend/requirements-dev.txt`
**What:** Everything needed to test and run locally: the Lambda requirements plus `boto3`, `uvicorn`, `httpx`, `pytest`, `pyyaml` and `moto[dynamodb]`, and the two security scanners, `pip-audit` and `bandit`, pinned to exact versions.

**Why:** It's separate from the Lambda requirements, so test tools are never shipped to production. `-r recipe_api/requirements.txt` keeps the shared versions identical.

---

## `backend/tests/`: backend, frontend and local-dev tests

### `backend/tests/test_handler.py`
**What:** API behavior tests. Each one sends a real API Gateway (payload 2.0) event through the Lambda handler against a moto table. They cover:
- profiles and preferences, including hemisphere-only location;
- current names everywhere; sign-up names;
- missing claims → 401;
- public recipes and comments;
- family privacy, with 404s;
- membership rules, owner removal and multiple families;
- the 10-family limit, rename and invite preview;
- publishing;
- sharing one recipe with several families:
  - only the author can share, and only with their own families;
  - each family's feed shows it, and Home lists it once;
  - other members see only their own families' names;
  - unsharing, keeping or dropping a family the author has left, sharing to Public, and deleting it with its shares;
- My recipes: everything you wrote, comments excluded, still listed after leaving a family;
- recipes saved with one `familyId` still showing up, and upgrading to `SHARE` items when edited;
- upgrading old single-family profiles;
- validation errors, dropped blank lines;
- 429s and the per-IP limit.

**Why:**
- **Testing through `handler()`** exercises the same path Lambda uses: Mangum, routing, claims from the event and error handlers. That's more realistic than calling functions directly.
- **A fake clock** is used where order matters. Timestamps have one-second resolution, which once made a test pass by luck.

### `backend/tests/test_routes.py`
**What:** Checks that the API Gateway route list in `infra/recipe_stack.py` matches the FastAPI routes exactly, and that only `/public/*` routes skip sign-in.

**Why:** API Gateway only forwards routes it knows about. A route added in FastAPI but not in the CDK works locally and gives a 404 in production. This test makes that impossible to miss.

### `backend/tests/test_frontend.py`
**What:**
- Every `api("…")` call in `app.js` must match a gateway route and method.
- `index.html`, `privacy.html` and `terms.html` may only link files that exist, and `favicon.ico` must exist.
- No page has inline scripts, styles or event handlers, which the CSP would block.
- Every page's footer links both policies.
- Every `#anchor` link in the policies, including cross-links such as `terms.html#who-can-use`, points at a heading that exists.
- Every `#/page` link in `app.js` (such as `#/mine`) goes to a page `route()` handles; an unknown one would quietly show Home.
- Each policy page has an "Effective" date and the support email address.
- The sign-up form has both required checkboxes, and checks them before calling Cognito.
- `app.js` and `theme.js` must parse (`node --check`).

**Why:** The frontend has no build step, so nothing else would catch a typo in an API path or a JavaScript syntax error before deploy. A missing file shows up on the live site as a 403, not a 404, which is confusing to diagnose. Inline code works on the local server, which sends no CSP, but is blocked live, so it's checked here.

### `backend/tests/test_mail_forwarder.py`
**What:** The support@ forwarder, with fake S3, SES and Parameter Store clients:
- From becomes support@ with the sender's decoded name, Reply-To is the sender (or their own Reply-To), and the old signature headers are gone while the body is unchanged;
- virus, spam and DMARC-`reject` failures aren't forwarded;
- the handler reads `support/<message id>` and sends to the address from Parameter Store.

**Why:** A mistake here loses privacy and deletion requests without anyone noticing, so the header handling is checked without needing AWS.

### `backend/tests/test_local_dev.py`
**What:** Tests the local dev server:
- sign-up with emailed codes, then using the API;
- accounts and recipes surviving a restart;
- password reset and lockout;
- passwords stored hashed, and refresh tokens being revocable.

**Why:** Local dev is how every change is tried before deploy, so it needs to behave like the real thing and keep its data.

---

## `infra/`: AWS infrastructure (CDK, Python)

### `infra/app.py`
**What:** The CDK entry point. It creates two stacks in **us-east-2**:
- `RecipeBook` (the app), given the domain settings from `cdk.json` when they're present;
- `RecipeBookPipeline` (CI/CD), given the GitHub owner, repository and branch from `cdk.json`.

It tags everything `project=recipe-book`.

**Why:**
- **The Region is fixed in code** because the project may only create Regional resources in its selected Region.
- **Two stacks**, because the pipeline deploys the app, but the pipeline itself is deployed by hand and rarely changes.
- **Tags** make cost reports and cleanup easy.

### `infra/recipe_stack.py`
**What:** The whole application stack:
- **DynamoDB table** (on-demand, TTL, `gsi1`, **RETAIN**);
- **private S3 bucket** for the site;
- **CloudFront security-headers policy** (CSP and friends);
- optional **custom domain**: an imported certificate, the canonical-host CloudFront Function, and Route 53 A/AAAA aliases;
- **CloudFront distribution** with OAC, HTTPS redirect and `PRICE_CLASS_100`;
- **Cognito user pool** (Lite, email sign-in, password policy, **RETAIN**) and its **web client** (no secret, `USER_PASSWORD_AUTH`, OAuth off, token revocation, 1-hour tokens, 30-day refresh);
- **Lambda** (Python 3.13 ARM64, 512 MB, 10 s, one-month logs) with **local pip bundling**;
- **HTTP API** with CORS for the site's one host, the Cognito authorizer, every route from `ROUTES`, stage throttling and JSON access logs;
- **BucketDeployment** that uploads `frontend/` plus a generated `config.json` and clears the CloudFront cache;
- optional **support mail** (`_support_mail`, with `supportForwardToParameter`): the SES domain identity with DKIM and MAIL FROM, the MX, SPF and DMARC records, a **RETAINed** mail bucket (365-day expiry), the forwarder Lambda, the receipt rules for `support@` and `dmarc-reports@`, and a custom resource that activates them;
- outputs.

**Why:**
- **`ROUTES` is a plain list** (path, method, needs sign-in) so a test can compare it with FastAPI's routes.
- **`PipBundling`** builds the Lambda package with the local pip, requesting Linux ARM64 / Python 3.13 wheels, so neither the PC nor CodeBuild needs Docker. pydantic has compiled parts, so getting the right platform's wheels matters. Docker bundling stays as the fallback.
- **512 MB of memory.** FastAPI and pydantic take longer to import than plain Python, and on Lambda more memory also means more CPU, which roughly halves cold starts at little extra cost.
- **`disable_o_auth=True`.** Otherwise CDK quietly turns on OAuth flows with an `example.com` callback, which this site doesn't use.
- **`prevent_user_existence_errors`**, so sign-in and reset errors don't reveal which emails have accounts.
- **CSP `connect-src`** allows only this Region's API Gateway and Cognito endpoints.
- **`PRICE_CLASS_100`** (North America and Europe edge locations) is the cheapest class and fits the audience.
- **RETAIN only on data**: the table, the user pool and the mail bucket. Everything else can be rebuilt, so it's deleted with the stack.
- **The domain is optional.** Without `context.domain`, the stack still deploys using the CloudFront address. That's useful for testing and was how the site first launched.
- **Support mail is optional too**, and needs the domain. CloudFormation can create a receipt rule set but not make it the active one, so an `AwsCustomResource` calls `SetActiveReceiptRuleSet` (and deactivates it on delete). The forwarding address isn't in the stack at all; see `forwarder.py`.

### `infra/pipeline_stack.py`
**What:** The CI/CD stack:
- a **CodeConnections** GitHub connection, created as pending until someone approves it once in the console;
- an **artifact bucket** (private, deletes after 30 days);
- three **CodeBuild projects** (Test, Synth, Deploy) on the ARM image, each with one-month logs;
- a **source role** limited to this connection;
- the **pipeline** (V2, QUEUED): Source → Test → Synth → **manual approval** → Deploy, with a trigger on pushes to the branch;
- outputs: the connection ARN and the pipeline URL.

**Why:**
- **Each stage gets only the roles it needs.** Synth can assume only CDK's lookup role, enough for a read-only `cdk diff`. Deploy can assume the deploy, file-publishing and lookup roles. Nothing else in the account is reachable.
- **Buildspecs are read from the repository** (`from_source_filename`), so build steps change with a normal commit instead of a pipeline redeploy.
- **Deploy gets Synth's output** (`extra_inputs`), so the exact templates you approved are the ones deployed.
- **QUEUED mode**: runs happen one at a time and in order, so an older run can never deploy over a newer one.
- **The connection can't be fully automated.** GitHub authorization needs a person in the console; the code comment says so.

### `infra/cdk.json`
**What:** CDK settings:
- the `app` command (the local virtual environment's Python, on Windows);
- `watch` excludes;
- `context.github` (owner `Jakedup7878`, repo `recipe-book`, branch `main`);
- `context.domain` (`familyfoodiebook.com`, the us-east-1 certificate ARN, the hosted zone ID, and `supportForwardToParameter`, the Parameter Store name that holds the support@ forwarding address).

**Why:** Deployment settings are configuration, not code. The buildspecs pass `--app "python app.py"` explicitly, because the `app` line points at the Windows virtual-environment path, which doesn't exist on the Linux build machines.

### `infra/requirements.txt`, `infra/package.json`, `infra/package-lock.json`
**What:**
- `requirements.txt`: the CDK library (`aws-cdk-lib==2.274.0`, `constructs`);
- `package.json`: the CDK command-line tool (`aws-cdk 2.1146.1`);
- `package-lock.json`: the lock file that pins the CLI's exact dependency tree.

**Why:** Pinned versions mean the pipeline builds with exactly what was tested locally. The CLI is a Node package, separate from the Python library. `npm ci` installs exactly what the lock file says.

### `infra/tests/test_recipe_stack.py`
**What:** Synthesizes the app stack and checks five things:
- the table and user pool are retained;
- the table has exactly the two indexes the API queries (`gsi1`, `byAuthor`); CloudFormation can only add one index per deploy, so this flags changes that would add two at once;
- without a domain, the site uses the CloudFront address;
- with a domain, both names, the certificate, the redirect function, the DNS records and CORS are set up correctly;
- with support mail, the mail DNS records sit alongside the unchanged site records; support@ is stored then forwarded and dmarc-reports@ only stored; the rule set is activated; the mail bucket is retained with a 365-day expiry; the forwarder can read only its one parameter; and no address other than the domain's own appears in the template. Without the parameter setting, none of it is created.

Bundling is skipped via CDK context so the tests run fast.

**Why:** Mistakes in infrastructure can be expensive, for example losing data with the wrong removal policy, or a broken domain. These catch them before a deploy.

### `infra/tests/test_pipeline_stack.py`
**What:** Checks five things:
- the stage order, with the manual approval before Deploy;
- only Deploy can use CDK's deploy roles, and Synth only the lookup role;
- the builds use the ARM image and the buildspec files from the repository;
- the buildspec files exist and are valid YAML;
- the Test stage runs both security scans on the production code, with no `||` or `--exit-zero` that would make them always pass, and both scanners are pinned in `backend/requirements-dev.txt`.

**Why:** The permission split is the pipeline's main safety property, so it's tested. The scans are a gate before every deploy, and a gate that's quietly removed looks exactly like one that passes, so that's tested too.

---

## `pipeline/`: CodeBuild steps

### `pipeline/buildspec-test.yml`
**What:** Installs Python 3.13 and Node 24 plus the development requirements, then:
- runs `pytest` on `backend/tests` and `infra/tests`, and publishes a JUnit test report you can view in CodeBuild;
- runs the two security scans:
  - **pip-audit** on `backend/recipe_api/requirements.txt`, the packages that ship in the Lambda;
  - **bandit**, at medium severity and above, on `backend/recipe_api` and `backend/mail_forwarder`.

**Why:**
- Node is installed so the frontend syntax checks run instead of being skipped. The JUnit report gives a per-test history in the console.
- **Why scan here:**
  - A failure stops the run before the approval step, so nothing with a known-vulnerable dependency or a new risky pattern can be approved by accident.
  - **pip-audit covers only what ships to production.** The build tools (CDK, pytest) never handle user data, and auditing them too would block deploys over issues that can't reach the site.
  - **bandit skips low-severity findings**, which are mostly noise.
- What to do when a scan fails is in [operations.md §3](operations.md#3-run-the-tests).

### `pipeline/buildspec-synth.yml`
**What:** Installs the CDK, runs `cdk synth RecipeBook --strict --quiet` into `cdk.out`, then `cdk diff` against what's deployed, and saves `cdk.out` as the stage's output.

**Why:**
- **`--strict`** fails on warnings.
- **`--quiet`** keeps the template out of the log, so the diff is easy to find.
- **If the diff can't run**, for example because the stack doesn't exist yet, the log says so instead of failing.
- **The output becomes the Deploy stage's input.**

### `pipeline/buildspec-deploy.yml`
**What:** Installs the pinned CDK command-line tool and runs `cdk deploy RecipeBook --app <the Synth output> --require-approval never`.

**Why:**
- **It deploys the approved assembly**, not a fresh build.
- **`--require-approval never`** is safe here because the pipeline's manual approval stage is the review gate. The Deploy stage only runs after it.

---

## Project files

### `CHANGELOG.md`
**What:** A dated record of each change: what changed for users, API and data changes, tests and deploy notes.

**Why:** Decisions and their reasons get lost in commit messages. This is the human-readable history.

### `CLAUDE.md`
**What:** Instructions for the AI assistant working on this project:
- project settings: help level HIGH, AWS profile and Region, domain, certificate ARN, the note that the deployed stacks are kept, and the leftover resources;
- AWS's guidance for the new AWS experience: Region rules, Free plan constraints, terminology.

**Why:** It keeps future sessions consistent: the same Region, the same safety rules (for example, no deleting retained data without asking), and the same level of explanation.

### `.gitignore`
**What:** Excludes virtual environments, `node_modules`, `cdk.out`, Python caches, personal Claude settings and **`backend/.localdata/`** (local accounts and recipes).

**Why:** Build output and personal or local data should never be committed. Local test accounts in particular stay out of git.

### `.claude/launch.json`
**What:** The `recipe-book-local` launch entry: it runs `backend/local_dev.py` with the project's virtual environment on port 8001.

**Why:** It lets the Claude desktop app's preview pane start the local server with one click, so every UI change can be checked in a real browser.

### `README.md` and `docs/`
**What:** This documentation.

**Why:** So someone new, or you in six months, can understand, run and change the project without reconstructing the reasoning.

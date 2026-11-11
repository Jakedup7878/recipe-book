# Changelog

Notable changes to Recipe Book. Newest first.

## 2026-11-11: Save a recipe as a draft

### What changed for people using the site

- **Save as draft** on the recipe form, beside **Publish recipe**. A draft is saved to your account, so it's on every device you sign in on, and **only you can see it**.
- **A draft needs only a title.** Ingredients and steps can wait until you're ready to share it.
- **My recipes has a Drafts section** at the top when you have any. Draft cards have a dashed outline and say when they were last saved.
- **Finishing a draft:**
  - open it and choose **Keep writing**, then **Save draft** again or **Publish recipe**;
  - or, once it has ingredients and steps, use **Share to Public** or **Share to Family** on its page.
- **A published draft is dated the day you share it**, so it shows up as a new recipe in the feeds.
- **It's one-way:** a recipe that's been shared can't go back to being a draft. Other people's comments would be hidden from them while still stored.
- Drafts have no comments, and deleting one asks first, like any recipe.
- **Privacy Policy** (effective November 11, 2026): the short version now says drafts can be seen only by you.

### How it works

- No new routes, tables or indexes: `visibility` gains a third value, `"draft"`, on `POST /recipes` and `PUT /recipes/{id}`. This deploy changes only the Lambda code and the site files.
- A draft is a recipe item with no feed key and no `SHARE` items, so it's in no feed. My recipes finds it through the `byAuthor` index, like everything else you wrote.
- `backend/recipe_api/main.py`:
  - `with_sharing()` holds the rules for all three routes that save a recipe;
  - `require_complete()` asks for ingredients and steps only when a recipe is shared;
  - `can_view()` lets a draft through only for its author, and everyone else gets 404;
  - `add_comment()` refuses drafts.
- `frontend/app.js`: the form's `save()`, with a guard against double clicks; draft cards, the Drafts section, and the draft version of the recipe page.
- The local dev server's sample data gains one draft, Alice's "Cornbread Dressing". It appears after `--reset`, since sample data is only created on the first run.
- See decision 20 in [docs/architecture.md](docs/architecture.md) for why drafts are saved to the account instead of auto-saved in the browser.

### Tests

- New `test_drafts_are_private_to_their_author` and `test_sharing_a_draft_publishes_it_as_a_new_recipe` (`backend/tests/test_handler.py`). To check they work, drafts were put in the public feed on purpose: both failed, and the code was restored.
- `test_local_dev.py` checks the sample draft is in Alice's list and in neither feed.
- **Checked in a browser on the local dev server**, signed in as Alice:
  1. Save as draft with no title is stopped at the title field;
  2. with only a title it saves, and the draft's page has a Draft badge, no share buttons and no comments;
  3. a signed-out request for the draft gets 404, and it isn't in the public list;
  4. My recipes shows it under Drafts, and again under All my recipes with a Draft badge;
  5. after adding an ingredient and a step and saving the draft again, Share to Public published it: top of the public feed, loadable signed out, comments on;
  6. editing the shared recipe offers only Save changes.
- Full suite: 79 passed.

## 2026-10-29 (afternoon): Installable app (PWA)

### What changed for people using the site

- **The site installs as an app** on Android, iPhone and desktop Chrome/Edge. It gets its own icon and opens full screen, without the browser's address bar.
  - **Android, Chrome, Edge:** the footer's new **Install the app** opens the browser's install prompt. The browser menu's *Install app* works too.
  - **iPhone and iPad:** **Install the app** explains Share → *Add to Home Screen*. Apple offers no prompt that a site can open.
  - The link hides itself where neither applies, and once the app is installed.
- **It opens without a connection.** The pages, styles, scripts and icons are kept on the device. Recipes still need a connection, and a page that couldn't load says so and **loads by itself when the connection comes back**. Installed apps have no reload button.
- **Being offline no longer signs you out.** Before, opening the site offline more than an hour after signing in failed the token refresh, and the app then deleted the saved sign-in. Now it keeps it and refreshes once back online.
- **Clearer offline messages:** "You're offline. Connect to the internet and try again." instead of the browser's "Failed to fetch".
- **Privacy Policy** (effective October 29, 2026): §6 lists the saved site files, and says they hold no recipes, comments or account details.

### How it works

- `frontend/manifest.json`: the app's name, icons and colors. Its paths are relative, so beta and local dev work too.
- `frontend/icon-192.png`, `icon-512.png`, `icon-maskable-512.png`: drawn from the favicon's shapes. The maskable one fills the square, with the artwork inside the circle Android always keeps.
- `frontend/sw.js`, the service worker:
  - it saves the site's own files (`SHELL`), going **network first**, so deploys still show up on the next load;
  - API and sign-in requests are never handled or stored;
  - see [docs/files.md](docs/files.md), and decision 19 in [docs/architecture.md](docs/architecture.md).
- `app.js` registers the worker after the first page draws, and adds the install link, `NetworkError`, and reloading pages when the connection returns. `confirmDialog()` can now leave out Cancel (`cancelLabel: null`).
- No infrastructure changes: the existing Content Security Policy (`default-src 'self'`) already allows the manifest and the worker. The Synth diff should show only the `DeploySite` asset changing.

### Tests

- New `backend/tests/service_worker.test.js` runs `sw.js` against a fake browser in Node, and `test_frontend.py` runs it. Removing the worker's address check made two of its tests fail, and the check was restored.
- New in `test_frontend.py`:
  - the manifest is installable, and its icon sizes and colors match;
  - `SHELL` lists every frontend file;
  - an offline token refresh keeps the sign-in;
  - `sw.js` parses.
- **Checked in a browser on the local dev server:**
  1. the worker activated and saved exactly the 15 `SHELL` files, with no API responses, even after signing in and loading pages;
  2. with the server stopped and an expired sign-in, the app opened from its copies, kept the sign-in, and showed the offline message;
  3. after the server restarted, the `online` event reloaded the page;
  4. the iPhone dialog has only "Got it", and the footer link matches the policy links.
- Full suite: 77 passed.

## 2026-10-29: Security scanning in the pipeline

### What changed

- **The pipeline's Test stage runs two security scanners** after the tests. Either one failing stops the run before the approval step, so nothing can be approved while a scan is failing.
  - **pip-audit 2.10.1** checks the packages that ship in the Lambda (FastAPI, Mangum, pydantic and everything they pull in) against public vulnerability databases. It resolves them the same way the Lambda build does.
  - **bandit 1.9.4** checks the Python that runs in AWS (`backend/recipe_api`, `backend/mail_forwarder`) for risky patterns, failing on medium and high severity.
  - Both versions had been released for months before they were pinned. For security tools, a release that has been public for a while is less likely to be broken or compromised.
- **Both scans were clean when added:** no known vulnerabilities, and no bandit findings at any severity. So a future failure always means something new.
- **Before adding the scans, the code was checked for injection.**
  - There's no SQL or PartiQL anywhere.
  - Every DynamoDB expression is fixed text, with input only as `:placeholder` values.
  - No user text is inserted as HTML in the browser, and there's no shell or `eval` in the code that runs in AWS.
- **Docs:**
  - [docs/operations.md](docs/operations.md) §3 has the local commands, and what to do when a scan fails: upgrade the package, or, only if there's no fix yet, accept it temporarily with `--ignore-vuln` and a dated reason. It also gets a troubleshooting row.
  - [docs/architecture.md](docs/architecture.md) §4 gets an injection section, and its access table now matches multi-family sharing (it still described one family per recipe).
  - [docs/files.md](docs/files.md) describes the new scan step and test.

### Tests

- New `test_test_stage_runs_the_security_scans_and_fails_on_findings` (in `infra/tests/test_pipeline_stack.py`). It fails if:
  - either scan is removed from the Test stage;
  - a scan is made to always pass (`||` or `--exit-zero`);
  - either scanner is no longer pinned.

  To check that the test works, I added `|| true` to the bandit command, saw the test fail, and restored the file.
- Full suite: 72 passed.

## 2026-10-27: Share a recipe with several families; My recipes

### What changed for people using the site

1. **Share to Public / Share to Family on your own recipes.**
   - Your recipe's page has a **"Who can see this"** panel, which says whether the recipe is public or which families it's shared with.
   - **Share to Public** moves it out of its families into Public recipes, after a site-styled confirmation. This replaces the old "Publish to public" button.
   - **Share to Family** opens a dialog of styled checkboxes, one per family with its member count. Your current families are already ticked.
     - The button keeps count ("Share with 2 families") and is disabled until at least one family is ticked.
     - Families you've left that the recipe is still shared with are listed as "A family you've left", so you can untick them.
     - A public recipe shared this way becomes family-only.
2. **One recipe, several families.**
   - Members of each family you pick see the recipe and all of its comments, including comments from the other families.
   - They only ever see the names of *their own* families on it. Only you see the full list.
   - If you're in more than one of those families, Home still lists the recipe once.
3. **My recipes**, a new top-bar link between Home and Family (`#/mine`). It has two sections:
   - **Shared with family**: your family-only recipes.
   - **All my recipes**: everything you've written, each card labeled Public or with its families.
4. **Family-only recipes have their own color, wherever they're listed**, including Home, a family's page and My recipes:
   - a tinted card with a colored left edge, and a matching badge and section marker;
   - the color is a hue apart from each season's accent: green in fall, plum in winter, teal-blue in spring, berry in summer, with lighter versions in dark mode;
   - every one is checked to at least 4.5:1 contrast (WCAG AA) for small text.
5. **The recipe form's "Who can see it"** is now two choice cards, **Public** and **Family only**, with the same family checklist under Family only. It replaces the one-family dropdown.
6. **The Privacy Policy and Terms of Service** (effective October 27, 2026) now say:
   - recipes can be shared with several families;
   - comments are visible across those families;
   - other families' names aren't shown;
   - you can stop sharing with a family you've left.

### How it works

- **New routes.** Both are signed in and added to API Gateway:
  - `GET /me/recipes`;
  - `PUT /recipes/{id}/sharing`, with `{visibility, familyIds}`, author only.
- **Recipe changes.** `PUT /recipes/{id}` and `POST /recipes` take `familyIds` (a list) instead of `familyId`. The old field is still accepted, for browsers with a cached copy of the old app. Responses return `familyIds`, trimmed to the viewer's own families unless they're the author.
- **Share items.** Each family a recipe is shared with gets a `RECIPE#<id> / SHARE#<familyId>` item that puts it in that family's feed. It holds no recipe data.
  - Writing a recipe and its share items is one transaction.
  - Feeds re-read each recipe and check its sharing again, so a leftover share can't leak it.
- **A new index, `byAuthor`** (`authorId`, `createdAt`), for My recipes. DynamoDB fills it in for existing recipes automatically.
- **Recipes saved before this change keep working** (one `familyId`, in the feed themselves). They switch to the new layout the next time their author edits or shares them.
- Details and trade-offs are in [docs/architecture.md](docs/architecture.md) (data model, and decisions 17 and 18).

### Tests

- New handler tests:
  - sharing with several families: author-only, your own families, listed once, names hidden from other families, unsharing, families you've left, Share to Public, delete;
  - My recipes;
  - old one-family recipes, read and upgraded.
- Updated tests for the `familyIds` field.
- The local sample data shares Alice's pie with both her families, and the restart test checks that it's listed once.
- New: `test_table_indexes_match_what_the_api_queries` (infra) and `test_every_page_link_goes_to_a_page_the_router_handles` (frontend).
- Full suite: 71 passed.
- Manual checks on the local dev server, using data saved before this change:
  - My recipes showed the old-format pie as a family card.
  - Share to Family: ticks, the count on the button, disabled with nothing ticked, sharing with two families.
  - As Bob, in only one of the families: he saw only "The Washingtons", with no sharing panel.
  - The recipe form at 375 px in light mode, with no sideways scrolling.

### Deploy note

- Run 9982fcf1 finished at 23:27, but DynamoDB was still building the new `byAuthor` index until **23:34**.
- In between, My recipes returned an error, and the Lambda logged two failed loads, at 23:32 and 23:33 ("Cannot read from backfilling global secondary index").
- Nothing else was affected. Once active, the index held all 4 recipes.
- From now on, a new index ships in a deploy of its own, before the code that queries it. See [docs/operations.md](docs/operations.md) §4, "Changing the database".

## 2026-10-26 (later): support@ receives mail

### What changed

- **`support@familyfoodiebook.com` now receives mail.** The Privacy Policy and Terms send privacy, deletion, abuse and copyright requests there. Amazon SES receives it in us-east-2, keeps the raw message in a private S3 bucket, and a small Lambda (`backend/mail_forwarder/forwarder.py`) forwards it to the owner's inbox.
  - The copy comes from `"<sender> via Family Foodie Book" <support@familyfoodiebook.com>`, with Reply-To set to the sender, so Reply in the inbox answers them (from the inbox's own address).
  - Mail SES marks as a virus or spam, or that fails its sender's DMARC `reject` policy, is kept in the bucket but not forwarded.
  - Only `support@` and `dmarc-reports@` have receipt rules; SES rejects mail for any other address.
  - The forwarding address is in Parameter Store (`/recipe-book/support-forward-to`), not in Git or the CloudFormation template. The forwarder reads it for each message, so changing it needs no deploy.
- **New DNS records**, all from the stack (`_support_mail()` in `infra/recipe_stack.py`). No existing record changes:
  - MX → `inbound-smtp.us-east-2.amazonaws.com`;
  - SPF `v=spf1 include:amazonses.com ~all`;
  - DMARC `p=none`, with reports to `dmarc-reports@` (stored in the bucket, not forwarded);
  - three Easy DKIM CNAMEs;
  - MX and SPF for the MAIL FROM subdomain `mail.familyfoodiebook.com`.
- That's also **step 1 of [docs/plans/email-with-ses.md](docs/plans/email-with-ses.md)** (domain identity, DKIM, MAIL FROM, DMARC), so moving Cognito's emails to SES later needs no more DNS work.
- The **mail bucket is RETAINed** like the table and user pool, because it holds real correspondence. Messages expire after 365 days.
- CloudFormation can't activate a receipt rule set, so a CDK custom resource calls `SetActiveReceiptRuleSet`, and deactivates it if the stack is removed.
- Turned on by `supportForwardToParameter` in `cdk.json` → `context.domain`. Without it, none of this is created.
- Hosted zone import switched to `PublicHostedZone.from_public_hosted_zone_attributes` so SES can add its DKIM records. It's a reference only; `cdk diff` shows no change to the existing records.
- **Docs:** [docs/operations.md §7](docs/operations.md#7-support-email-supportfamilyfoodiebookcom) (how it works, records, setup, testing, replying, cost), with the later sections renumbered; plus `files.md`, `architecture.md` (decision 16, costs) and the SES plan's status.

### Set up by hand (outside the stack, because they hold a personal address)

- The Parameter Store `String` `/recipe-book/support-forward-to`.
- An SES identity for the inbox address: SES is still in the **sandbox**, where it only sends to verified addresses. AWS emails that inbox a verification link to click.

### Tests

- New `backend/tests/test_mail_forwarder.py` (11 tests): header rewriting (decoded names, existing Reply-To, signature headers removed, body unchanged), the virus/spam/DMARC filter, and the handler's S3 read and SES send.
- `infra/tests/test_recipe_stack.py` (4 new tests): the mail records next to the unchanged site records; the rules (support@ stored then forwarded, dmarc-reports@ stored only) and their activation; the retained bucket and expiry; the forwarder reads only its one parameter, and no address other than the domain's own is in the template; nothing is created without the setting.
- Full suite: 66 passed. `cdk diff` against the live stack: 24 resources added, nothing modified or replaced.

### Not yet

- **Replying as support@** needs SES production access and a sending path; until then, replies come from the inbox's own address.
- After a few weeks of clean DMARC reports, move DMARC from `p=none` to `p=quarantine`.

## 2026-10-26: Privacy Policy, Terms of Service, sign-up agreement

### What changed

- **New Privacy Policy** (`frontend/privacy.html`, at `/privacy.html`). It's written from what the site actually does:
  - What's collected: email, display name, recipes, comments, families and appearance settings.
  - Passwords go only to Amazon Cognito; the site never receives or stores them.
  - Location, if you turn it on, is reduced in the browser to "north" or "south".
  - Request logs (with IP addresses) are kept 30 days. For signed-out visitors, short-lived rate-limit counters are kept per IP address.
  - No cookies, analytics, ads or tracking, and data is never sold.
  - Who can see public and family recipes and comments; email addresses are never shown.
  - AWS hosts everything, in the United States.
  - Retention, and the reader's rights. Copies of data, corrections and account deletion are by email to `support@familyfoodiebook.com`, answered within 30 days.
  - Legal bases for EU and UK visitors, children (13+), and security.
- **New Terms of Service** (`frontend/terms.html`, at `/terms.html`):
  - Age: 13 or older, with a parent's or guardian's permission under 18.
  - Account rules.
  - You keep ownership of your recipes. You grant a license to host and show them, limited to the visibility you choose.
  - How families work: invite codes, the owner, leaving, publishing.
  - Community rules.
  - A **food-safety and allergy notice**.
  - Moderation, and a copyright-complaint process.
  - "As is" disclaimers, a limit on liability, changes, and contact.
  - No governing-law clause for now.
- Both open with a plain-language **"short version"** and a linked contents list, and follow the season theme and dark mode. They're plain HTML pages, not `#/` routes, so they have stable URLs and work without JavaScript.
- **Footer links** to both on every page, signed in or out. They're plain HTML, so they show before the app loads. Signed-out visitors also still see Appearance there.
- **Sign-up now has two required checkboxes**, under "Before you join":
  - "I'm 13 or older and agree to the Terms of Service";
  - "I've read the Privacy Policy".

  Each link opens in a new tab, so the form isn't lost. The browser blocks the form until both are ticked, and the site checks again before contacting Cognito, using its own error message.

### Tests

- `backend/tests/test_frontend.py`:
  - all three pages link only files that exist;
  - no page has inline scripts, styles or event handlers, which the CSP blocks;
  - every footer links both policies;
  - every `#anchor` in the policies points at a real heading, including links between the two pages;
  - both have an "Effective" date and the support address;
  - sign-up has both required checkboxes and checks them before calling Cognito.
- Full suite: 51 passed.
- Manual checks on the local dev server:
  - Submitting without the boxes ticked stops at the browser's check.
  - With `required` removed, the site's own error message appears and no account is created.
  - With both ticked, sign-up continues to "Check your email".
  - Clicking a policy link doesn't tick its box; clicking the label text does.
  - Both pages checked in dark mode at desktop width, and in light mode at 375 px with no sideways scrolling.
  - Signed in, the footer shows only the two policy links.
  - No console errors.

### Before relying on these

- **`support@familyfoodiebook.com` doesn't receive mail yet.** Set up forwarding (or SES receiving) before deploying, because both documents send privacy and deletion requests there.
- These were written for this site, but they're not legal advice. Have them reviewed, especially before adding payments.

## 2026-10-25 (night): Site-styled confirmations, favicon

### What changed

- **The site's own confirmation dialog replaces the browser's pop-up** (`confirmDialog()` in `frontend/app.js`, styles under "Confirm dialog" in `frontend/styles.css`).
  - All six confirmations use it: publishing a family recipe, deleting a recipe, deleting a comment, making a new invite code, leaving a family, and removing a member.
  - Each has a clear title, a short explanation, and a button that names the action ("Publish to public", "Delete recipe", "Leave family"…) instead of a generic OK.
  - The dialog follows the season theme and dark mode: the season icon at the top, the page behind dimmed in the header color and slightly blurred, and a short fade-in that's skipped for people who turn on "reduce motion".
  - **Destructive actions** (delete, leave, remove) get a red button, and the focus starts on **Cancel**, so pressing Enter by accident doesn't delete anything. For non-destructive actions, the focus starts on the action button.
  - It's built on the HTML `<dialog>` element opened with `showModal()`. Keyboard focus stays inside it, the page behind can't be clicked, and screen readers announce it by its title.
  - **Escape**, clicking outside the dialog, and **Cancel** all dismiss it. Escape is also handled in code, because Chrome sometimes skips its built-in Escape-to-close for dialogs.
  - **Follow-up fix, found while checking the live site.** The first version waited for the dialog's `close` event to report the answer. Browsers send that event later, as a queued task, and in a background or hidden tab it didn't arrive at all. When that happened, the dialog closed but the action never got its answer, and the invisible dialog element stayed on the page. Now every way out reports the answer and removes the dialog immediately: Cancel, the action button, Escape, the browser's own `cancel` event, and a click outside. The `close` event is only a backup.
  - Small wording fix: the publish message now says "It moves from The Washingtons to Public recipes" instead of "The Washingtons's recipes".
- **Favicon: fixes the `403 /favicon.ico` console error.**
  - Browsers request `/favicon.ico` on their own. The site had none, and the private S3 bucket behind CloudFront answers a missing file with **403** (it can't reveal whether the file exists), not 404.
  - The site now has an icon of an open cream recipe book with an autumn leaf, on the header's rust gradient. It comes in three files:
    - `frontend/favicon.svg`: the source design, used by modern browsers;
    - `frontend/favicon.ico`: 16, 32 and 48 px, for the automatic `/favicon.ico` request and older browsers;
    - `frontend/apple-touch-icon.png`: 180 px, for iPhone and iPad home screens.
  - `index.html` links all three. The `.ico` and `.png` were rasterized from the SVG's shapes, so they match it.

### Tests

- New `test_index_html_references_files_that_exist` (`backend/tests/test_frontend.py`): every file `index.html` links must exist in `frontend/`, and `favicon.ico` must exist. On the live site, a missing file shows up as a 403.
- Full suite: 38 passed.
- Manual checks on the local dev server:
  - The publish dialog: Escape, an outside click and Cancel all leave the recipe unchanged; Enter on the focused button publishes it. The sample recipe was then restored to family-only.
  - The delete dialog at 375 px in light mode: a red button, focus on Cancel, and it fits the screen.
  - All three icons are served (200) with the right content types, and there are no console errors.

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

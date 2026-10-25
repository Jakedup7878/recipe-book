# Changelog

Notable changes to Recipe Book. Newest first.

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

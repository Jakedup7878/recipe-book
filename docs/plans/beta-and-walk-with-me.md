# Plan: a beta environment, then "Walk with me"

**Goal:**
1. A `beta` branch that deploys automatically to its own beta site, with separate accounts and data, for you and your testers.
2. The **Walk with me** feature, built on a feature branch and tested on beta:
   - a step-by-step cooking view;
   - a timer for any step that needs one ("Put the cookies in the oven for 30 mins" offers a 30-minute timer you start when the cookies go in).

Status: **planned, not built.** The owner is building both, so this is a guide rather than a spec. The file names and settings match the repository as of 2026-10-28.

## 1. How the pieces fit together

```
feature/walk-with-me ──PR──▶ beta ──push──▶ "recipe-book-beta" pipeline ──auto──▶ RecipeBookBeta stack
                                                                                   (own site, own accounts, own data)
                              beta ──PR──▶ main ──push──▶ "recipe-book" pipeline ──you approve──▶ RecipeBook
                                                                                   (familyfoodiebook.com)
```

Three decisions, and why:

- **Beta gets its own copy of everything:** database, accounts, API and website. That's a second instance of `RecipeBookStack`. Never point beta code at the production table: a beta bug that writes bad data should only damage test data. Testers sign up separately on the beta site.
- **Beta starts on its CloudFront address** (`https://dxxxx.cloudfront.net`), without a custom domain.
  - A `beta.familyfoodiebook.com` address would need a new certificate made by hand in us-east-1 (CloudFormation can't run there in this project), plus changes to the stack's domain code, which assumes the main domain plus `www`.
  - Bonus: work networks that block newly registered domains don't block `cloudfront.net`.
- **Beta deploys automatically, with no approval step.** Fast feedback is the point of beta. The diff is still printed in the Synth log, and the beta table and user pool are RETAINed like production's.

## 2. Set up beta (one time)

### Step 1: start a branch

```bash
git switch main
git pull
git switch -c setup-beta
```

### Step 2: add the beta stack

In `infra/app.py`, under the existing `RecipeBookStack(...)`:

```python
RecipeBookStack(
    app, "RecipeBookBeta",
    env=env,
    domain=None,   # CloudFront address; also means no SES mail setup (see the warning below)
    description="Family Foodie Book BETA: separate accounts and data for testing",
)
```

> ⚠️ **Don't give beta the `domain` settings.** They include `supportForwardToParameter`, which sets up SES mail receiving. SES allows only **one active receipt rule set per Region**, so a beta stack activating its own would silently switch off the one that delivers support@ mail for production.

### Step 3: let the build scripts deploy either stack

- `pipeline/buildspec-synth.yml`: replace `RecipeBook` in the `cdk synth` and `cdk diff` lines with `"${TARGET_STACK:-RecipeBook}"`.
- `pipeline/buildspec-deploy.yml`: the same in the `cdk deploy` line.

The `:-RecipeBook` default keeps the production pipeline working unchanged until the pipeline stack is updated in step 6.

### Step 4: two pipelines from one stack

In `infra/pipeline_stack.py`:

- **Keep the GitHub connection, the artifact bucket and the three CodeBuild projects as they are.** Both pipelines share them, so no second GitHub authorization is needed.
- **Move everything from `source_output = ...` to the `triggers=...` block into a loop or helper**, called once per pipeline, for example with:
  - `("main", "RecipeBook", "recipe-book", True)`;
  - `("beta", "RecipeBookBeta", "recipe-book-beta", False)`.

  For each one, vary:
  - the `branch`, in the source action and the push filter;
  - `pipeline_name`;
  - `environment_variables={"TARGET_STACK": codebuild.BuildEnvironmentVariable(value=target)}` on the Synth and Deploy actions;
  - whether the `Approve` stage is included.
- **Leave the production pipeline's construct IDs unchanged** (`"Pipeline"`, `"SourceActionRole"`, …). Add a suffix only for beta (`"PipelineBeta"`). Renaming an existing ID makes CloudFormation delete the production pipeline and build a new one, losing its run history. Changing an ID means replacing the resource.
- Put the beta branch name in `infra/cdk.json` context, next to `"branch": "main"`, and read it in `app.py` the same way `branch` is read today.

### Step 5: write a test first

Add to `infra/tests/test_pipeline_stack.py`, which already synthesizes the pipeline stack and checks its stages, and assert:
- there are two `AWS::CodePipeline::Pipeline` resources;
- the beta pipeline has no `Approve` stage;
- the production pipeline's logical ID still starts with `Pipeline`.

### Step 6: ship it, in this order

1. **Merge the branch to `main`.** Commit, push `setup-beta`, open a pull request into `main`, and merge it. The production pipeline runs. Its diff for `RecipeBook` should say **"There were no differences"**, because only build scripts and `app.py` changed. Approve it.
2. **Deploy the pipeline stack by hand.** The pipeline doesn't update itself.

   ```bash
   cd infra
   npx cdk diff RecipeBookPipeline --profile recipe-book
   ```

   The diff should only **add** things: a pipeline, roles and policy statements. If anything shows as replaced, stop and fix the construct IDs. Then:

   ```bash
   npx cdk deploy RecipeBookPipeline --profile recipe-book
   ```
3. **Create the `beta` branch from `main` and push it.** That starts the beta pipeline, which creates the whole `RecipeBookBeta` stack. Expect 10–15 minutes the first time; creating a CloudFront distribution is slow.

   ```bash
   git switch main
   git pull
   git switch -c beta
   git push -u origin beta
   ```
4. **Find the beta site's address:**

   ```bash
   aws cloudformation describe-stacks --stack-name RecipeBookBeta --query "Stacks[0].Outputs" --profile recipe-book --region us-east-2
   ```

   If `aws` complains, use the newer CLI's full path (see the troubleshooting table in [../operations.md](../operations.md)).

### Step 7 (optional, recommended): a "BETA" banner

Add an `environment` field to the `config.json` the stack generates (`s3deploy.Source.json_data` in `infra/recipe_stack.py`). Pass it in as a new stack parameter, and have `app.js` show a banner when it's `"beta"`. Testers then always know which site they're on.

## 3. Everyday workflow

```bash
git switch beta
git pull
git switch -c feature/walk-with-me
```

1. **Build** and commit on the feature branch. Test locally with `backend/local_dev.py` and the test suite.
2. **Open a pull request into `beta`.** Push the feature branch first. Merging it deploys to beta automatically.
3. **Test on beta.** Send testers the beta address, and collect feedback.
4. **Release.** Open a pull request from `beta` into `main` and use **"Create a merge commit"**, not squash. Squashing leaves beta looking permanently "ahead" of main. The production pipeline runs; review the diff and approve.
5. **Urgent fix on production?** Branch from `main`, fix it, merge it into `main`, then merge `main` back into `beta`, so beta doesn't undo the fix later.

Also:
- **Data changes go to beta first**, including the "index first, then the code that reads it" rule in [../operations.md](../operations.md) §4. Beta holds older data too, so it tests the upgrade paths.
- If your GitHub plan allows it (public repo, or GitHub Pro for a private one), add a branch rule so `main` only changes through pull requests.
- If you ever delete the beta stack, its RETAINed table and user pool stay behind; delete them by hand.

## 4. Building Walk with me

### Design

For a first version, **detect timers from the step text, in the browser.** There's no backend or database change, so it's a well-sized first feature. Letting authors set timers explicitly is the bigger, later change (milestone 7).

**Where the code goes:** a new `frontend/cook.js`, loaded with `defer` before `app.js` in `index.html`, exposing a global the way `theme.js` exposes `window.RecipeTheme`. Also add it to `SHELL` in `frontend/sw.js`, so the installed app has it offline (`test_service_worker_saves_every_file_the_app_needs` fails until you do). Keep the timer-finding function free of the DOM, and add this at the bottom so Node tests can load it:

```js
if (typeof module === "object") module.exports = { findTimers };
```

### Milestones

Finish each one, with its tests, before starting the next.

1. **`findTimers(stepText)`, test first.** Run the tests with Node's built-in `node --test`, wrapped in a pytest test (like the `node --check` test in `backend/tests/test_frontend.py`) so the pipeline runs them unchanged. Also add `cook.js` to that file's syntax-check list.

   | Step text | Expected |
   |---|---|
   | "Bake for 30 mins." | one timer, 30:00 |
   | "Simmer 1 hour 15 minutes" | one timer, 1:15:00 |
   | "Bake 10-12 minutes, until golden" | one timer, offering 10:00 (note the range) |
   | "Rest for half an hour" | 30:00 |
   | "Boil 8 minutes, then bake 25 minutes" | two timers |
   | "Preheat to 350°F", "a 9x13 pan", "2 cups" | **none** (avoid false matches) |
   | "Chill overnight" | none, or a "no timer" hint |

2. **The step-by-step view.**
   - A **Walk with me** button on the recipe page opens `#/cook/<id>`.
   - One step at a time, in large text, with "Step 3 of 8", Next and Back buttons (and arrow keys), an ingredients panel, and Exit.
   - Add `cook` to `route()`. `test_every_page_link_goes_to_a_page_the_router_handles` fails if a link points at a page the router doesn't handle.
3. **The timer.**
   - A chip under the step ("30:00 · Start") with Start, Pause and Reset. Several can run at once (pasta and sauce).
   - **Store when the timer ends** (`Date.now() + duration`) and recompute the time left each second; don't subtract 1 per tick. Browsers slow down timers in background tabs, the same cause as the confirm-dialog bug of 2026-10-25, so counting ticks drifts. The end-time approach stays exact.
   - Save running timers in `localStorage`, so a reload doesn't lose them.
4. **Telling the cook the time is up.**
   - A sound (Web Audio API), a flashing chip, and the tab title changing to "⏰ Done".
   - iPhones only play sound after a tap, so create the audio on the **Start** tap.
   - `navigator.vibrate` doesn't work in iPhone Safari; treat it as a bonus where it's supported.
5. **Keep the screen on.** Use the Screen Wake Lock API (`navigator.wakeLock.request("screen")`, Safari on iOS 16.4+).
   - Request it when cook mode opens, release it on Exit, and request it again when the page becomes visible: the browser drops it whenever the page is hidden.
   - **Be honest about the iPhone limit:** if the phone locks or the person switches apps, the page is paused, and the alarm can't sound until they come back. The time shown stays correct thanks to milestone 3.
   - A true lock-screen alarm needs a notification the phone shows itself. The site has been an installable web app since 2026-10-29, but on iPhone a web app can only get notifications pushed from a server (Web Push), not schedule its own. Native local notifications (the app wrapped with Capacitor) are the simpler route. That's a separate, later project.
6. **Styling and accessibility.**
   - Buttons at least 44 px tall, readable from across the kitchen, following the season themes and text-size setting.
   - All CSS in `styles.css`, since the Content Security Policy blocks inline styles.
   - An `aria-live` region, so screen readers announce "Timer done".
7. **Later: timers set by the author.** An optional timer per step, saved with the recipe. This is a backend change: the API models, the data, old recipes, and tests in `test_handler.py`. It's a good second project, using the patterns from multi-family sharing (see [../architecture.md](../architecture.md) §3).

### Done means

- The tests pass.
- It works on the beta site in iPhone Safari and desktop Chrome.
- At least one other person has cooked with it.
- `CHANGELOG.md` and `docs/files.md` are updated.

Then it's ready to go from beta to main.

When the feature branch is pushed, ask Claude to review it, as a senior engineer would review a pull request (bugs, edge cases, readability), while you keep the authorship.

# Operations: build, run, test, deploy

Commands are for **Windows PowerShell** from the repository folder, unless noted. The AWS CLI profile is `recipe-book` and the Region is `us-east-2`.

## 1. One-time setup on a new computer

You need:

| Tool | Version | Why |
|---|---|---|
| Python | 3.13 | Same as the Lambda runtime |
| Node.js | 24 | Runs the CDK command-line tool, and the frontend syntax-check test |
| AWS CLI | v2 | Deploying, approvals, checks |
| Git | any recent | Pushing to GitHub, which starts the pipeline |

Steps:

1. **Sign in to AWS**: `aws login --profile recipe-book`. It opens a browser. Sessions expire; when a command says your credentials expired, run it again.
2. **Create the Python environment** (in `infra`):
   - `python -m venv infra\.venv`
   - `infra\.venv\Scripts\python.exe -m pip install -r infra\requirements.txt -r backend\requirements-dev.txt`
3. **Install the pinned CDK command-line tool**: `cd infra; npm ci`.
4. **Bootstrap CDK** (only once per AWS account and Region; already done for this project): `npx cdk bootstrap aws://202518310652/us-east-2 --profile recipe-book`.

## 2. Run the app locally

```
infra\.venv\Scripts\python.exe backend\local_dev.py
```

- Open **http://127.0.0.1:8001** for the site, or **/docs** for the API's interactive page.
- **First run:** creates the test accounts (alice, bob, carol; the passwords are printed in the terminal), sample recipes, and two families.
- **Sign-up and reset codes** are printed in the terminal; no email is sent.
- **Data** is saved in `backend\.localdata\` and survives restarts. Use `--reset` to start over and `--empty` for a first run without sample data.
- **A different port:** set `$env:PORT = "8002"` first.
- **In the Claude desktop app**, the `recipe-book-local` entry in `.claude/launch.json` starts the same server in the preview pane.

## 3. Run the tests

```
infra\.venv\Scripts\python.exe -m pytest backend/tests infra/tests -q
```

There are 38 tests: API behavior, route parity between the gateway and FastAPI, frontend checks, the local dev server, and both CDK stacks. The pipeline runs the same command; Node.js must be installed or the two JavaScript syntax checks are skipped.

## 4. Deploy (the normal way: through the pipeline)

1. **Commit and push to `main`.** The push starts the `recipe-book` pipeline automatically.
2. **Test** runs the suite. **Synth** builds the templates and prints `cdk diff`.
3. **Review the diff.** CodePipeline console → `recipe-book` → Synth stage → *SynthAndDiff* → *View logs*. Look at what changes. For example, a front-end-only change should show only the `DeploySite` asset changing. Be wary of anything that **destroys** or **replaces** a resource.
4. **Approve** in the console (Approve stage → *Review* → *Approve*). The Deploy stage then deploys the exact templates you reviewed.
5. **Check the live site.** CloudFront's cache is cleared automatically; if the browser shows an old version, hard-refresh.

**Pipeline tips:**
- Runs are **queued**. If an unwanted run is sitting at the approval step, newer pushes wait behind it. Stop the unwanted run (*Stop execution → Abandon*) or reject its approval.
- If a run fails, open the failed stage's CodeBuild log. Test failures appear in the *Reports* tab too.
- Changes to `infra/pipeline_stack.py` aren't deployed by the pipeline. Deploy them by hand: `cd infra; npx cdk deploy RecipeBookPipeline --profile recipe-book`.

## 5. Deploy from scratch (a new AWS account or Region)

1. Complete the setup in §1.
2. **Pipeline stack**: in `infra/cdk.json`, set `context.github` (owner, repo, branch), then run `npx cdk deploy RecipeBookPipeline --profile recipe-book`.
3. **Approve the GitHub connection** in the console: Developer Tools → Settings → Connections → `recipe-book-github` → *Update pending connection*. It stays **Pending** until a person does this; no API can.
4. **Push to `main`**, or start the pipeline from the console, then review and approve. The first deploy creates the whole app.
5. **Optional custom domain**: see §6.

## 6. Custom domain (how familyfoodiebook.com was set up)

1. **Register** the domain at an outside registrar. Route 53 Domains doesn't accept Free-plan accounts.
2. **Create a Route 53 hosted zone** for it, then set the zone's **four nameservers** at the registrar.
3. **Request the certificate in us-east-1** (CloudFront requires that Region; the project can't run CloudFormation there, so this is done with the CLI): `aws acm request-certificate --region us-east-1 --domain-name <domain> --subject-alternative-names www.<domain> --validation-method DNS`.
4. **Add the two validation CNAMEs** that ACM lists to the hosted zone, and **keep them forever** so the certificate renews automatically. Wait for the status **ISSUED**; usually minutes after DNS is live.
5. **Put the settings in `infra/cdk.json`** under `context.domain`: `name`, `certificateArn`, `hostedZoneId`. Commit, push, and approve the deploy.
6. CloudFront now serves both names. The CloudFront Function redirects `www.` and `*.cloudfront.net` to the main domain, and CORS allows only that domain.

## 7. Monitoring and checking

| What | Where |
|---|---|
| API errors and timing | CloudWatch Logs → `RecipeBook-ApiFunctionLogs…` (one month of logs) |
| Every API request (route, status, latency, IP, auth error) | CloudWatch Logs → `RecipeBook-ApiAccessLogs…`, JSON lines |
| Pipeline runs and build logs | CodePipeline → `recipe-book`; CodeBuild → `recipe-book-test`, `-synth`, `-deploy` |
| Accounts | Cognito → user pool `us-east-2_aMAe1QAlk` |
| Data | DynamoDB → the `RecipeBook-Table…` table (Explore items) |
| Spend and credits | AWS Settings → Billing |

Quick live checks: `https://familyfoodiebook.com/` returns 200; `/favicon.ico` returns 200; the API's `/public/recipes` returns 200; `/me` with no token returns 401.

## 8. Troubleshooting (problems actually met on this project)

| Symptom | Cause | Fix |
|---|---|---|
| `EPERM` errors from `cdk synth` locally | OneDrive locks files in `infra\cdk.out` while syncing | Synthesize to a folder outside OneDrive: `npx cdk synth --output <a temp folder>` |
| `git commit -m "…"` cuts the message at quotes in PowerShell 5.1 | PowerShell passes quoted text to Git incorrectly | Write the message to a file and use `git commit -F <file>` |
| An AWS command in Git Bash says a log group "doesn't exist" | Git Bash rewrites `/aws/...` into a Windows path | Run it with `MSYS_NO_PATHCONV=1`, or use PowerShell |
| `ExpiredToken` / "credentials expired" | The `aws login` session ended | Run `aws login --profile recipe-book` again |
| Push to GitHub asks for credentials or fails | A stale stored credential | Push from a terminal; Git Credential Manager opens a browser sign-in |
| Local server won't start: port in use | An old server process is still running | Stop it, or set `$env:PORT` to another port |
| Console shows `403 /something` on the live site | The file doesn't exist (private S3 answers 403 for missing files) | Add the file to `frontend/`; `test_frontend.py` checks linked files exist |
| New pipeline run never starts | An earlier run is waiting for approval (QUEUED mode) | Approve, reject or stop the earlier run |
| Sign-up emails stop arriving | Cognito's built-in sender allows only about 50 emails a day | Switch Cognito to Amazon SES; see [plans/email-with-ses.md](plans/email-with-ses.md) |
| Sudden "Access Denied" on things that worked | The project's spend limit, or the Free plan's credits or end date | AWS Settings → Billing; check the plan and spend limit |

## 9. Cleanup

- **Remove the app**: `cd infra; npx cdk destroy RecipeBook --profile recipe-book`. The **DynamoDB table and Cognito user pool are kept** (RETAIN). Delete them by hand only if you really want the data and accounts gone.
- **Remove the pipeline**: `npx cdk destroy RecipeBookPipeline`.
- **Keep** the hosted zone, the certificate (and its validation records) and the CDK bootstrap stack (`CDKToolkit`) if you plan to redeploy.
- **Known leftovers** from an earlier teardown, both empty and safe to delete once you confirm: table `RecipeBook-TableCD117FA1-T61QGMDHM8PS` and user pool `us-east-2_bRqGjSz7A`.

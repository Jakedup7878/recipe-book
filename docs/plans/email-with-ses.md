# Plan: email with Amazon SES

**Goal:** send all of the site's email from `@familyfoodiebook.com` through **Amazon Simple Email Service (SES)**:
- **account emails** (sign-up codes, password resets), sent by Cognito;
- later, **opt-in emails**: the weekly world-meal digest, family invites by email, receipts for paid plans.

Status: **step 1 built** (2026-10-26) as part of receiving mail for `support@familyfoodiebook.com` (see [operations.md §7](../operations.md#7-support-email-supportfamilyfoodiebookcom)); **steps 2-5 planned, not built.** SES is included in the project's Free plan.

## 1. Why this is needed

- **The current sender won't scale.** Today Cognito sends sign-up and reset codes with its **built-in sender** (`no-reply@verificationemail.com`). AWS limits that to roughly **50 emails a day** per user pool, and says it's meant for testing, not production. One busy day of sign-ups, or a family inviting relatives, can hit the limit, and new people then can't confirm their accounts.
- **Your own domain is more trustworthy.** Mail from `@familyfoodiebook.com` is less likely to be marked as spam, and people trust it more.
- **Monetization needs it anyway.** Newsletters, the weekly digest and receipts all need a real sending service.

Cost: **$0.10 per 1,000 emails** (pay-as-you-go), plus $0.12 per GB of attachments. A thousand sign-ups costs a dime.

## 2. Setup, step by step

Everything except the production-access request can be defined in the CDK app (`infra/recipe_stack.py`), so it's reviewable and repeatable.

### Step 1: verify the domain in SES (us-east-2)

- Create an SES **domain identity** for `familyfoodiebook.com` in **us-east-2**, the same Region as the user pool. That's the best fit, and Cognito supports it.
- Turn on **Easy DKIM**: SES gives three CNAME records that let receiving servers check the mail really came from us. The CDK's SES email-identity construct can add these to the Route 53 zone automatically.
- Add a **custom MAIL FROM domain** such as `mail.familyfoodiebook.com`, which needs one MX record and one SPF TXT record. Bounces then go to our domain, and SPF and DMARC line up.
- Add a **DMARC** TXT record at `_dmarc.familyfoodiebook.com`. Start in report-only mode (`p=none`, with reports to an address you read). After a few clean weeks, move to `p=quarantine`. Gmail and Yahoo require DMARC from bulk senders.

**Done (2026-10-26).** `_support_mail()` in `infra/recipe_stack.py` creates the domain identity with Easy DKIM, `mail.familyfoodiebook.com` as MAIL FROM, an apex SPF record (`v=spf1 include:amazonses.com ~all`), and DMARC at `p=none` with reports to `dmarc-reports@familyfoodiebook.com`, stored in the mail bucket. Later steps reuse this identity; don't create a second one. Any new sender (another provider, for example) must be **added to the same apex SPF record**, because a domain can have only one.

### Step 2: request production access

- New SES accounts start in the **sandbox** in each Region: mail can only go to **verified** addresses, at most **200 a day** and **1 per second**. Cognito can't email real users until this is lifted. (The support@ forwarder works in the sandbox only because its one destination is verified. Replying *as* support@ also needs production access.)
- Request production access in the SES console (Account dashboard → *Request production access*). Describe the use honestly and specifically:
  - **transactional**: sign-up confirmation codes and password resets for familyfoodiebook.com;
  - **later, opt-in only**: a weekly recipe digest;
  - how bounces and complaints are handled (step 4);
  - that there's an unsubscribe link and that people opt in.
- AWS reviews by hand, usually within about a day. It may ask questions; answer them in the case.

### Step 3: switch Cognito to SES

- In the user pool settings, change the email sending from *Cognito* to *SES* (`DEVELOPER` mode):
  - From: `Family Foodie Book <no-reply@familyfoodiebook.com>`;
  - Reply-to: `support@familyfoodiebook.com`, which receives mail and forwards it to the owner's inbox;
  - the SES Region: us-east-2;
  - the SES configuration set from step 4.
- Cognito then sends through **our** SES account, with our domain and our sending limits.
- Cognito creates a **service-linked role** to call SES. That needs permission to create service-linked roles. **Test this step first**: the project's Free-plan policies already blocked one kind of IAM change (identity providers), so confirm this one is allowed before planning around it.
- **Customize the message templates**, so the code email says "Family Foodie Book", explains what the code is for, and matches the site.
- In CDK this is the user pool's `email` setting (Cognito's "with SES" option, using the verified domain and from-address). The user pool is **RETAINed** and this setting can be changed in place, so no accounts are affected.

### Step 4: handle bounces and complaints

Sending reputation is what keeps mail out of spam folders, and SES watches it closely. AWS reviews accounts whose **bounce rate goes above about 5%** or whose **complaint rate goes above about 0.1%**.

- Create an SES **configuration set** with event destinations for bounces, complaints and deliveries, sent to **EventBridge** or **SNS**.
- Add a small **Lambda** that:
  - marks the profile as `emailStatus: bounced` after a hard bounce, and stops sending to it;
  - marks the profile as `emailOptOut: true` after a complaint ("this is spam"), and stops all non-essential email to it.
- Turn on the **account-level suppression list** (bounces and complaints), so SES itself refuses to send to bad addresses.
- Set **CloudWatch alarms** on the bounce and complaint rates, emailing you through SNS well before AWS's thresholds.

### Step 5: opt-in email (digest, family invites by email)

- **Consent first.** A checkbox in the profile: "Email me the weekly world meal plan", off by default. Store `emailPrefs` on the profile.
- **Send from a Lambda** with the SES v2 `SendEmail` API, using an SES **template**, from `hello@familyfoodiebook.com`.
- **Keep account emails and newsletters apart.** Use a separate configuration set and, ideally, a subdomain such as `news.familyfoodiebook.com`, so a newsletter problem can't hurt the delivery of sign-up codes.
- **Include a working one-click unsubscribe**: the `List-Unsubscribe` and `List-Unsubscribe-Post` headers plus a visible link. Gmail and Yahoo require this from bulk senders. The link goes to a signed, per-person URL on the API that turns off `emailPrefs` with no sign-in needed.
- **Follow the law.** Under US CAN-SPAM, marketing email must include a **physical postal address** and must honor unsubscribes promptly. Other countries have stricter consent rules (GDPR in the EU, CASL in Canada). Consent-first, opt-in email covers most of them. This is general information, not legal advice.
- **Throttle** sends to below your SES per-second rate, for example with an SQS queue feeding the sending Lambda.

## 3. What changes in the code (summary)

| Where | Change |
|---|---|
| `infra/recipe_stack.py` | ~~The SES domain identity (DKIM, MAIL FROM) in the existing Route 53 zone~~ (done); configuration sets; the event destination and bounce/complaint Lambda; alarms; the user pool `email` setting; later the digest sender and its queue |
| `backend/recipe_api/main.py` | Email preferences in the profile model; a public, signed unsubscribe endpoint (rate-limited per IP); never sending to bounced or opted-out profiles |
| `frontend/app.js` | Email preferences on the profile page |
| Route 53 | ~~DKIM CNAMEs, MAIL FROM MX and SPF, DMARC (all through the CDK)~~ (done, with the support@ MX) |
| Tests | DNS records exist; the user pool uses SES with the right from-address; unsubscribe works and is signed; bounces and complaints mark the profile |

## 4. Order of work

1. ~~Domain identity, DKIM, MAIL FROM and DMARC (`p=none`). Wait until the domain shows **verified**.~~ Done with support@ receiving.
2. Request production access. Meanwhile, test by sending to your own verified address.
3. The bounce/complaint handling, suppression list and alarms. **Before** sending to anyone else.
4. Switch Cognito to SES and customize the templates. Test sign-up and password reset end to end.
5. Later: opt-in digest emails with unsubscribe.

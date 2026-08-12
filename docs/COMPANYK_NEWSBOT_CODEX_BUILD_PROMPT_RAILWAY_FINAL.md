# Company K Newsbot — Greenfield Development Prompt

You are building a **new Company K portfolio news bot from scratch**.

This is a **greenfield build**.  
Do **not** ask for, inspect, or reuse an existing repository unless I explicitly provide one later.

## Files I will provide

Treat the following as project inputs:

1. `keyword_map_FINAL.yaml`
   - This is the **frozen source of truth for news-selection logic**.
   - Do not redesign, rewrite, simplify, normalize away, or silently “improve” its rules.
   - Do not hardcode company-specific keywords in Python when they already belong in this YAML.

2. `COMPANYK_NEWSBOT_GREENFIELD_HANDOFF_PROMPT.md`
   - This contains the project background, architecture, constraints, source strategy, testing strategy, email requirements, and recommended development order.
   - Read it fully before changing files.

3. If provided, a previous daily email `.eml` or screenshots
   - Use only as a **UX/layout reference**.
   - Do not inherit the old bot’s filtering logic from it.

---

# Objective

Build a Python news bot that runs automatically every morning as a **Railway Cron Job** and produces a concise HTML email containing:

### Route A — Direct Company News
News where a Company K portfolio company is directly mentioned.

Examples:
- financing
- IPO
- M&A
- earnings
- contracts
- clinical milestones
- production facilities
- executive changes
- regulation
- overseas expansion

### Route B — External Impact News
News where a portfolio company may **not be named**, but the event can materially affect that company through a specific registered exposure.

Examples:
- customer CAPEX
- competitor clinical/product/investment events
- regulation
- platform billing/API/SDK changes
- technical standards
- supply-chain / raw-material events
- market-demand changes
- comparable transactions

Route B must be driven by the exposure registry, event families, guards, and related logic in `keyword_map_FINAL.yaml`.

**Broad industry matching is prohibited.**

---

# Core Principle

The news-selection logic has already been designed and historically validated at the specification level.

Your job is **not to redesign the logic**.

Your job is to turn the frozen specification into a reliable runtime system:

```text
Collect
→ Normalize
→ Deduplicate
→ Generate Candidates
→ Route A / Route B
→ Materiality / Causal Judgment
→ Rank
→ Summarize
→ Render HTML Email
→ Send
```

If runtime results look wrong, diagnose the failure layer before changing the YAML:

```text
collector?
query generation?
normalization?
dedup?
YAML parser?
Route A implementation?
Route B candidate generation?
LLM judge?
ranking?
email rendering?
actual FINAL rule issue?
```

Do not edit the frozen rule map until a rule-level defect has been demonstrated separately.

---

# Technical Direction

Use Python.

Preferred baseline:

- Python 3.12+
- PyYAML
- pydantic
- httpx or requests
- feedparser
- BeautifulSoup4 where needed
- OpenAI Python SDK
- Jinja2
- python-dotenv
- pytest

Automation:

- Railway Cron Job
- GitHub is the source-code repository; Railway is the scheduled runtime.
- The Railway service must run one short-lived batch command and exit when the run finishes.
- Do not run an in-process scheduler or keep a 24/7 web server alive just to wait for the next execution.
- Railway cron schedules use standard five-field crontab expressions and are evaluated in UTC.
- Convert the approved KST delivery window to UTC explicitly in deployment configuration.
- Railway does not guarantee execution to the exact minute; a run may start a few minutes late.
- Therefore schedule the job with enough lead time for collection, judging, rendering, and email delivery rather than promising an exact-to-the-minute inbox arrival.
- If a previous cron execution is still active, Railway can skip the next scheduled execution. The application must therefore close all resources and terminate cleanly.
- Add an application-level maximum runtime/deadline so a hung collector, network call, or LLM request cannot leave the cron deployment active indefinitely.

Secrets:

- Never commit API keys, passwords, SMTP credentials, recipient addresses that should remain private, or other secrets.
- Use `.env` only for local development and keep it gitignored.
- Use Railway Variables for deployed configuration and secrets.
- Do not print secret values in application logs.

---

# Target Repository Shape

Start with a clean structure similar to:

```text
companyk-newsbot/
├─ config/
│  └─ keyword_map_FINAL.yaml
├─ src/
│  ├─ collectors/
│  ├─ rules/
│  ├─ judges/
│  ├─ dedup/
│  ├─ email/
│  ├─ models/
│  └─ main.py
├─ tests/
│  ├─ unit/
│  └─ fixtures/
├─ artifacts/
├─ .env.example
├─ .gitignore
├─ pyproject.toml
├─ railway.json
└─ README.md
```

You may adjust package-level details when technically justified, but keep responsibilities separated.

---

# Required Architecture

## 1. Configuration Layer

At startup:

- load `keyword_map_FINAL.yaml`
- parse it into typed models
- validate required structure
- fail fast if the configuration is inconsistent

Validation must include, where applicable:

- YAML parses successfully
- schema version exists
- company rules are present
- exposure IDs are unique
- required exposure fields exist
- referenced event families exist
- required guards / judge fields are structurally valid
- explicit zero-exposure declarations remain distinguishable from missing data

Do not silently skip malformed rules.

---

## 2. Article Model / Normalization

Create a canonical internal article model.

At minimum include fields such as:

```text
source
source_type
title
url
canonical_url
published_at
retrieved_at
description
body/text when available
language
query/origin metadata
```

Normalization should make later dedup and routing deterministic.

---

## 3. News Collection

Start simple and reliable.

Initial sources should prioritize:

1. Google News RSS
2. official company / regulator / institution RSS when available
3. targeted official feeds or searches

Do not build an unnecessarily complex scraper farm.

Collectors should be modular so additional sources can be added later.

External-impact Route B should prefer source quality in roughly this order:

1. regulator / government / court / standards body
2. customer / competitor / company official source
3. Reuters / major financial press
4. reputable trade media
5. general media

---

# Route A

Route A should be as deterministic as practical.

Conceptually:

```text
article
→ portfolio entity / alias match
→ event context
→ materiality
→ dedup / rank
```

Use aliases and company metadata from the YAML.

Do not spend an LLM call merely to confirm an obvious company-name match if deterministic rules are sufficient.

Avoid substring mistakes and ambiguous alias matches.

---

# Dedup Is a First-Class Feature

Repeated news was a major weakness of the previous bot.

Implement dedup before building the full Route B pipeline.

## Article-level dedup

At minimum consider:

- canonical URL
- normalized URL
- normalized title
- syndicated Google News duplicates
- same-outlet duplicate variants

## Event-level dedup

Different publications covering the same event should be clustered.

Example:

```text
SK hynix new fab investment
├─ company press release
├─ Reuters
├─ Korean financial media A
└─ Korean financial media B
```

The email should normally show one primary item and optionally indicate additional coverage.

Design event clustering as its own component rather than burying it inside email rendering.

---

# Route B

Route B must have two distinct stages.

## B1. Exposure-based Candidate Generation

Do not do:

```text
46 portfolio companies × every collected article
```

Instead normalize the exposure registry into a structure conceptually like:

```text
Exposure Subject
→ search/query terms
→ matching candidate articles
→ impacted portfolio companies
```

Candidate generation should be driven only by registered exposure logic.

Examples of exposure subjects may include entities or topics such as:

```text
Google Play
CXL
SK hynix
semaglutide
Disney+
LiPF6
```

Do not widen a specific exposure into generic industry news.

---

## B2. Causal / Materiality Judge

Only send **pre-filtered candidates** to the LLM judge.

The judge must determine:

1. Is this a real, material external event?
2. Does it match the registered exposure?
3. Is there a credible company-specific causal mechanism?
4. Is it merely broad industry commentary?
5. What is the likely impact direction?
6. What event family applies?
7. What materiality level applies?

Use structured output.

A result should conceptually resemble:

```json
{
  "qualifies": true,
  "company": "Example Company",
  "exposure_id": "example_exposure",
  "event_family": "policy_regulatory",
  "materiality": "high",
  "impact_direction": "mixed",
  "causal_mechanism": "..."
}
```

Rejected candidates should also return an explicit reason when possible.

Examples:

```text
broad_industry_only
wrong_context
non_material
wrong_jurisdiction
weak_causal_link
duplicate_event
```

Keep the judge prompt and output schema version-controlled.

---

# Ranking

The final email must not become a news dump.

Priority should generally be:

```text
high-materiality direct company news
→ high-materiality external-impact news
→ other material direct news
→ medium external-impact news
```

Low-context/noise items should normally be excluded.

Make thresholds and daily limits configurable rather than scattering magic numbers through code.

Possible controls:

- total max items
- max items per company
- minimum materiality
- maximum repeated coverage of one event

Do not over-optimize thresholds before shadow testing.

---

# Summaries

Generate concise summaries only after an item has qualified.

For direct news:

```text
What happened?
```

For Route B:

```text
What happened?
Why does this matter to this specific portfolio company?
```

The user-facing email must **not** expose:

- internal exposure IDs
- raw judge prompts
- internal scores
- debugging metadata

---

# Email

Initial HTML structure:

```text
[컴퍼니케이 데일리] YYYY-MM-DD

오늘의 주요 포트폴리오 뉴스: N건

1. 기업 직접 소식

[회사명]
기사 제목
한 줄 요약
출처 / 링크

2. 포트폴리오 영향 이슈

[영향 회사명]
외부 사건 제목
왜 중요한지 한 줄
출처 / 링크
```

Every Route B item must clearly explain:

> 왜 이 회사에 중요한지

Do not send to real Company K recipients during development.

Support separate modes:

```text
local
test
shadow
live
```

`live` must be impossible to trigger accidentally without explicit configuration.

---

# Railway Runtime / Deployment

Railway is the production scheduler and runtime for this project.

The intended deployment model is:

```text
GitHub repository
        ↓
Railway service deployment
        ↓
Railway Cron schedule
        ↓
single batch start command
        ↓
collect → judge → dedup → render → send
        ↓
persist state / audit result
        ↓
process exits
```

## Batch Process Contract

The production entry point should be a single command such as:

```text
python -m src.main
```

The exact command may change with packaging, but there must be one clear batch entry point.

The process must:

1. start;
2. validate configuration;
3. acquire any run/idempotency guard;
4. execute the daily pipeline;
5. persist the final run state;
6. close HTTP clients, database/SQLite handles, files, and other resources;
7. exit with code `0` on success;
8. exit non-zero on a failed run.

Do not keep a Flask/FastAPI server, infinite loop, sleep loop, APScheduler, `schedule`, or other long-running scheduler alive in production.

## Cron Schedule

Railway cron uses UTC.

Keep the business delivery time conceptually in KST, but convert it to UTC only at the deployment configuration boundary.

Do not scatter timezone conversions throughout application logic.

Do not claim exact-to-the-minute execution. Railway may start cron jobs a few minutes after the configured minute.

The production schedule should therefore target a **delivery window**, not an exact inbox timestamp.

If the desired email arrival time is `T`, schedule the batch sufficiently before `T` to cover:

- Railway start variance;
- news collection time;
- LLM judge time;
- dedup/ranking/summary time;
- email delivery time.

The exact lead time should be tuned during shadow runs from measured runtime.

## Overrun Protection

Railway may skip a scheduled execution if the previous execution remains active.

Therefore:

- every network request must have a timeout;
- LLM calls must have a timeout/retry policy;
- collector failures should be bounded;
- the whole daily run must have a maximum runtime;
- all opened resources must be closed;
- the process must terminate after completion.

A single hung article fetch must never keep the service alive until the next day.

## Railway Configuration

Prefer Railway config-as-code where it improves reproducibility.

A `railway.json` may define items such as:

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "deploy": {
    "startCommand": "python -m src.main",
    "restartPolicyType": "NEVER"
  }
}
```

Do **not** put an arbitrary production cron time into the initial scaffold just to fill the field.

At Step 10, set the actual `cronSchedule` only after the intended KST delivery window is known for the deployment.

If the cron schedule is configured in the Railway dashboard instead of `railway.json`, document the exact setting in the README so deployment is reproducible.

## Railway Variables

Deployed configuration should come from Railway Variables.

Examples may include:

```text
APP_ENV
RUN_MODE
OPENAI_API_KEY
EMAIL_SENDER
EMAIL_RECIPIENTS
SMTP_* or email-provider credentials
STATE_DIR
LOG_LEVEL
```

Never commit real secret values.

`local`, `test`, `shadow`, and `live` modes must remain logically separate.

A live recipient list must never be the default.

## Railway Persistent State

Do not assume the ordinary Railway deployment filesystem is durable.

For the production-light version, prefer a very small persistent Railway Volume for:

```text
sent article fingerprints
sent event fingerprints
recent event clusters
last successful run
run ledger
optional shadow artifacts
```

Keep the persistent storage implementation behind a small state interface so it can later be replaced by Postgres or another store if necessary.

The application must still function locally without Railway.

## GitHub Role

GitHub remains the repository and version-control source.

GitHub Actions is **not required for the daily production schedule**.

Do not add a scheduled GitHub Actions workflow as a second production scheduler.

If CI is added later, it may run tests/lint on pushes or pull requests, but it must remain separate from the Railway daily cron.

---

# State

Start without a heavy database unless runtime requirements prove one is needed.

Persist enough information to avoid repeated daily emails.

At minimum think about:

- previously sent canonical URLs
- article fingerprints
- event fingerprints
- last run
- recent event clusters

Railway service deployment filesystems are ephemeral unless persistent storage is attached, so do not rely on ordinary local files surviving future deployments.

For the first version, prefer the simplest robust state strategy:

1. use a small Railway Volume mounted at a documented path (for example `/data`) when practical;
2. keep durable state there using SQLite or compact JSON/JSONL rather than introducing a full database;
3. make the state path configurable through an environment variable such as `STATE_DIR`;
4. keep local development compatible by falling back to a repository-local ignored state directory.

If Railway Volume constraints later become a problem, abstract the state interface so it can be moved to an external database without rewriting routing logic.

The state layer must support idempotency. A retry or manual rerun of the same daily job must not resend already-sent events unless explicitly forced.

For the first version, prefer the simplest robust option and document its tradeoffs.

---

# Logging / Artifacts

Every run should leave a machine-readable and human-readable audit trail.

In Railway:

- emit structured, readable logs to stdout/stderr so they appear in Railway Logs;
- if a persistent Railway Volume is configured, also write compact run artifacts under the persistent state/artifact directory;
- do not depend on ephemeral deployment files for historical audit data.

Record at minimum:

```text
run timestamp
source counts
articles collected
articles after normalization
articles after article dedup
event clusters
Route A matches
Route B candidates
Route B accepted
Route B rejected
rejection reasons
final email item count
errors
```

This logging is required for shadow-test tuning.

Do not log secrets.

---

# Testing Strategy

Implement three layers.

## Unit tests

Cover at minimum:

- YAML loader
- configuration validation
- alias matching
- query generation
- URL/title normalization
- article dedup
- event dedup
- Route A matching
- email rendering

## Fixture tests

Create representative fixtures for:

- Route A positive
- Route B positive
- broad-industry negative
- adversarial negative
- ambiguous alias
- direct-route / external-route overlap
- duplicate article
- duplicate event cluster

Where historical validation examples are available in the supplied material, reuse them rather than inventing easier cases.

## Shadow runtime

The real quality test is:

```text
today's collected news
→ what matched?
→ which route?
→ why accepted?
→ why rejected?
→ what duplicates were collapsed?
→ what would appear in the email?
```

Shadow mode must run without emailing real recipients.

---

# Development Order

Work in this order unless a concrete dependency requires a small adjustment:

### Step 0
Repository scaffold, packaging, config locations, test runner, and Railway-compatible batch entry-point/config skeleton. Do not deploy or schedule the service yet.

### Step 1
Typed YAML loader + fail-fast validation.

### Step 2
Article model + first news collector.

### Step 3
Route A direct-company detection.

### Step 4
Article-level and initial event-level dedup.

### Step 5
Route B exposure normalization + candidate generation.

### Step 6
Structured LLM causal/materiality judge.

### Step 7
Ranking + concise summary generation.

### Step 8
HTML email renderer.

### Step 9
Local end-to-end run.

### Step 10
Railway deployment + Railway Cron schedule + persistent state handling.

### Step 11
Shadow daily runs and diagnostic tuning.

Do not jump straight to a large end-to-end script.

---

# Step 10 Railway Acceptance Criteria

When Step 10 is eventually implemented, it is not complete until all of the following are true:

- the GitHub repository is connected to the intended Railway service;
- Railway can build the project reproducibly;
- the production start command launches exactly one batch run;
- the service terminates successfully after the batch finishes;
- the cron expression is documented and verified as UTC;
- the corresponding intended KST delivery window is documented;
- deployed secrets/configuration come from Railway Variables;
- persistent state survives a new deployment/restart test;
- rerunning the same fixture/day does not duplicate previously sent events;
- shadow mode cannot send to live Company K recipients;
- a forced failure exits non-zero and leaves useful Railway logs;
- a hung external request is bounded by timeout;
- no scheduled GitHub Actions workflow exists as a competing production scheduler.

Railway cron timing should be measured during shadow operation. If observed start variance or pipeline runtime threatens the desired inbox window, adjust the cron lead time rather than moving scheduling logic into the Python application.

---

# How You Should Work

You are acting as the implementation engineer.

For each step:

1. inspect the relevant source-of-truth files
2. state the concrete implementation boundary
3. implement it
4. add or update tests
5. run the relevant tests
6. fix failures
7. briefly report:
   - files changed
   - tests run
   - what is now working
   - remaining risk / next step

Do not stop merely because the first implementation compiles.

Do not ask me to decide routine engineering details that can be resolved from this specification.

Prefer small, reviewable modules over clever abstractions.

Do not introduce unnecessary infrastructure.

---

# Critical Constraints

- Greenfield repository.
- Do not request an old repo.
- Railway Cron is the production scheduler/runtime for the daily batch.
- GitHub is source control, not the production daily scheduler.
- Do not create a scheduled GitHub Actions workflow for production.
- The Railway cron process must be short-lived and exit cleanly after each run.
- Do not assume exact-to-the-minute Railway cron execution.
- Protect against overruns because an active previous execution can cause the next Railway cron execution to be skipped.
- `keyword_map_FINAL.yaml` is the frozen logic source of truth.
- No portfolio-company keyword duplication in source code.
- Broad-industry matching is forbidden for Route B.
- Route B requires a specific registered exposure and causal link.
- Dedup is mandatory, including event-level dedup.
- Secrets never enter the repository.
- Never send development emails to actual Company K recipients.
- Preserve a shadow/test path before live mode.
- Configuration failures should fail the run instead of silently producing a bad email.
- Diagnose runtime implementation failures before touching the FINAL rule map.

---

# First Task

Start only with:

## Step 0 + Step 1

Create the clean repository scaffold and implement the typed loader / validator for `keyword_map_FINAL.yaml`.

Do **not** implement collectors, Route A, Route B, OpenAI calls, email sending, Railway deployment, or the production cron schedule yet.

At Step 0, only make the repository structurally Railway-compatible (for example, a single batch entry point and optional minimal `railway.json` skeleton). Actual deployment belongs to Step 10.

Before coding:

1. inspect the YAML structure
2. summarize the actual top-level schema and important nested structures you found
3. identify any schema assumptions you will encode
4. then implement the loader and validation layer

Acceptance criteria for this first task:

- repository installs cleanly
- `keyword_map_FINAL.yaml` is loaded from config, not duplicated
- typed models exist
- invalid/missing required structures cause a clear failure
- valid config passes
- tests cover success and several malformed-config cases
- README explains how to run config validation
- all tests pass

At the end, stop and report the Step 0 + Step 1 result. Do not continue to Step 2 until instructed.

# Company K Newsbot — Day 3 Development Record

**Date:** 2026-08-12  
**Scope:** greenfield development, GitHub source control, Railway deployment, and controlled email-delivery verification.  
**Vault status:** not updated. This is a project deliverable only.

## 1. What we built

We built the foundation of a daily Company K portfolio-news bot. The intended system is a short-lived Railway batch job that gathers portfolio-relevant news, filters it through deterministic Company K rules, uses OpenAI only where causal judgement or Korean summarisation is needed, renders a Korean HTML email, and eventually sends it each morning.

The frozen rule file at `config/keyword_map_FINAL.yaml` is the single runtime source of truth. Company names, aliases, external exposures, valid dates, event families, exclusions, and matching context are deliberately **not duplicated in Python**.

```text
Frozen FINAL rule map
        ↓
Google News RSS collection
        ↓
Article normalisation + duplicate removal
        ↓
Route A: direct portfolio-company news
Route B: registered external exposure news
        ↓
Route B OpenAI causal/materiality check
        ↓
Ranking + Korean structured summaries
        ↓
Korean HTML email rendering
        ↓
Resend delivery → jeremy.cheon@pm.me
```

## 2. Project structure created

```text
companyk-newsbot/
├─ config/keyword_map_FINAL.yaml       # frozen Company K rule map
├─ src/companyk_newsbot/
│  ├─ collectors/                      # Google News RSS collector
│  ├─ models/                          # canonical Article data model
│  ├─ rules/                           # Route A and Route B rule logic
│  ├─ dedup/                           # article and event de-duplication
│  ├─ judges/                          # OpenAI structured judgement/summaries
│  ├─ email/                           # HTML renderer + Resend adapter
│  ├─ ranking.py                       # daily prioritisation and limits
│  ├─ pipeline.py                      # in-memory end-to-end orchestration
│  ├─ state.py                         # durable run/sent-item state model
│  └─ main.py                          # Railway batch entry point
├─ tests/                              # 36 automated tests
├─ Dockerfile                          # deterministic Railway build/install
├─ railway.json                        # Railway batch deployment config
└─ docs/
   ├─ COMPANYK_NEWSBOT_CODEX_BUILD_PROMPT_RAILWAY_FINAL.md
   └─ DAY_3_WORK_LOG_2026-08-12.md
```

## 3. Development completed

### 3.1 Typed configuration and fail-fast validation

- Loaded the frozen YAML through Pydantic models instead of reading unvalidated dictionaries throughout the codebase.
- Validated the key schema: companies, aliases, external exposures, zero-exposure closures, globally unique exposure IDs, evidence metadata, `valid_from` dates, and event-family references.
- Kept an explicit distinction between:
  - a confirmed company with no justified external exposure; and
  - missing/incomplete exposure data.
- Added a command-line configuration validation path.

### 3.2 Canonical article collection

- Created one typed `Article` model for all news sources.
- Implemented a Google News RSS collector using explicit queries only.
- Normalised titles/descriptions, parsed publication timestamps, stripped common tracking parameters, and retained provenance such as the originating query and feed URL.

### 3.3 Deterministic Route A: direct company news

- Implemented matching against the company name plus YAML aliases.
- Added boundary handling to avoid English substring false positives.
- Honoured YAML guards for forbidden standalone terms, required context, negative terms, Korean spacing variation, and English ambiguity context.
- Route A is deterministic and does not call OpenAI.

### 3.4 De-duplication

- Added article-level de-duplication using canonical URLs and normalised titles.
- Kept duplicate audit reasons rather than silently dropping evidence.
- Added Route A event clustering for cross-publication coverage of the same company event.
- Preserved potentially conflicting numerical facts as separate events instead of collapsing them incorrectly.

### 3.5 Deterministic Route B: external-impact candidates

- Built an exposure registry from only the YAML’s registered external exposure query terms.
- De-duplicated collector queries while retaining every relevant company/exposure link.
- Rejected unregistered queries and articles older than the exposure’s `valid_from` date.
- Kept Route B candidate generation separate from causal/materiality judgement.

### 3.6 OpenAI structured judgement and summaries

- Added a structured Route B causal/materiality judge using the OpenAI Responses API and Pydantic parsing.
- The model can qualify only the specific pre-registered company/exposure path; it cannot invent new exposures.
- Enforced output contracts for company, exposure ID, event family, materiality, impact direction, and rejection state.
- Added a second structured Korean summary step after qualification/ranking.
- Route B summaries require a short company-specific `why_it_matters`; internal IDs, prompts, and scoring metadata are never included in the email.
- Railway defaults were set to:
  - `OPENAI_MODEL=gpt-5.6-sol`
  - `OPENAI_REASONING_EFFORT=medium`

### 3.7 Ranking and presentation

- Added deterministic ranking with an overall daily cap and a per-company cap.
- Prioritisation order distinguishes direct company news from external-impact news and gives high-materiality items priority.
- Built a Korean HTML email with two visible sections:
  1. 기업 직접 뉴스
  2. 포트폴리오 영향 뉴스
- HTML content is escaped before rendering to protect the email body from untrusted article text.

### 3.8 State model

- Added an atomic JSON state store with a run ledger and sent article/event fingerprints.
- The state model supports future durable de-duplication once a Railway Volume is attached.

### 3.9 Delivery adapter and controlled test

- Added a direct Resend HTTP delivery adapter.
- Added `RUN_MODE=test`, which sends a **template-only, zero-news** email and does not invoke OpenAI.
- Confirmed actual end-to-end delivery to `jeremy.cheon@pm.me`.
- Received test subject:
  - `[Company K] 포트폴리오 데일리 뉴스 | 2026-08-12`
- Confirmed sender:
  - `Company K Newsbot <onboarding@resend.dev>`

## 4. Deployment and infrastructure work

- Created private GitHub repository `dahyeong0323/companyk-newsbot`.
- Connected Railway project `innovative-integrity`, production service `companyk-newsbot`, to `main` with auto-deploy enabled.
- Stored the Railway-based FINAL build specification in this repository.
- Configured Railway variables for execution mode and OpenAI model selection.
- Kept `OPENAI_API_KEY` and `RESEND_API_KEY` out of Git; secrets live only in Railway Variables.
- Added an explicit `Dockerfile` so Railway always:
  1. copies the repository,
  2. runs `python -m pip install --no-cache-dir .`, and
  3. starts `python -m companyk_newsbot.main`.

## 5. Problems encountered and how they were fixed

| Problem | Why it happened | Fix |
| --- | --- | --- |
| Railway rejected the first deployment | `restartPolicyMaxRetries` was set to `0`, but Railway requires at least `1`. | Changed it to `1`. |
| Railway could not import `companyk_newsbot` | Railpack did not install the project package before start. | Switched to a root `Dockerfile` with explicit `pip install .`. |
| Installed package could not find the frozen YAML | The original path was based on the installed package location. | Resolve `config/keyword_map_FINAL.yaml` from the application working directory. |
| Initial delivery attempts produced no email | The application never reached the delivery code because of the import failure. | Fixed package build/start first, then reran a controlled test. |
| Railway Raw Editor removed secret variables | Replacing the entire raw variable block omitted existing keys. | Restored the keys directly with Railway’s normal Variable UI; avoid full Raw Editor replacement. |

## 6. Verification evidence

- Full automated test suite: **36 passed**.
- Installed-package local check succeeded:

```powershell
python -m pip install .
python -m companyk_newsbot.main
```

- Railway Dockerfile deployment completed successfully.
- Railway deploy log confirmed: `Test email accepted by Resend.`
- Proton Mail received the rendered Company K template email.

## 7. Current operational state

What works now:

- GitHub → Railway automatic deployment.
- Deterministic data/rules/pipeline components are implemented and tested.
- Resend delivery is proven end to end.
- A safe template-only test mode exists.

What is intentionally not yet enabled:

- The Railway `main.py` entry point does **not yet call the full collector → pipeline → OpenAI → delivery path** in production.
- No Railway Volume has been attached for durable sent-item de-duplication.
- No Railway Cron schedule is configured.
- `RUN_MODE=live` remains deliberately blocked until the real-news shadow run is completed and reviewed.

## 8. Security and working rules

- Never commit real API keys or `.env` files.
- Keep `OPENAI_API_KEY` and `RESEND_API_KEY` in Railway Variables only.
- Treat the OpenAI key previously pasted into chat as exposed and rotate it.
- Do not use Railway Raw Editor to overwrite the full variable set.
- Do not update the Obsidian Vault unless explicitly requested.

## 9. Next development sequence

1. Wire Google News collection, Route A/B processing, OpenAI judgement/summaries, ranking, and Resend delivery into the Railway entry point.
2. Attach a Railway Volume and set `STATE_DIR=/data` for persistent fingerprint state.
3. Run a real-news shadow report and inspect its selected items before enabling email delivery.
4. Enable `RUN_MODE=live` only after the shadow run is approved.
5. Add Railway Cron for **09:00 KST** once the real runtime duration is measured.

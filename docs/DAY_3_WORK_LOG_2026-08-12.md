# Company K Newsbot — Day 3 Development Record

**Date:** 2026-08-12
**Classification:** personal_work / internal development record
**Scope:** Company K portfolio-news bot development, GitHub/Railway deployment, controlled email verification, real-news E2E, and Luna-primary judge preparation.
**Vault status:** This is a repository document. The Obsidian Vault was not updated during development unless separately requested.

## 1. Today’s outcome

We progressed from a repository scaffold to a deployed, non-live news pipeline that has completed both a controlled email test and a real Full Shadow run.

The system now has:

```text
Frozen keyword map
→ Google News RSS collection
→ freshness filter
→ article deduplication
→ Route A direct-company detection + event clustering
→ Route B registered exposure candidate generation
→ causal/materiality LLM judgement
→ ranking
→ Korean summary + why_it_matters
→ HTML email rendering
→ Resend delivery (test recipient only)
```

At the end of Day 3, the next-generation Route B architecture was developed locally:

```text
Luna / medium primary judge
→ clear ACCEPT or REJECT ends at Luna
→ ambiguity or Luna technical failure goes to Sol / medium
→ per-candidate audit trail and metrics
```

It is committed locally and fully tested, but was deliberately **not pushed, deployed, or run as a new Full Shadow**. This prevents an accidental costly Railway run before Day 4 review.

## 2. Timeline: what happened, what broke, and how it was resolved

| Stage | What happened | Result / lesson |
| --- | --- | --- |
| Repository setup | Created private GitHub repo `dahyeong0323/companyk-newsbot`; Railway service connected to `main`. | GitHub is source control; Railway is the future scheduler/runtime. |
| Initial Railway deploy | Railway rejected `restartPolicyMaxRetries: 0`. | Railway requires at least `1`; changed it to `1`. |
| First successful build, failed start | Railway could not import `companyk_newsbot`. | Railpack had not installed the project package. Added an explicit Dockerfile with `pip install .`. |
| Package/config path issue | Installed package could not locate `config/keyword_map_FINAL.yaml`. | Configuration resolution was corrected to use the application working directory. |
| Controlled Resend validation | `RUN_MODE=test` sent a zero-news HTML template only to `jeremy.cheon@pm.me`. | Resend delivery, sender configuration, and HTML rendering were proven end-to-end. |
| Railway variable edits | Replacing the Raw Editor variable block hid/removed existing variables. | Restored variables in the normal Railway Variable UI. Do not replace the full raw-variable block without preserving every key. |
| First real E2E failure | Startup command used `python -m companyk_newsbot.main` before package path was correct. | Fixed packaging/start configuration and redeployed. |
| Real smoke E2E | A controlled real-news test ran with Google News RSS and OpenAI. | Email was received with Route A/Route B content. Smoke evidence: 89 collected, 83 after dedup, 1 Route A, 83 Route B candidates, 25 judged, 1 Route B accepted, 2 final email items. |
| Freshness debugging | Early real-news runs could return zero items because news timestamps fell outside a static window. | Implemented production-style freshness windows: last successful delivery checkpoint minus overlap, otherwise a 30-hour first-run window. |
| Full Shadow baseline | Ran all 46 direct-company queries and all 159 exposure queries: 205 total. | Sol-only judge processed all 1,525 Route B candidates. The run completed successfully without email or production-checkpoint mutation. |
| Artifact loss discovered | Full Shadow JSON/HTML was written under `.state` inside a short-lived Railway container. Once the job ended, its filesystem was gone. | Only aggregate logs remain. The exact 11 selected items and all 1,525 detailed judgements cannot be recovered. This was the main Day 3 operational failure. |
| Luna cascade development | Built Luna-primary, Sol-fallback async judging with concurrency/RPM controls, structured output, audit data, replay mode, and a hard guard against ephemeral Full Shadow artifacts. | Local commit `c4b4a5d`; 66 automated tests pass. Not pushed/deployed/run yet by design. |

## 3. Verified Full Shadow baseline

**Deployment:** `df4cc7ef-ab27-4fae-bf56-2fd262f91077`
**Status:** completed successfully
**Mode:** `full_shadow` / no email delivery

| Metric | Result |
| --- | ---: |
| Direct-company queries | 46 |
| Registered exposure queries | 159 |
| Total queries | 205 |
| RSS collection successes / failures | 205 / 0 |
| Articles collected | 2,841 |
| Freshness accepted | 1,825 |
| Freshness rejected as too old | 1,016 |
| After article dedup | 1,574 |
| Article duplicates removed | 251 |
| Route A matches / events | 0 / 0 |
| Route B candidates | 1,525 |
| Route B deterministic prefilter rejections | 49 |
| Sol judge calls | 1,525 |
| Route B accepted | 15 |
| Route B rejected total | 1,559 |
| Ranked / final report items | 11 |
| Summary calls | 11 |
| Email sent | false |
| Same-run duplicate final items | 0 |
| Production delivery checkpoint changed | false |
| Total runtime | 4,586 seconds (~76 minutes) |

### Sol rejection breakdown

| Reason | Count |
| --- | ---: |
| `wrong_context` | 871 |
| `weak_causal_link` | 332 |
| `broad_industry_only` | 157 |
| `wrong_jurisdiction` | 139 |
| `non_material` | 11 |
| `unregistered_query` prefilter | 49 |

### Important limitation of this baseline

The run created:

```text
.state/artifacts/full_shadow_20260812T054252Z.json
.state/artifacts/full_shadow_20260812T054252Z.html
```

Those were **inside the Railway container**, not a mounted Volume. The completed batch instance stopped and Railway Console showed `No running instances`; the files therefore disappeared. We retain aggregate Railway logs only.

Consequences:

- We cannot inspect the actual 11 selected articles, titles, links, summaries, or `why_it_matters` from that run.
- We cannot inspect the 1,525 individual Sol decisions or rejection samples.
- We cannot use the original artifact for a strict Luna-vs-Sol replay benchmark.

This must not happen again. A Full Shadow now requires an explicit persistent `ARTIFACT_DIR`; otherwise the application refuses to start the Full Shadow stage.

## 4. Current duplicate handling

### Article-level duplicate handling

Articles are deduplicated when they share either:

- a canonical URL; or
- a normalized title.

The duplicate reason is retained in the in-memory audit grouping rather than silently discarded.

### Route A: direct-company news

Different publishers covering the same event for the same company are clustered into a single Route A event. One representative article is selected from the cluster and is used for ranking/email. Other coverage remains only as in-memory cluster membership for that run; it is **not yet durable storage** and the representative is currently the first collected matching article.

There is no publisher-quality, article-depth, official-source, or recency scoring for selecting the representative article yet.

### Route B: external-impact news

Route B is still mainly article-level after the initial article dedup. Different publishers with different URLs/titles about the same external event can become separate candidates. Ranking caps output to at most 12 total items and two per company, but this is not a true event-level deduplication solution.

### Delivery idempotency

For a future real delivery, each final item has a stable article/event fingerprint. It is recorded only after a successful delivery, then skipped on later runs. Same-run fingerprint repeats are also skipped. This state is only durable when `STATE_DIR` points to a mounted Railway Volume.

## 5. Luna-primary / Sol-fallback development status

Local commit: `c4b4a5d feat: add luna primary sol fallback judge`
Test suite: **66 passed**
Push/deployment/new Full Shadow: **not performed**

### Implemented

- `gpt-5.6-luna` / medium as the primary Route B structured judge.
- `gpt-5.6-sol` / medium only for Luna escalation or terminal Luna technical failure.
- Luna structured outputs: `ACCEPT`, `REJECT`, `ESCALATE_TO_SOL`.
- Separate bounded concurrency and request-start RPM limits:
  - Luna: 32 concurrent, 400 RPM default.
  - Sol: 8 concurrent, 120 RPM default.
- Retry budget, timeout metrics, 429/timeout/schema failure tracking, and explicit unresolved state for terminal Sol failure.
- Stable candidate IDs so async completion order cannot attach a decision to the wrong article.
- Per-candidate audit fields for model, reasoning effort, decision, rejection reason, concise audit reason, escalation reason, retry count, latency, and final decision source.
- `cascade_eval` replay mode, designed to compare stored Sol-only artifacts to Luna without recollecting RSS or calling Sol again.
- Full Shadow refuses to use ephemeral storage when `ARTIFACT_DIR` is not configured.

### Current constraint

The intended Sol-only replay artifact was lost before the persistence guard existed. Re-running all 1,525 candidates through Sol would take roughly 76–90 minutes and cost about $10-class development spend, so this is **not approved as the next step**.

The preferred next experiment is one Luna-primary / Sol-fallback Full Shadow after persistent storage is configured. It will be substantially cheaper than Sol-only because Sol should receive only genuinely uncertain cases. Since the original Sol baseline is gone, quality review will initially rely on detailed saved audit samples rather than a strict 15/15 historical preservation comparison.

## 6. What remains for Day 4

### 6.1 Run and evaluate Luna cascade — only after infrastructure is ready

1. Push the already-tested local commit deliberately.
2. Attach a Railway Volume at `/data`.
3. Set `STATE_DIR=/data` and `ARTIFACT_DIR=/data/artifacts`.
4. Keep `RUN_MODE=shadow` until intentionally changing to the non-delivery Full Shadow invocation.
5. Run exactly one Luna-primary / Sol-fallback Full Shadow with email disabled.
6. Retrieve and inspect the persisted JSON/HTML before changing any business rules.
7. Measure runtime, Luna/Sol call mix, escalation rate, 429/retry/timeout data, token usage, and estimated/measured cost.

### 6.2 Improve duplicate/event selection policy

Define and implement a clear policy before live email:

- Route A representative article selection should prefer, in order, an official source or primary source, clearer factual detail, recency, and reputable publisher coverage.
- Persist Route A cluster members/audit instead of keeping them only in process memory.
- Add Route B event-level deduplication so the same external event covered by several publishers does not consume multiple candidate/judge calls or multiple ranking slots.
- Preserve genuinely conflicting numerical facts as separate events; do not collapse them merely because article titles are similar.
- Test edge cases: same event/different publishers, follow-up reporting, corrections, translated reprints, and distinct events on the same company/topic.

### 6.3 Upgrade the executive insight layer

The current summary is a factual Korean article summary plus Route B `why_it_matters`. This is not yet sufficient for an executive/VC audience.

The next summary contract should produce a concise **investment-relevant one-liner**, grounded only in article evidence and the approved causal path. It should explain why an executive should care, for example:

- demand signal, customer traction, or adoption scale;
- revenue, margin, unit economics, runway, or financing implications;
- regulatory, market-access, platform, or supply-chain impact;
- competitive position, strategic optionality, or portfolio exposure;
- a material change in risk, timing, or next monitoring question.

It must **not** produce generic restatements such as “Company X listed product Y” or unsupported VC-style speculation. The output should be evidence-based, company-specific, and concise enough to scan in an email.

Suggested review question per item:

> “If a Company K partner reads only this one line, do they understand the decision-relevant implication and the evidence behind it?”

## 7. Safety and operating rules preserved

- `config/keyword_map_FINAL.yaml` remains frozen and was not modified.
- The 46-company monitoring universe remains unchanged.
- Route A business semantics remain unchanged.
- No Company K production recipient has received a message.
- Full Shadow sends no email and does not advance the production delivery checkpoint.
- `RUN_MODE=live` remains blocked.
- Real API keys are not committed. Keys previously pasted into chat should be rotated.
- Do not replace the entire Railway Raw Variables block.
- Do not update the Obsidian Vault without an explicit user command.

## 8. Files and commits relevant to Day 3

| Commit | Purpose |
| --- | --- |
| `60e1ab9` | separated smoke and Full Shadow execution |
| `9e5ffb1` | fixed runtime freshness-window behavior |
| `a2208ae` | added Full Shadow review artifact generation (later found to be ephemeral without a Volume) |
| `c4b4a5d` | added Luna-primary/Sol-fallback judge, replay mode, audit metrics, and persistent-artifact guard; local only, not pushed |

## 9. Day 3 conclusion

The core bot is no longer a scaffold: it has collected real news, routed it through actual Company K rules, made real LLM causal/materiality decisions, rendered email output, and completed a no-email Full Shadow safely.

The day’s most important operational lesson is equally clear: **an evaluation run is not useful unless its detailed outputs are persisted and retrievable.** Day 4 starts with durable artifacts, then Luna performance/cost evaluation, then event-level deduplication and truly executive-grade investment insight lines.

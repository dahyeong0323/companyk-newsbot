# Company K Newsbot

Greenfield implementation of the Company K portfolio news bot. The frozen rule
map in `config/keyword_map_FINAL.yaml` is the runtime source of truth; company
keywords must not be duplicated in Python.

The active build specification is
`docs/COMPANYK_NEWSBOT_CODEX_BUILD_PROMPT_RAILWAY_FINAL.md`. Railway Cron will
be the eventual production scheduler; GitHub remains source control only.

## Current production-candidate scope

The v1 production path is Route A-only: registry, Google News RSS coverage
gate, freshness, deduplication, enrichment, deterministic identity matching,
event clustering, Luna DirectEvent assessment, grounding, and email delivery.
Route B remains disabled in production.

## Validate the configuration

```powershell
python -m pytest
python -m companyk_newsbot.cli validate-config
```

The second command validates `config/keyword_map_FINAL.yaml` by default. Use
`--config PATH` to validate a candidate copy without changing the frozen map.

## Google News RSS collector

`GoogleNewsRSSCollector` accepts explicit queries and produces canonical
`Article` records. Query generation remains out of scope until the Route A and
Route B steps, so this collector does not yet read company or exposure rules.

## Route A direct-company detection

`RouteADetector` accepts the validated map and returns direct company matches
for a normalized `Article`. It reads company names, aliases, standalone guards,
required context, and negative/English ambiguity context from the FINAL YAML.
It makes no LLM calls and does not yet classify events, deduplicate, rank, or
send alerts.

## Deduplication

`ArticleDeduplicator` collapses exact canonical-URL and normalized-title
duplicates while retaining an audit group and reason. `RouteAEventClusterer`
then groups similar cross-publication Route A coverage only within the same
company. Conflicting numeric facts (for example, different funding amounts) are
kept as separate events for later review.

## Route A-only default

The normal runtime loads `config/portfolio_registry.yaml`, builds direct queries
for every registered current/former company name, and never loads Route B
configuration. After freshness and article deduplication, query-scoped articles
whose RSS snippet does not expose the candidate identity receive bounded free
publisher-page enrichment. JSON-LD, OpenGraph, `<article>`, and `<main>` evidence
is preferred; failures remain a deterministic no-match. Dedup preserves all
originating query/company provenance and every publisher URL is fetched at most
once per run. Deterministic Route A matching and proto-event clustering then run
before one low-reasoning Luna assessment
per event. Every unique `DELIVER` event is ordered by materiality, recency, and
stable tie-breaker; there is no global or per-company delivery cap. Every
email-bound event receives one Luna grounding call. The assessment text is
reused directly; there is no article-level classifier or second summary call.

## RSS resilience and collection coverage

Google News RSS requests use a stable explicit `User-Agent`, `Accept`, and
`Accept-Language` profile. Transient `429`, `502`, `503`, `504`, connection
failures, and read timeouts receive at most two retries with bounded exponential
backoff and jitter. A usable `Retry-After` on `429` or `503` is honored up to
five seconds. Six requests remain the healthy concurrency ceiling; a dominant
transient-failure window triggers a cooldown and at most two serial probes. If
those real queued queries also fail, the remaining queue is recorded as
`skipped_systemic_failure` instead of being hammered or mislabeled as a timeout.

`RSS_MIN_SUCCESS_RATIO` defaults to `0.90`. Every configured direct query stays
in the denominator. Below the threshold the run is `inconclusive`, normal
briefing rendering and delivery stop, production delivery checkpoints and sent
fingerprints remain unchanged, and a Full Shadow writes a clearly labeled
collection-failure artifact. A zero-news briefing is valid only after sufficient
collection coverage and normal freshness/routing complete with zero final Route
A events.

Set `ROUTE_B_ENABLED=true` only to opt into the preserved experimental Route B
pipeline. The default also keeps `ROUTE_A_EVENT_RESOLVER_ENABLED=false`, so there
are no pre-assessment model calls. Sol is rejected by the Route A judge and
grounder.

## Dormant Route B exposure candidates

`ExposureRegistry` turns only registered `external_exposures[].subject.query_terms`
into de-duplicated collector queries and retains every company-specific exposure
attachment. `RouteBCandidateGenerator` accepts only articles whose collector
query is registered, and enforces `valid_from` before creating candidates.
It does not yet decide causal fit or materiality; that is the Step 6 judge.

## Cost-first Route B classifier

When Route B is explicitly enabled, `NEWSBOT_COST_FIRST_PIPELINE=true` selects
the preserved GPT-5.4 nano classifier, which resolves
clear Route B ACCEPT/REJECT cases and returns `ESCALATE_TO_LUNA` for genuine
ambiguity. Nano operational failures also escalate to GPT-5.6 Luna. A terminal
Luna operational failure conservatively retains the candidate with
`accepted_due_to_classifier_failure=true`; it is never treated as a semantic
reject. The cost-first path rejects any Sol model configuration.

Set `NEWSBOT_COST_FIRST_PIPELINE=false` to restore the frozen `c8783a8`
Luna-primary / Sol-fallback classifier. This rollback code is isolated in
`route_b_legacy.py` and is unreachable from the normal cost-first path.

## Legacy Route B ranking and summaries

`NewsRanker` applies the configured daily and per-company caps, ordered as high
direct news → high external impact → other direct news → medium/low external
impact. `NewsSummarizer` runs only after an item is qualified and ranked. It
uses GPT-5.6 Luna at low reasoning effort to produce a short grounded factual
summary; Route B items include a direct `why_it_matters` only when needed.
Luna grounding remains mandatory and deterministic safe fallback remains in
place.

## HTML email rendering

`HtmlEmailRenderer` renders a standalone Korean HTML email. The default Route
A-only mode emits one direct-news feed and omits the empty Route B section.
Legacy opt-in mode retains separate direct/external sections. It HTML-escapes
article content and never renders internal exposure IDs.

## Local / shadow pipeline

`NewsPipeline` composes normalized articles through article deduplication, Route
A event clustering, Route B candidate generation and judgement, ranking,
summary, and HTML rendering. It does not send mail. A fully offline fixture
integration test proves this flow; the runtime command comes next.

## Railway deployment preparation

Railway runs one short-lived batch process: `python -m companyk_newsbot.main`.
For v1 production, set `RUN_MODE=live`, `PRODUCTION_EMAIL_ENABLED=true`, keep
`ROUTE_B_ENABLED=false`, and set
`PORTFOLIO_REGISTRY_PATH=config/portfolio_registry.yaml`,
`DIRECT_EVENT_MODEL=gpt-5.6-luna`, `DIRECT_GROUNDING_MODEL=gpt-5.6-luna`, and
`STATE_DIR=/data`. Store `OPENAI_API_KEY`
only as a Railway secret Variable. Attach a Railway Volume at `/data`; the JSON
state there stores recent run ledger entries and sent fingerprints for the
future idempotency layer. No cron expression is committed: configure it in UTC
only after a KST delivery window and shadow-runtime lead time are measured.

The current entry point validates config and records a pre-delivery shadow run;
it cannot send email, and `live` mode is deliberately blocked.

Railway cron is evaluated in UTC. `railway.json` configures `0 23 * * *`, which
is 08:00 Asia/Seoul every day. The application uses Asia/Seoul for the email date
and blocks non-scheduled `live` starts outside a short scheduler tolerance window,
preventing deployment-time delivery.

## Offline replay limitation

Classifier replay requires the exact stored `description`, `text`, and
`origin_metadata` used by the classifier. The replay loader fails explicitly
when a historical artifact lacks those fields; it never recollects live news.
New Full Shadow artifacts persist these fields so future migrations can replay
the exact candidate corpus.

## Railway skeleton

`railway.json` declares a one-shot batch command with no restart policy. It has
no cron schedule yet: the UTC cron expression and KST delivery window belong to
Step 10, after shadow-runtime measurements.

## Schema assumptions encoded today

- `schema_version`, `name`, `company_rules`, and `external_impact_logic` are required.
- `company_rules` is a mapping keyed by company name; each company has an
  `aliases` field (which may deliberately be empty).
- Each company must declare either one or more `external_exposures` or an explicit
  `no_justified_external_exposure.status: true` closure.
- Exposure IDs are globally unique; exposures require a subject, query terms,
  ISO `valid_from`, evidence URL/source type, and valid event-family references.
- Event families must match the configured external-impact matching rules.

These checks preserve the distinction between a confirmed zero-exposure closure
and missing exposure data.

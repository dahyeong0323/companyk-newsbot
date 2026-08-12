# Company K Newsbot

Greenfield implementation of the Company K portfolio news bot. The frozen rule
map in `config/keyword_map_FINAL.yaml` is the runtime source of truth; company
keywords must not be duplicated in Python.

The active build specification is
`docs/COMPANYK_NEWSBOT_CODEX_BUILD_PROMPT_RAILWAY_FINAL.md`. Railway Cron will
be the eventual production scheduler; GitHub remains source control only.

## Current scope

Step 0–2 is complete: repository scaffold, typed loading and fail-fast
validation of the FINAL rule map, and a Google News RSS collector that returns
normalized articles. Route A/B, deduplication, LLM calls, email, and automation
are deliberately not implemented yet.

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

## Route B exposure candidates

`ExposureRegistry` turns only registered `external_exposures[].subject.query_terms`
into de-duplicated collector queries and retains every company-specific exposure
attachment. `RouteBCandidateGenerator` accepts only articles whose collector
query is registered, and enforces `valid_from` before creating candidates.
It does not yet decide causal fit or materiality; that is the Step 6 judge.

## Route B causal/materiality judge

`RouteBCausalMaterialityJudge` sends only pre-filtered Route B candidates to
the OpenAI Responses API and requests a typed `JudgeOutput`. It rejects model
outputs whose company, exposure ID, event family, or rejected-state fields do
not satisfy the candidate contract. Configure `OPENAI_API_KEY` and
`OPENAI_MODEL` only in local `.env` or Railway Variables; no live API call is
made by the tests. The implementation follows OpenAI's structured-output
pattern using Pydantic parsing: [official OpenAI documentation](https://developers.openai.com/api/docs/guides/structured-outputs).

## Ranking and summaries

`NewsRanker` applies the configured daily and per-company caps, ordered as high
direct news → high external impact → other direct news → medium/low external
impact. `NewsSummarizer` runs only after an item is qualified and ranked. It
produces an external-facing summary; Route B items require a separate
`why_it_matters`, while internal exposure IDs and prompt metadata are withheld.

## HTML email rendering

`HtmlEmailRenderer` renders a standalone Korean HTML email with separate direct
and external-impact sections. It HTML-escapes article content and never renders
internal exposure IDs. Delivery credentials and actual sending remain out of
scope until the end-to-end/deployment steps.

## Local / shadow pipeline

`NewsPipeline` composes normalized articles through article deduplication, Route
A event clustering, Route B candidate generation and judgement, ranking,
summary, and HTML rendering. It does not send mail. A fully offline fixture
integration test proves this flow; the runtime command comes next.

## Railway deployment preparation

Railway runs one short-lived batch process: `python -m companyk_newsbot.main`.
Set `RUN_MODE=shadow`, `STATE_DIR=/data`, `OPENAI_MODEL=gpt-5.6-sol`, and
`OPENAI_REASONING_EFFORT=medium` as Railway Variables. Store `OPENAI_API_KEY`
only as a Railway secret Variable. Attach a Railway Volume at `/data`; the JSON
state there stores recent run ledger entries and sent fingerprints for the
future idempotency layer. No cron expression is committed: configure it in UTC
only after a KST delivery window and shadow-runtime lead time are measured.

The current entry point validates config and records a pre-delivery shadow run;
it cannot send email, and `live` mode is deliberately blocked.

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

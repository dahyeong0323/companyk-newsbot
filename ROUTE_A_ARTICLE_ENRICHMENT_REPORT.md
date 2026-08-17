# Route A Article Enrichment Report

## Final status

`READY_FOR_ENRICHED_ROUTE_A_SHADOW`

The collection/enrichment gap is fixed without loosening deterministic Route A matching and without adding any OpenAI, paid scraping, embedding, or Route B work. No email, paid Full Shadow, deployment, cron change, or production checkpoint mutation occurred in this task.

## Starting worktree

- Branch: `main`
- HEAD: `c8783a81d0d2fa199a0d317d0b3c32a1c1dd1546`
- Worktree was already dirty with the preserved Cost-first and Route A-only migrations.
- Existing modified/untracked files and forensic artifacts were retained. No checkout, reset, restore, cleanup, or deletion was used.
- Starting offline baseline: `197 passed in 5.37s`

## Confirmed root cause

The specification's hypothesis matched the current code and real artifact:

- `GoogleNewsRSSCollector` populated title, short RSS description, Google wrapper URL, and `origin_metadata.query` but not meaningful `Article.text`.
- `RouteADetector` validated only `title + description + text`.
- The latest Shadow artifact contained 179 collected articles; all 179 retained query provenance and 0 had populated text.
- Freshness accepted 97 articles, but their RSS-visible fields contained no registered identity evidence, producing 0 Route A matches.
- `ArticleDeduplicator` previously retained only the representative article's metadata, so a duplicate returned by another company query could lose that query/company provenance.

The fix therefore enriches evidence before Route A identity validation. Query provenance remains only candidate scope, never acceptance evidence.

## Architecture implemented

```text
Google News RSS
→ freshness
→ exact article dedup + provenance union
→ query → candidate company IDs
→ skip fetch when RSS already proves scoped identity
→ otherwise bounded free publisher enrichment
→ scoped deterministic Route A validation
→ unchanged event assessment/ranking/grounding path
```

### URL resolution strategy

- Preserve the original Google News URL in `Article.url` and `origin_metadata.google_news_url`.
- Convert Google's slow `/rss/articles/{id}` wrapper to its responsive `/articles/{id}` route for resolution only.
- Prefer a safe external canonical/OpenGraph/anchor URL when directly exposed.
- Otherwise read Google's public `data-n-a-id`, timestamp, and signature attributes and use its public `Fbv4je` batchexecute decoder to obtain the publisher article URL.
- Accept only `http`/`https`, reject credentialed URLs, localhost, `.local`, and literal private/loopback/link-local/reserved/multicast addresses.
- Follow redirects manually with a maximum of 4 and validate each hop.
- Preserve the original URL while storing the normalized resolved publisher URL for audit; canonical identity changes only after article dedup.

### Article extraction strategy

Deterministic extraction priority:

1. JSON-LD `Article` / `NewsArticle` headline, description, and `articleBody`
2. `<article>`
3. `<main>`
4. page title plus OpenGraph/meta/Twitter title and description

The extractor decodes HTML entities, removes scripts/styles/noscript/templates/SVG/forms, excludes navigation/footer/aside elements, and removes nested related/recommended/sidebar/promotion/advertising containers. Arbitrary full-body text is not accepted as identity evidence. Extracted text is capped at 20,000 characters and raw HTML is never persisted.

### Query/company provenance strategy

- Each RSS article starts with `query`, `origin_queries`, and `google_news_url`.
- Dedup unions all `origin_queries` and `candidate_company_ids` while preserving the first article's public semantics.
- Normalized query mapping resolves those queries to registry company IDs.
- Route A checks only those candidate companies, including every company retained after multi-query dedup.
- A query hit alone never creates a match; the candidate's registered terms and existing ambiguity guards must pass against RSS or enriched article evidence.

### Concurrency, caching, and failure isolation

- Global enrichment concurrency: 8
- Publisher per-host concurrency: 2
- Google resolution broker concurrency: bounded by the global 8
- Per-request timeout: 8 seconds
- Redirect limit: 4
- Same-run exact URL cache: one fetch task per stable URL
- Host failure circuit breaker: 3 failures by default
- Maximum downloaded HTML inspected: 2 MB
- Timeout, blocked, HTTP, parse, and insufficient-content results are isolated per article and remain deterministic NO MATCH.

## Telemetry added

- enrichment candidates / skipped-visible / attempted / success
- timeout / HTTP error / blocked / parse error / insufficient content
- resolved publisher URLs / same-run cache hits
- Route A matches from RSS / enriched content
- enrichment runtime
- existing Route B, article-level AI, Sol, assessment, grounding, token, cost, and total runtime telemetry remains intact

Full Shadow artifacts retain compact per-article enrichment audit metadata without raw HTML or full-page logs.

## Files changed in this task

New:

- `src/companyk_newsbot/enrichment.py`
- `tests/test_enrichment.py`
- `ROUTE_A_ARTICLE_ENRICHMENT_REPORT.md`

Updated:

- `src/companyk_newsbot/collectors/google_news_rss.py`
- `src/companyk_newsbot/dedup/article.py`
- `src/companyk_newsbot/rules/route_a.py`
- `src/companyk_newsbot/route_a_only.py`
- `src/companyk_newsbot/e2e.py`
- `tests/test_google_news_rss.py`
- `tests/test_dedup.py`
- `tests/test_e2e_execution.py`
- `.env.example`
- `pyproject.toml`
- `README.md`

`config/keyword_map_FINAL.yaml`, Route B business logic, portfolio registry contents, Vault, email delivery code, and production checkpoint were not changed.

## Tests

- Starting baseline: 197 passed
- Final suite: `218 passed in 5.96s`
- Final failures: 0
- Syntax validation: `AST_OK 39 files`
- `git diff --check`: passed
- `keyword_map_FINAL.yaml`: no diff

New offline coverage includes:

- OpenGraph and standard meta title/description
- JSON-LD Article and NewsArticle, including `articleBody`
- `<article>` and `<main>` extraction
- Korean/English text, HTML entities, and script/style/noscript stripping
- related-news/footer-only identity rejection
- RSS-visible identity fetch skipping
- identity found only after JSON-LD/article enrichment
- timeout, 403, non-HTML/parse failure fail-closed behavior
- wrong-entity ASCII substring rejection
- short-English ambiguity guard on enriched text
- multi-query/company provenance union through dedup
- exact same-run URL cache fetch-once behavior
- scoped company matching rather than all-portfolio acceptance
- Google responsive-route conversion, redirect safety, private-network blocking, decoder-based publisher resolution, URL preservation, and canonicalization
- default Route B/Sol/article-level AI zero-call invariants after enrichment integration

## Small live no-AI diagnostic

No RSS recollection, OpenAI call, email, or state mutation was performed.

Preliminary diagnosis:

- The raw `/rss/articles/...` route returned 503 after roughly 6 seconds; a 6-article bounded probe timed out 6/6 in 15.594 seconds.
- The responsive `/articles/...` route returned its HTML in roughly 0.5 seconds but exposed decoder attributes rather than a direct publisher anchor. This led to the public decoder implementation above.

Final post-fix diagnostic using 3 articles from the preserved Shadow artifact:

- Queries represented: 2
- Articles: 3
- Enrichment candidates / attempts: 3 / 3
- Publisher URL resolutions: 3
- Enrichment successes: 3
- Timeout / blocked / HTTP / parse / insufficient: 0 / 0 / 0 / 0 / 0
- Resolved publishers: `gamemeca.com`, `jalopnik.com`, `ctvnews.ca`
- New identity confirmations: 0
- Runtime: 2.738 seconds
- OpenAI calls: 0
- Email calls: 0

The three sampled articles were genuine Google query false positives, so the correct result after successful publisher extraction was still NO MATCH. Offline fixtures separately prove that registered identity in JSON-LD or article content creates a scoped Route A match. Precision was not loosened to force a nonzero count.

## Production side effects

- OpenAI calls during this task: 0
- Route B calls: 0
- Sol calls: 0
- Email sends: 0
- Production delivery checkpoint before/after: `null` / `null`
- Shadow checkpoint remained the prior run value: `2026-08-14T02:48:41.833731+00:00`
- Vault writes: 0

## Git diff/status snapshot

Tracked worktree snapshot before adding this untracked report: `24 files changed, 1595 insertions(+), 263 deletions(-)`. This includes the user's pre-existing uncommitted Cost-first and Route A-only changes; untracked migration/enrichment files are not included in that stat.

The worktree remains intentionally dirty and uncommitted. No push, deploy, email, or paid Full Shadow was performed.

# RSS Resilience and Collection Coverage Gate Report

## Final status

`READY_FOR_RSS_RESILIENT_SHADOW`

The Google News RSS path now retries bounded transient failures, stops systemic
outages early, preserves every configured query in coverage accounting, and
cannot turn a `0/164` collection outage into a normal zero-news briefing.

## Starting worktree and tests

- Branch: `main`
- HEAD: `c8783a81d0d2fa199a0d317d0b3c32a1c1dd1546`
- The worktree was already dirty with the preserved Cost-first, Route A-only,
  portfolio-registry, and article-enrichment work. Nothing was reset, restored,
  deleted, or replaced.
- Starting offline baseline: `218 passed in 5.19s`
- The first sandboxed test attempt produced filesystem-permission setup errors;
  the same suite passed outside that restricted temp-directory boundary.

## Latest outage breakdown

The failed Shadow attempted 164 configured direct queries and recorded 0 query
successes. Its console output showed two completed upstream failure classes:
HTTP 503 and request timeout. Once the fixed 75-second whole-run deadline
expired, queued/uncompleted queries were converted to generic timeout results.
Therefore, not all 164 records represented requests that independently reached
Google.

| Class | What can be established from the preserved run |
|---|---|
| 429 | none observed |
| 503 | observed across distinct early queries; exact count was not persisted |
| other 5xx | none observed |
| connection timeout | not separately recoverable from the old generic timeout telemetry |
| read/request timeout | observed; exact count was not persisted |
| collection-deadline cancellation | observed for queued/uncompleted queries; exact count was not persisted |
| parse error | none observed |
| other | none observed |

The old artifact stored only aggregate success/failure counts, not typed
per-query failures, so an exact historical 503-versus-timeout count cannot be
reconstructed safely. The new artifact stores every typed result and attempt
count, closing that observability gap.

The previous collector retried only connect errors/timeouts once. It did not
retry 503 or read timeout, so retry storms did not cause this incident. However,
six workers immediately consumed more queued queries after each 503, keeping
pressure on an unhealthy upstream, while the global deadline mislabeled work
that had never started.

## Files changed for this task

- `src/companyk_newsbot/collectors/google_news_rss.py`
- `src/companyk_newsbot/collection_coverage.py` (new)
- `src/companyk_newsbot/e2e.py`
- `src/companyk_newsbot/full_shadow_artifacts.py`
- `src/companyk_newsbot/main.py`
- `.env.example`
- `README.md`
- `tests/test_google_news_rss.py`
- `tests/test_collection_coverage.py` (new)
- `tests/test_e2e_execution.py`
- `tests/test_state_and_main.py`

Route A identity rules, enrichment, deduplication, event clustering, ranking,
grounding, portfolio registry, Route B logic, and Sol reachability were not
changed.

## Retry and HTTP policy

- Transient only: 429, 502, 503, 504, connection error/timeout, and read timeout.
- Initial request plus at most two retries.
- Non-transient 4xx and parse errors are not retried.
- Exponential waits start at 0.5 seconds, cap at 4 seconds, and apply bounded
  ±20% jitter.
- A usable `Retry-After` on 429 or 503 is honored and capped at 5 seconds.
- Retry attempts and Retry-After usage are included in telemetry.
- Stable request headers were added: `User-Agent`, RSS/XML `Accept`, and
  `Accept-Language`. No header rotation or anti-bot evasion is used.

## Circuit breaker and pressure reduction

Healthy concurrency remains capped at six. Results are evaluated in bounded
waves. When at least six recent independent query results exist and 80% or more
are 429/503/timeout/connection failures, the breaker enters a one-second
cooldown and lowers pressure to serial execution. It uses at most the next two
real queued queries as probes; it creates no extra probe requests.

If neither probe succeeds, all remaining queries are recorded as
`skipped_systemic_failure` with zero attempts and the run stops early. If a
probe succeeds, normal bounded collection resumes. One or two isolated failures
cannot open the breaker.

## Deadlines and result semantics

- Per-query deadline: 12 → 30 seconds, enough for two bounded retries.
- Whole collection deadline: 75 → 120 seconds. The previously healthy
  164-query Shadow completed collection in 9.163 seconds; 120 seconds leaves
  bounded recovery room without becoming unbounded.
- The systemic breaker is designed to stop a broad outage much earlier than the
  whole-run deadline.
- Deadline cancellation is distinct from per-request timeout, and breaker skips
  are distinct from both.
- Statuses now include `success`, `rate_limited`, `service_unavailable`,
  `timeout`, `connection_error`, `http_error`, `parse_error`,
  `collection_deadline`, and `skipped_systemic_failure`.
- Every configured query always remains in the result denominator.

## Coverage gate and empty-day semantics

`RSS_MIN_SUCCESS_RATIO` is documented and defaults to `0.90`.

- success ratio >= 0.90: `SUFFICIENT`
- success ratio < 0.90: `INCONCLUSIVE`
- zero configured direct queries: configuration error

A valid zero-news day now requires sufficient collection coverage followed by
normal freshness, Route A routing, event assessment, and zero final meaningful
events. Only that path may produce the ordinary zero-news Shadow briefing.

On insufficient coverage the Route A-only runtime stops before freshness,
enrichment, Luna construction, normal rendering, and delivery. A Full Shadow
writes a diagnostic artifact titled
`[SHADOW][수집 실패] 컴퍼니케이 데일리 | YYYY-MM-DD`, but sends no email.
Its artifact and run ledger use `inconclusive`, with reason
`collection_coverage_below_threshold`.

The production delivery checkpoint, Shadow-success checkpoint, and sent article
or event fingerprints remain unchanged for an inconclusive run.

## Telemetry

The runtime, ledger, and review artifact now expose:

- `rss_query_total`, `rss_query_success`, `rss_query_failure`,
  `rss_success_ratio`
- `rss_429`, `rss_503`, `rss_other_5xx`, `rss_timeout`,
  `rss_connection_error`, `rss_parse_error`,
  `rss_skipped_systemic_failure`
- `rss_retry_attempts`, `rss_retry_after_used`,
  `rss_systemic_breaker_triggered`
- `collection_coverage_status`, `collection_coverage_threshold`

No response body, credential, or other secret is logged.

## Tests

Mocked tests cover:

- 503 → success on retry one or retry two; repeated 503 bounded at three attempts
- 429 with capped Retry-After and 429 with deterministic backoff/jitter
- read timeout recovery and bounded terminal timeout
- 404 and parse failures with no retry
- concurrency and stable request headers
- systemic 503 breaker, explicit skipped accounting, isolated failures, and
  recovery through a serial probe
- global and per-query deadline semantics
- exact 0.90 coverage boundary: 148/164 sufficient, 147/164 inconclusive
- zero configured queries as configuration error
- sufficient coverage plus zero Route A events as a valid empty Shadow
- insufficient coverage blocking normal email, Luna calls, checkpoint updates,
  and fingerprint mutation
- inconclusive Full Shadow ledger behavior

Final full suite: `240 passed in 7.45s` (baseline 218; 22 tests added).

## Optional no-AI live diagnostic

A three-query RSS-only diagnostic was attempted without loading `.env`, OpenAI,
email, or state. The first command failed syntax validation before HTTP. The
corrected command entered collection but its ad-hoc harness closed the HTTPX
client in a second event loop, raising `RuntimeError: Event loop is closed`
before metrics were printed. It was not repeated, so no success/failure counts
are claimed. This harness lifecycle issue is not present in the product path,
which uses one `async with` event loop, and that path is covered by the mocked
tests.

- Configured diagnostic queries: 3
- OpenAI calls: 0
- Email calls: 0
- State/checkpoint writes: 0
- Full 164-query Shadow: not run

## Side effects and worktree

- OpenAI calls during this task: 0
- Route B calls: 0
- Sol calls: 0
- Article-level AI calls: 0
- Emails sent: 0
- Production checkpoint changes: 0
- Vault writes: 0
- No deploy, cron change, commit, push, reset, restore, or cleanup was performed.

The worktree remains intentionally dirty and uncommitted. Final `git diff` and
`git status` were inspected after implementation; their aggregate includes the
user's pre-existing uncommitted migrations as well as this task.

Final tracked aggregate: 25 files changed, 2,406 insertions, 359 deletions.
The new coverage module, coverage tests, this report, and other preserved
pre-existing untracked migration files do not appear in that tracked diff stat.

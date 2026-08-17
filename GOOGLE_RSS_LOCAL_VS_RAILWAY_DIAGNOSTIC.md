# Google News RSS Local vs Railway Diagnostic

## Status

`TRANSIENT_OR_MIXED`

The local environment broadly failed (`0/8`), so the evidence does not support
a Railway-only failure. The Railway batch container was already exited and
could not accept an SSH command; no Railway RSS request was made. A clean
same-time environment comparison is therefore not available and a stronger
classification would overclaim.

## Worktree protection

- Branch: `main`
- HEAD: `c8783a81d0d2fa199a0d317d0b3c32a1c1dd1546`
- The existing dirty worktree and all prior migration/enrichment/resilience
  changes were preserved.
- `git diff`, cached diff, and untracked files were inspected before probing.
- No reset, checkout, restore, commit, push, deploy, restart, cron change, or
  product-logic modification was performed.
- A disposable local probe file was created under `tmp/` and removed after its
  single execution.

## Fixed probe queries

The same set was reserved for both environments. It was chosen from the current
registry order before any network result was known, mixing four Korean and four
English names and short/long forms:

1. `겟차`
2. `서남`
3. `위버스브레인`
4. `힐링페이퍼`
5. `Breeze Bio`
6. `KaliVir Immunotherapeutics`
7. `MPN Marketplace Networks`
8. `PORTONE HOLDINGS`

## Shared request settings

- Endpoint: `https://news.google.com/rss/search`
- Query construction: `<exact company query> when:2d`
- Parameters: `hl=en-US`, `gl=US`, `ceid=US:en`
- `User-Agent`: `CompanyK-Newsbot/1.0 (+Google-News-RSS)`
- `Accept`: `application/rss+xml, application/xml;q=0.9, text/xml;q=0.8`
- `Accept-Language`: `en-US,en;q=0.8`
- Concurrency: 6
- Connect timeout: 3 seconds
- Read timeout: 8 seconds
- Per-query deadline: 30 seconds
- Retry policy: initial request plus at most 2 transient retries
- Parser: feedparser

These settings came directly from the current collector. The probe did not run
freshness filtering, enrichment, Route A, event clustering, OpenAI, grounding,
or email.

## Local Windows results

Runtime:

- Python 3.13.2
- httpx 0.27.0
- feedparser 6.0.12

| Query | Attempts | Attempt results | Retry-After | Final | Latency | Parsed | Items |
|---|---:|---|---|---|---:|---|---:|
| 겟차 | 3 | 503, ReadTimeout, 503 | none | service_unavailable | 23.549s | no | 0 |
| 서남 | 3 | 503, ReadTimeout, 503 | none | service_unavailable | 20.718s | no | 0 |
| 위버스브레인 | 3 | ReadTimeout, ReadTimeout, 503 | none | service_unavailable | 22.593s | no | 0 |
| 힐링페이퍼 | 3 | 503, 503, 503 | none | service_unavailable | 19.474s | no | 0 |
| Breeze Bio | 3 | ReadTimeout, ReadTimeout, ReadTimeout | none | timeout | 24.348s | no | 0 |
| KaliVir Immunotherapeutics | 3 | 503, 503, 503 | none | service_unavailable | 18.716s | no | 0 |
| MPN Marketplace Networks | 3 | 503, ReadTimeout, ReadTimeout | none | timeout | 21.255s | no | 0 |
| PORTONE HOLDINGS | 3 | ReadTimeout, 503, ReadTimeout | none | timeout | 22.899s | no | 0 |

Totals:

- Queries attempted: 8
- Queries succeeded: 0
- Final 503/service-unavailable results: 5
- Final timeout results: 3
- Final 429 results: 0
- Other final errors: 0
- Individual HTTP attempts: 24
- Individual 503 responses: 13
- Individual ReadTimeout exceptions: 11
- Retry attempts: 16
- Retry-After used: 0
- Median per-query request latency: 21.924 seconds
- Total runtime: 74.374 seconds
- RSS parse successes: 0 (no HTTP 200 response reached the parser)

## Railway results

The linked target was confirmed without mutation:

- Workspace: `dahyeong0323's Projects`
- Project: `innovative-integrity`
- Environment: `production`
- Service: `companyk-newsbot`
- Region: `ams`
- Service type/state: one-shot batch, `Completed` / container `exited`

One safe SSH availability check (`railway ssh python --version`) returned:

```text
Your service's container is not running (status: exited).
Deploy or restart your service, then try again.
```

Per the diagnostic instructions, the service was not restarted or redeployed.
Consequently:

- Railway queries attempted: 0/8
- Railway queries succeeded: not measurable
- Railway 503/429/timeout/parse counts: not measurable
- Railway runtime/library versions: not measured from the stopped container
- Production state changes: none

## Comparison

The apples-to-apples comparison could not be completed because Railway had no
running container. The local half is nevertheless decisive against the current
`RAILWAY_ENVIRONMENT_FAILURE` hypothesis: the identical local request path was
not mostly healthy; it failed all eight queries with the same 503/ReadTimeout
pattern seen in the prior Full Shadows.

This does not prove a global Google outage or shared request issue because a
simultaneous Railway response was not obtained. It also does not reveal a local
versus Railway implementation mismatch; no remote implementation was executed.

## Final classification

`TRANSIENT_OR_MIXED`

Evidence is insufficient to isolate the environment, while the available local
evidence shows that the failure is not currently exclusive to Railway.

## Recommended next action

At the next explicitly authorized time when the existing Railway batch
container is running, execute this exact eight-query RSS-only command through
`railway ssh` once, without invoking the newsbot entrypoint. Do not restart or
redeploy solely to manufacture the comparison.

## Safety

- OpenAI calls: 0
- Emails sent: 0
- Full Shadow runs: 0
- Railway RSS requests: 0
- Production checkpoint/fingerprint changes: 0
- Vault writes: 0

# V1 Production Freeze and Deploy Report

Date: 2026-08-17

## Starting point

- Branch: `main`
- Starting commit: `c8783a81d0d2fa199a0d317d0b3c32a1c1dd1546`
- Source of truth: preserved dirty local worktree containing the completed Route A-only migrations.
- Baseline test run before this freeze: `252 passed`.

## No-cap delivery

The previous default ranker limited delivery to 12 total items and two per company. V1 now uses ranking as ordering only: materiality, recency, and stable tie-breaker remain, but the Route A production default has no global or per-company limit.

Tests prove 0, 1, 7, 12, 13, and 20 qualifying events retain the same count; three distinct events from one company all survive; duplicate coverage collapses to one event; `IGNORE` is omitted; and grounding is called once for every email-bound `DELIVER` event.

## Zero news and collection safety

With sufficient collection coverage and no qualifying events, the rendered Route A body contains exactly:

> 오늘 컴퍼니케이파트너스 포트폴리오 회사의 주요 기사는 없습니다.

Below `RSS_MIN_SUCCESS_RATIO=0.90`, the result remains `INCONCLUSIVE`: no normal email, no delivery checkpoint advance, and no sent-fingerprint mutation.

## Production behavior

- Recipient: configured `NEWSBOT_RECIPIENT=jeremy.cheon@pm.me` only.
- Production profile: `RUN_MODE=live` with `PRODUCTION_EMAIL_ENABLED=true`.
- Route B: `false`; Sol production calls: `0`; article-level AI calls: `0`.
- Every delivered Route A event is Luna-assessed and Luna-grounded. Unsupported facts use the existing deterministic fallback and unsupported insights are omitted.
- Successful email delivery alone advances the production delivery checkpoint. Shadow keeps its separate checkpoint. Duplicate fingerprints are not resent.
- The application blocks a live start outside 07:55–08:15 Asia/Seoul, so deployment/configuration does not trigger a surprise production email.

## Railway

- Project/service: `innovative-integrity` / `companyk-newsbot` / `production`.
- Persistent state: ready Railway Volume mounted at `/data`.
- Config-as-code schedule: `0 23 * * *` (UTC), equivalent to 08:00 Asia/Seoul every day.
- Existing OpenAI and Resend secrets were present; values were not read or recorded. Required non-secret production settings were verified/configured without exposing values.
- Deployment ID: `33a46836-efb0-4a1f-857d-e724f9a8fe7f`.

## Tests

- Final: `258 passed in 7.62s`.
- The first sandbox run hit Windows temporary-directory permissions; the final run used an approved writable temp location and completed cleanly.

## Side effects

- Manual production email sent: **NO**.
- Vault changed: **NO**.
- Shadow artifacts, `.env`, API keys, and private source files are excluded from the freeze commit.

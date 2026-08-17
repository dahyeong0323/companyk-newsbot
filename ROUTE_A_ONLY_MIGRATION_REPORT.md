# Route A-only Migration Report

## Completion status

`READY_FOR_ROUTE_A_SHADOW`

The attached portfolio workbook imported successfully, the registry validates with complete direct-query coverage, the default runtime is Route A-only, and the full offline regression suite passes. No live Shadow, email delivery, deployment, cron mutation, production checkpoint mutation, or Vault write was performed.

## Starting git/worktree state

- Branch: `main`
- Starting HEAD: `c8783a81d0d2fa199a0d317d0b3c32a1c1dd1546`
- Starting test baseline: `181 passed`
- The worktree was already dirty with the Cost-first Nano/Luna migration. Those edits and untracked forensic artifacts were preserved; no reset, checkout, cleanup, or deletion was performed.
- Pre-existing modified files included `.env.example`, `README.md`, resolver/e2e/editorial/replay/judge code, and their Cost-first tests.
- Pre-existing untracked files included the frozen Route B legacy module, Cost-first reachability test, replay JSON files, and full-shadow forensic JSONL files.

## Excel import

- File: `C:\Users\dahye\Documents\카카오톡 받은 파일\투자잔액(20260630).xlsx`
- SHA-256: `e2386c135112c8e505fc2bcd8e98f4994ea567fc9d51e7bda1a0a26ee53282a5`
- Inspected sheets: `전체투자현황`, `Sheet2`
- Deterministically selected source: `Sheet2!A1:A155`
- Raw rows: 155
- Nonblank company rows: 155
- Final registry companies: 155
- Former-name rows: 9
- Duplicate rows: 0
- Normalized company identity conflicts: 0
- Cross-company query/match-term conflicts: 0
- Workbook import was executed locally with `openpyxl`; generated YAML was loaded again through the production registry validator.

## Registry

- Registry: `config/portfolio_registry.yaml`
- Companies: 155
- Direct search terms/queries: 164
- Companies without an attempted direct query: 0
- Former-name queries: 9
- Exact legacy metadata/alias merges: 41
- Legacy-unmatched companies: 114
- Companies with only one search term: 146
- Query normalization is NFKC/case-insensitive and duplicate requests are removed deterministically.
- The importer does not invent aliases or fuzzy-match workbook names to the legacy map.
- Potentially ambiguous short English names retained and reported for review: `Breeze Bio`, `MPN Marketplace Networks` (2). These are not silent import conflicts; registry ambiguity rules and conservative Route A matching remain active. Compound legal suffixes such as `Pte. Ltd.` and `GmbH` are removed deterministically, so `Noah's Farm` and `PORTONE HOLDINGS` are searched without partial legal-suffix noise.
- `config/keyword_map_FINAL.yaml` has no diff and remains a dormant Route B asset.

## Architecture

### Default runtime

1. Load `PortfolioRegistry` independently from legacy Route B configuration.
2. Build direct queries for every active company and selected former names.
3. Collect Google News RSS and retain query provenance.
4. Apply freshness filtering and article deduplication.
5. Apply deterministic Route A entity matching and ambiguity guards.
6. Build deterministic proto-event clusters; `ROUTE_A_EVENT_RESOLVER_ENABLED=false` by default.
7. Call one Luna `DirectEventJudge` per proto-event.
8. Remove `IGNORE` events before ranking.
9. Rank only `DELIVER` events using returned materiality, maximum 12 total and 2 per company, with no padding.
10. Ground only final selected events, at most 12 calls.
11. Render a Route A-only email with no empty Route B section.

`ROUTE_B_ENABLED=false` is the default. The keyword map, exposure registry, Route B candidate generation, Route B classifier, and Route B event resolver are not loaded or constructed on that path. `ROUTE_B_ENABLED=true` preserves the existing legacy/Cost-first opt-in architecture.

### Direct Event Judge contract

The strict structured output contains `decision` (`DELIVER`/`IGNORE`), `reason_code`, `materiality`, `event_family`, optional `fact_summary`, optional `investor_insight`, and supplied evidence IDs only. `IGNORE` requires `materiality=none` and no briefing text. `DELIVER` requires non-none materiality, a fact, and valid event evidence IDs. The judge uses Luna at low reasoning effort and rejects any Sol model configuration.

The assessment already supplies the editorial fact/optional insight. There is no second summary/editorial model call and no article-level AI classification.

### Grounding and fallback contract

One Luna grounding call is made only for each final selected event. A supported fact is delivered unchanged. An unsupported fact is replaced by the deterministic representative article title. An unsupported or absent investor insight is omitted. Unknown assessment evidence IDs fail closed locally to the same title/no-insight fallback. Unsupported model-written text therefore cannot be delivered.

Full Shadow artifacts now preserve registry metadata/hash, normalized query-to-company coverage, proto-events, structured assessments, final ranking/email, grounding verdicts, stage tokens/latencies, optional versioned configurable cost estimates, and explicit zero-call Route B/Sol telemetry.

## Tests

- Old baseline: `181 passed`
- Final suite: `197 passed in 5.28s`
- Failures: 0
- Syntax validation: `AST_OK 38 files`

Added/updated coverage proves:

- current registry count/hash and all-company query coverage;
- former-name parsing, including comma-containing English legal names;
- registry loading without legacy Route B configuration;
- article dedup before event assessment;
- one assessment per deterministic proto-event, including multi-publication events;
- strict DELIVER/IGNORE fixtures across 16 representative categories;
- IGNORE removal before ranking and materiality propagation;
- valid variable output sizes (0, 1, 3, 7, 12) and >12 capping;
- grounding only final selected events, never more than 12;
- unsupported fact title fallback and unsupported insight omission;
- no empty Route B email section;
- default Route B candidates/calls, article-level AI calls, and Sol calls are all zero;
- legacy Route B/editorial behavior remains available when explicitly enabled;
- shadow runs do not advance the production delivery checkpoint.

The one legacy Full Shadow editorial-failure fixture now explicitly sets `ROUTE_B_ENABLED=true`, because it intentionally tests the preserved opt-in Route B/editorial pipeline rather than the new default.

## Cost-call invariants

- Default Route B calls: 0
- Default Route A event-resolver calls: 0
- Default Sol calls: 0
- Article-level AI calls: 0
- Direct assessment calls: exactly the Route A proto-event count
- Grounding calls: exactly the final selected event count, maximum 12
- Separate assessment/grounding token and latency telemetry is recorded.
- Cost estimation is enabled only when a complete, versioned set of per-million-token environment prices is configured; vendor prices are not hardcoded into business logic.

## Files changed for this migration

New migration files:

- `config/portfolio_registry.yaml`
- `PORTFOLIO_REGISTRY_IMPORT_REPORT.md`
- `ROUTE_A_ONLY_MIGRATION_REPORT.md`
- `tools/import_portfolio_registry.py`
- `src/companyk_newsbot/portfolio_registry.py`
- `src/companyk_newsbot/route_a_only.py`
- `src/companyk_newsbot/judges/direct_event.py`
- `tests/test_portfolio_registry.py`
- `tests/test_route_a_only.py`
- `tests/fixtures/direct_event_assessments.json`

Migration integration changes:

- `.env.example`
- `pyproject.toml`
- `src/companyk_newsbot/e2e.py`
- `src/companyk_newsbot/main.py`
- `src/companyk_newsbot/rules/route_a.py`
- `src/companyk_newsbot/email/renderer.py`
- `src/companyk_newsbot/full_shadow_artifacts.py`
- `src/companyk_newsbot/judges/__init__.py`
- `src/companyk_newsbot/judges/summary.py`
- `tests/test_e2e_execution.py`
- `tests/test_email_renderer.py`
- `tests/test_final_hardening.py`

The remaining modified/untracked Cost-first files shown by Git were present at the start and were intentionally preserved.

## Git diff/status snapshot

Final tracked worktree diff snapshot: `20 files changed, 1418 insertions(+), 261 deletions(-)`. This includes the user's pre-existing uncommitted Cost-first changes; untracked migration files are not counted by `git diff --stat`.

Current status remains intentionally uncommitted and dirty. Modified tracked files include the pre-existing Cost-first files plus the integration files listed above. Untracked files include the three required migration artifacts, new Route A modules/tests/import tool, and the pre-existing forensic/legacy files. No commit, push, deploy, or cleanup was performed.

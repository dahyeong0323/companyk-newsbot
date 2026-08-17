# Space Solutions Entity Disambiguation Report

## Final status

`READY_SPACE_SOLUTIONS_DISAMBIGUATED`

The `스페이스솔루션` portfolio record now identifies the intended Daejeon
aerospace/rocket valve manufacturer and fails closed for the known PIM/KONEX
same-name company. The search term remains broad; query provenance still does
not prove identity.

## Source PDF and verified corporate facts

Source: `C:\Users\dahye\Documents\카카오톡 받은 파일\스페이스솔루션 등기부등본_260706(~).pdf`

The 30-page registry extract was rendered and visually checked. Text extraction
was used only to locate the relevant fields. Verified non-personal facts:

- Legal name: `주식회사 스페이스솔루션`
- Corporate/legal registry number: `135011-0105756`
- Registered head office: `대전광역시 유성구 문지로 229(문지동)`
- Corporate website: `https://www.spacesolutions.co.kr`
- Registered business purposes used as identity evidence:
  - `항공우주(로켓)분야 고압 제어용밸브 제조 및 판매업`
  - `기계설비 공사업`
  - `무역업`
  - `부동산 임대업`

The PDF labels `135011-0105756` as `등록번호`; it is stored as a corporate
registry number, not a business registration number. No officer name, personal
address, birth date, resident identifier, security term, or investment term was
copied into runtime configuration. `누리호` was not added because this PDF does
not establish that specific association.

## Registry changes

Only the `company-d4d8d99a77ebbd89` / `스페이스솔루션` record changed:

- Preserved search term: `스페이스솔루션`
- Canonical legal name: `주식회사 스페이스솔루션`
- Match terms preserve the bare and abbreviated Korean forms and add the full
  legal name.
- Positive target context:
  - `항공우주`
  - `로켓`
  - `고압 제어용밸브`
  - `고압 제어 밸브`
  - `대전`
  - `유성구`
  - `문지로`
  - `spacesolutions.co.kr`
- Negative same-name context, scoped only to this record:
  - `PIM`
  - `코넥스`
  - `KONEX`
- All registered Space Solutions name forms are marked ambiguous standalone
  forms and require at least one positive target-company discriminator.
- Added optional `identity_metadata` containing only the registry number,
  registered head office, website, registered business purposes, and source
  document name.

The query plan remains 164 direct queries. The corporate registry number is
metadata only and is not a search term or article requirement.

## Matching behavior

Accepted deterministic fixtures:

- `스페이스솔루션 + 항공우주`
- `주식회사 스페이스솔루션 + 로켓 + 고압 제어용밸브`
- `스페이스솔루션 + 대전/유성구`
- `스페이스솔루션 + spacesolutions.co.kr`

Rejected deterministic fixtures:

- `스페이스솔루션 + PIM`
- `스페이스솔루션 + 코넥스`
- `스페이스솔루션 + KONEX`
- bare `스페이스솔루션` with no legal-entity discriminator
- unregistered `Space Solution` and unrelated fragment collisions

The PIM/KONEX fixtures include a positive location word and still reject,
proving that explicit wrong-entity context wins rather than passing through the
positive-context gate.

## Generic matcher support

No company-name branch was added. The existing ambiguity mechanism received one
narrow generic rule: when a match term is explicitly `forbidden_standalone`,
any configured negative context rejects it even if a positive discriminator is
also present. Existing records that do not combine those controls retain their
prior behavior.

`CorporateIdentityMetadata` is optional and forbidden-extra, so the other 154
portfolio records require no data change. Identity metadata is not referenced
by the email renderer or user-facing briefing path.

## Tests

- Added 12 targeted cases covering the four positive discriminator families,
  three wrong-company contexts, bare-name failure, fragment/English collisions,
  metadata loading, no `누리호`, and unchanged 164-query coverage.
- Focused Space Solutions + existing Route A/registry suite: `30 passed in 2.55s`
- Full suite baseline: `240 passed in 8.87s`
- Final full suite: `252 passed in 10.32s`
- AST validation: 66 Python files parsed successfully
- `git diff --check`: passed (line-ending notices only)

## Files changed for this patch

- `config/portfolio_registry.yaml`
- `src/companyk_newsbot/portfolio_registry.py`
- `src/companyk_newsbot/rules/route_a.py`
- `tests/test_space_solutions_disambiguation.py`
- `SPACE_SOLUTIONS_ENTITY_DISAMBIGUATION_REPORT.md`

The aggregate Git worktree remains intentionally dirty with all previously
preserved Cost-first, Route A-only, enrichment, and RSS-resilience work. Since
the registry/model/test files were already untracked before this patch, the
repository's tracked-only `git diff` is not a complete task diff. No existing
work was reset, restored, discarded, committed, or pushed.

## Side effects

- OpenAI calls: 0
- Route B calls: 0
- Sol calls: 0
- Emails sent: 0
- Full Shadow runs: 0
- Deploy/cron changes: 0
- Production checkpoint/fingerprint changes: 0
- Vault writes: 0

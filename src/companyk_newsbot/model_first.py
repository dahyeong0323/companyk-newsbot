"""Model-first Route A preparation; Python enforces only safety invariants."""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from hashlib import sha256
import json
import os
from typing import Any, Callable

from companyk_newsbot.dedup import EventCluster, RepresentativeArticleSelector, article_id
from companyk_newsbot.dedup.anchors import EventAnchors
from companyk_newsbot.rules import RouteAMatch
from companyk_newsbot.semantic_grouping import EventCandidate, EventGroup, GroupingProvider, validate_partition
from companyk_newsbot.semantic_identity import IdentityDecision, IdentityProvider, IdentityVerdict


def _payload(match: RouteAMatch) -> dict[str, object]:
    article = match.article
    return {"article_id": article_id(article), "title": article.title, "url": article.canonical_url, "lead": (article.text or article.description or "")[:1400],
            "publisher": article.source, "publisher_domain": article.origin_metadata.get("resolved_domain"),
            "published_at": article.published_at.isoformat() if article.published_at else None,
            "origin_queries": article.origin_metadata.get("origin_queries", [])}


def prepare_events(matches: tuple[RouteAMatch, ...], registry: Any, *, identity_provider: IdentityProvider,
                   grouping_provider: GroupingProvider, progress: Callable[[str, str, str | None], None] | None = None) -> tuple[tuple[EventCluster, ...], dict[str, object]]:
    companies = {company.display_name: company for company in registry.companies}
    candidate: dict[str, list[RouteAMatch]] = defaultdict(list)
    for match in matches:
        if match.company in companies:
            candidate[match.company].append(match)
    metrics: dict[str, object] = {"identity_articles": 0, "identity_related": 0, "identity_not_related": 0,
               "identity_uncertain": 0, "identity_failures": 0, "grouping_companies": 0, "grouping_failures": 0}
    related: dict[str, list[RouteAMatch]] = defaultdict(list)
    batch_size = max(1, int(os.getenv("IDENTITY_BATCH_SIZE", "25")))

    def identity_company(name: str, values: list[RouteAMatch]) -> tuple[str, list[RouteAMatch], dict[str, int], list[dict[str, object]]]:
        company = companies[name]; selected: list[RouteAMatch] = []
        local = {key: 0 for key in metrics if isinstance(metrics[key], int)}
        audit: list[dict[str, object]] = []
        context = json.dumps({"website": company.identity_metadata.website if company.identity_metadata else None,
                              "business_purposes": company.identity_metadata.business_purposes if company.identity_metadata else []}, ensure_ascii=False)
        aliases = list(dict.fromkeys([*company.match_terms, *company.search_terms]))
        for offset in range(0, len(values), batch_size):
            batch = values[offset:offset + batch_size]; payloads = [_payload(value) for value in batch]
            if progress: progress("identity", "started", str(payloads[0]["article_id"]))
            try:
                verdicts = identity_provider.classify_many(company=company.display_name, aliases=aliases, registry_context=context, articles=payloads)
                if progress: progress("identity", "completed", str(payloads[-1]["article_id"]))
            except Exception:
                verdicts = {}; local["identity_failures"] += 1
                if progress: progress("identity", "failed", str(payloads[-1]["article_id"]))
            for value, payload in zip(batch, payloads):
                raw = verdicts.get(str(payload["article_id"]), IdentityVerdict.UNCERTAIN)
                decision = raw if isinstance(raw, IdentityDecision) else IdentityDecision(IdentityVerdict(raw), "unknown", "provider_no_audit")
                verdict = decision.verdict
                local["identity_articles"] += 1; local[f"identity_{verdict.value.casefold()}"] += 1
                audit.append({"company_id": company.company_id, "company": company.display_name, "article_id": payload["article_id"],
                    "title": payload["title"], "url": payload["url"], "verdict": verdict.value,
                    "confidence": decision.confidence, "reason_code": decision.reason_code})
                if verdict is IdentityVerdict.RELATED: selected.append(value)
        return company.company_id, selected, local, audit

    workers = max(1, int(os.getenv("MODEL_FIRST_CONCURRENCY", "6")))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(identity_company, name, values) for name, values in candidate.items()]
        for future in as_completed(futures):
            company_id, values, local, audit = future.result()
            related[company_id].extend(values)
            for key, value in local.items(): metrics[key] += value
            metrics.setdefault("identity_decisions", []).extend(audit)

    selector = RepresentativeArticleSelector(); events: list[EventCluster] = []
    by_id_and_name = {company.company_id: company.display_name for company in registry.companies}

    def partition_once(company_id: str, candidates: list[EventCandidate]) -> tuple[EventGroup, ...]:
        return validate_partition(candidates, tuple(grouping_provider.partition(company_id=company_id, candidates=candidates)))

    def bulk_fallback(company_id: str, candidates: list[EventCandidate]) -> tuple[EventGroup, ...]:
        # A 93-article payload with 1,400-character leads is too large to safely
        # partition in one structured response. Keep original ordering and bound
        # each provisional call to 24 articles before one compressed final merge.
        if len(candidates) == 1:
            item = candidates[0]
            return (EventGroup((item.article_id,), item.article_id, item.title[:160],
                "grouping unavailable after one retry", "single article fail-safe"),)
        chunk_size = 24
        provisional: list[EventGroup] = []
        for offset in range(0, len(candidates), chunk_size):
            chunk = candidates[offset:offset + chunk_size]
            try:
                provisional.extend(partition_once(company_id, chunk))
            except Exception:
                provisional.extend(EventGroup((item.article_id,), item.article_id, item.title[:160],
                    "chunk grouping unavailable", "single article chunk fail-safe") for item in chunk)
        if len(provisional) <= 1:
            return tuple(provisional)
        compressed: list[EventCandidate] = []
        provisional_by_id: dict[str, EventGroup] = {}
        for index, group in enumerate(provisional):
            compressed_id = f"provisional-{index}"
            provisional_by_id[compressed_id] = group
            original = {item.article_id: item for item in candidates}
            representative = original[group.representative_article_id]
            member_titles = "; ".join(
                original[item].title[:180] for item in group.member_article_ids[:2]
            )
            compressed.append(EventCandidate(compressed_id, group.event_label,
                f"Representative: {representative.title[:300]}\nMembers: {member_titles}",
                representative.publisher, representative.published_at))
        try:
            merged = partition_once(company_id, compressed)
        except Exception:
            return tuple(provisional)
        final: list[EventGroup] = []
        for group in merged:
            children = [provisional_by_id[item] for item in group.member_article_ids]
            selected = provisional_by_id[group.representative_article_id]
            final.append(EventGroup(tuple(item for child in children for item in child.member_article_ids),
                selected.representative_article_id, group.event_label, group.reason, group.representative_reason))
        return tuple(final)

    def group_company(company_id: str, values: list[RouteAMatch]):
        candidates = [EventCandidate(article_id(value.article), value.article.title, (value.article.text or value.article.description or "")[:1400], value.article.source, value.article.published_at) for value in values]
        if progress: progress("grouping", "started", company_id)
        for attempt in range(2):
            try:
                groups = partition_once(company_id, candidates)
                if progress: progress("grouping", "completed", company_id)
                return company_id, values, groups, False
            except Exception:
                if attempt == 0:
                    continue
        # A failed full-company partition must never erase related news. The
        # provisional/final merge path adds no event taxonomy or semantic rule.
        groups = bulk_fallback(company_id, candidates)
        if progress: progress("grouping", "failed", company_id)
        return company_id, values, groups, True
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(group_company, company_id, values) for company_id, values in related.items() if values]
        for future in as_completed(futures):
            company_id, values, groups, used_fail_safe = future.result(); metrics["grouping_companies"] += 1
            if used_fail_safe:
                metrics["grouping_failures"] += 1
            lookup = {article_id(value.article): value for value in values}
            for group in groups:
                members = [lookup[item] for item in group.member_article_ids]
                primary = lookup[group.representative_article_id]
                normal_members = [value for value in members if not selector.is_low_quality_repost(value.article)]
                if selector.is_low_quality_repost(primary.article) and normal_members:
                    primary, _, _ = selector.choose(normal_members, lambda value: value.article)
                coverage = tuple(value for value in members if value is not primary)
                scores = {article_id(value.article): selector.score(value.article) for value in members}
                canonical = sha256(f"{company_id}|{group.event_label.casefold()}".encode()).hexdigest()
                events.append(EventCluster(by_id_and_name[company_id], primary, coverage, canonical[:16], EventAnchors.from_article(primary.article), scores, (), canonical, group.event_label, group.reason, group.representative_reason))
    # Provider-internal failures are intentionally surfaced even when a provider
    # converts the affected batch to UNCERTAIN/empty for local isolation.
    identity_payload = getattr(identity_provider, "metrics_payload", lambda: {})()
    grouping_payload = getattr(grouping_provider, "metrics_payload", lambda: {})()
    metrics["identity_failures"] = max(metrics["identity_failures"], int(identity_payload.get("identity_failures", 0) or 0))
    metrics["grouping_failures"] = max(metrics["grouping_failures"], int(grouping_payload.get("grouping_failures", 0) or 0))
    attempts = int(identity_payload.get("identity_requests", 0) or 0) + int(grouping_payload.get("grouping_requests", 0) or 0)
    failures = metrics["identity_failures"] + metrics["grouping_failures"]
    minimum = int(os.getenv("MODEL_FIRST_SYSTEMIC_FAILURE_MIN_REQUESTS", "3"))
    ratio = float(os.getenv("MODEL_FIRST_SYSTEMIC_FAILURE_RATIO", "0.25"))
    metrics["model_first_systemic_failure"] = attempts >= minimum and failures / attempts >= ratio
    company_counts: dict[str, dict[str, object]] = {}
    for decision in metrics.get("identity_decisions", []):
        row = company_counts.setdefault(str(decision["company_id"]), {"company_id": decision["company_id"], "company": decision["company"], "identity_related": 0, "identity_not_related": 0, "identity_uncertain": 0, "canonical_events": 0})
        row[f"identity_{str(decision['verdict']).casefold()}"] += 1
    for event in events:
        row = next((value for value in company_counts.values() if value["company"] == event.company), None)
        if row is not None: row["canonical_events"] += 1
    metrics["company_stage_counts"] = list(company_counts.values())
    return tuple(events), metrics

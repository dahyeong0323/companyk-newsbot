"""Deterministic direct-company entity detection for Route A."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from companyk_newsbot.config import CompanyRule, KeywordMapConfig
from companyk_newsbot.portfolio_registry import PortfolioCompany, PortfolioRegistry
from companyk_newsbot.collectors.google_news_rss import normalized_query
from companyk_newsbot.models import Article


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("–", "-").replace("—", "-")


def _spans(text: str, term: str) -> list[tuple[int, int]]:
    """Match registered terms without allowing ASCII substring false positives."""
    normalized_text = _normalized(text)
    normalized_term = _normalized(term).strip()
    if not normalized_term:
        return []
    escaped = re.escape(normalized_term)
    if any(char.isascii() and char.isalnum() for char in normalized_term):
        pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
    else:
        pattern = escaped
    return [match.span() for match in re.finditer(pattern, normalized_text)]


def _contains(text: str, term: str) -> bool:
    if _spans(text, term):
        return True
    # Korean entity strings can appear with spacing variation in source feeds.
    normalized_term = _normalized(term)
    if not normalized_term or any(char.isascii() and char.isalnum() for char in normalized_term):
        return False
    return re.sub(r"\s+", "", normalized_term) in re.sub(r"\s+", "", _normalized(text))


@dataclass(frozen=True)
class RouteAMatch:
    company: str
    matched_terms: tuple[str, ...]
    article: Article


class RouteADetector:
    """Find direct portfolio-company mentions using only the frozen YAML map."""

    def __init__(self, config: KeywordMapConfig | PortfolioRegistry) -> None:
        self._company_rules = (
            config.company_rules if isinstance(config, KeywordMapConfig)
            else {company.display_name: company for company in config.companies}
        )
        self._companies_by_id = (
            {company.company_id: company for company in config.companies}
            if isinstance(config, PortfolioRegistry) else {}
        )
        self._company_ids_by_query: dict[str, tuple[str, ...]] = {}
        if isinstance(config, PortfolioRegistry):
            owners: dict[str, list[str]] = {}
            for company in config.companies:
                for term in company.search_terms:
                    owners.setdefault(normalized_query(term), []).append(company.company_id)
            self._company_ids_by_query = {key: tuple(values) for key, values in owners.items()}

    def detect(self, article: Article) -> list[RouteAMatch]:
        text = "\n".join(part for part in (article.title, article.description, article.text) if part)
        return self._detect_rules(article, text, self._company_rules.items())

    def candidate_company_ids(self, article: Article) -> tuple[str, ...]:
        """Resolve direct-query provenance to a stable, registry-scoped company set."""
        if not self._companies_by_id:
            return ()
        metadata = article.origin_metadata
        explicit = metadata.get("candidate_company_ids", [])
        values: list[str] = []
        if isinstance(explicit, list):
            values.extend(str(value) for value in explicit if str(value) in self._companies_by_id)
        queries = metadata.get("origin_queries", [])
        if not isinstance(queries, list):
            queries = []
        query = metadata.get("query")
        if isinstance(query, str):
            queries = [query, *queries]
        for value in queries:
            if isinstance(value, str):
                values.extend(self._company_ids_by_query.get(normalized_query(value), ()))
        return tuple(dict.fromkeys(values))

    def detect_scoped(self, article: Article, *, include_text: bool = True) -> list[RouteAMatch]:
        """Validate only companies mapped from direct-query provenance; provenance alone never accepts."""
        company_ids = self.candidate_company_ids(article)
        if not company_ids:
            return []
        text_parts = (article.title, article.description, article.text if include_text else None)
        text = "\n".join(part for part in text_parts if part)
        rules = ((self._companies_by_id[value].display_name, self._companies_by_id[value]) for value in company_ids)
        return self._detect_rules(article, text, rules)

    def with_candidate_provenance(self, article: Article) -> Article:
        """Attach compact query/company audit fields without treating them as content evidence."""
        queries = article.origin_metadata.get("origin_queries", [])
        if not isinstance(queries, list):
            queries = []
        query = article.origin_metadata.get("query")
        if isinstance(query, str):
            queries = [query, *queries]
        queries = list(dict.fromkeys(value for value in queries if isinstance(value, str) and value.strip()))
        metadata = dict(article.origin_metadata)
        metadata["origin_queries"] = queries
        metadata["candidate_company_ids"] = list(self.candidate_company_ids(article.model_copy(update={"origin_metadata": metadata})))
        return article.model_copy(update={"origin_metadata": metadata})

    def _detect_rules(self, article: Article, text: str, rules) -> list[RouteAMatch]:
        matches: list[RouteAMatch] = []
        for company, rule in rules:
            matched_terms = self._matched_terms(company, rule, text)
            if matched_terms:
                matches.append(RouteAMatch(company=company, matched_terms=tuple(matched_terms), article=article))
        return matches

    @staticmethod
    def _extra(rule: CompanyRule | PortfolioCompany, key: str, default: object) -> object:
        if isinstance(rule, PortfolioCompany):
            mapping = {"required_context": "required_context", "negative_terms": "negative_context",
                "forbidden_standalone": "forbidden_standalone", "required_context_for_forbidden": "required_context_for_forbidden",
                "english_negative_context": "english_negative_context", "english_required_context_for_short_form": "english_required_context_for_short_form"}
            return getattr(rule.ambiguity, mapping[key], default)
        return rule.model_extra.get(key, default) if rule.model_extra else default

    def _matched_terms(self, company: str, rule: CompanyRule | PortfolioCompany, text: str) -> list[str]:
        terms = list(dict.fromkeys(rule.match_terms if isinstance(rule, PortfolioCompany) else [company, *rule.aliases]))
        forbidden = set(self._extra(rule, "forbidden_standalone", []))
        negative_terms = self._extra(rule, "negative_terms", [])
        matched: list[str] = []
        for term in terms:
            if not _contains(text, term):
                continue
            if not self._is_outside_negative_only_match(text, term, negative_terms):
                continue
            if not self._context_allows(rule, term, text, forbidden):
                continue
            if not self._english_context_allows(rule, term, text):
                continue
            matched.append(term)
        # A registered long form (for example, ``ALPHA-X``) subsumes a shorter
        # alias in the same match. Retain the most specific evidence only.
        return [
            term
            for term in matched
            if not any(
                term != other and _normalized(term) in _normalized(other)
                for other in matched
            )
        ]

    @staticmethod
    def _is_outside_negative_only_match(text: str, term: str, negative_terms: object) -> bool:
        if not isinstance(negative_terms, list):
            return True
        term_spans = _spans(text, term)
        if not term_spans:
            return True
        negative_spans = [span for negative in negative_terms for span in _spans(text, str(negative))]
        return any(not any(start >= neg_start and end <= neg_end for neg_start, neg_end in negative_spans) for start, end in term_spans)

    def _context_allows(self, rule: CompanyRule | PortfolioCompany, term: str, text: str, forbidden: set[object]) -> bool:
        required_context = self._extra(rule, "required_context", [])
        if isinstance(required_context, list) and required_context and not any(_contains(text, str(value)) for value in required_context):
            return False
        if term not in forbidden:
            return True
        # A known ambiguous standalone name may have both target-company
        # discriminators and explicit wrong-entity context. Negative context
        # wins even when a generic positive word also appears in the article.
        negative_context = self._extra(rule, "negative_terms", [])
        if isinstance(negative_context, list) and any(_contains(text, str(value)) for value in negative_context):
            return False
        forbidden_context = self._extra(rule, "required_context_for_forbidden", None)
        if isinstance(forbidden_context, dict):
            required = forbidden_context.get(term, [])
        elif isinstance(forbidden_context, list):
            required = forbidden_context
        else:
            return False
        return bool(required) and any(_contains(text, str(value)) for value in required)

    def _english_context_allows(self, rule: CompanyRule | PortfolioCompany, term: str, text: str) -> bool:
        if not any(char.isascii() and char.isalpha() for char in term):
            return True
        negatives = self._extra(rule, "english_negative_context", [])
        if isinstance(negatives, list) and any(_contains(text, str(value)) for value in negatives):
            return False
        requirements = self._extra(rule, "english_required_context_for_short_form", {})
        if isinstance(requirements, dict) and term in requirements and isinstance(requirements[term], list):
            return any(_contains(text, str(value)) for value in requirements[term])
        return True

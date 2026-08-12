"""Deterministic direct-company entity detection for Route A."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from companyk_newsbot.config import CompanyRule, KeywordMapConfig
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

    def __init__(self, config: KeywordMapConfig) -> None:
        self._company_rules = config.company_rules

    def detect(self, article: Article) -> list[RouteAMatch]:
        text = "\n".join(part for part in (article.title, article.description, article.text) if part)
        matches: list[RouteAMatch] = []
        for company, rule in self._company_rules.items():
            matched_terms = self._matched_terms(company, rule, text)
            if matched_terms:
                matches.append(RouteAMatch(company=company, matched_terms=tuple(matched_terms), article=article))
        return matches

    @staticmethod
    def _extra(rule: CompanyRule, key: str, default: object) -> object:
        return rule.model_extra.get(key, default) if rule.model_extra else default

    def _matched_terms(self, company: str, rule: CompanyRule, text: str) -> list[str]:
        terms = list(dict.fromkeys([company, *rule.aliases]))
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

    def _context_allows(self, rule: CompanyRule, term: str, text: str, forbidden: set[object]) -> bool:
        required_context = self._extra(rule, "required_context", [])
        if isinstance(required_context, list) and required_context and not any(_contains(text, str(value)) for value in required_context):
            return False
        if term not in forbidden:
            return True
        forbidden_context = self._extra(rule, "required_context_for_forbidden", None)
        if isinstance(forbidden_context, dict):
            required = forbidden_context.get(term, [])
        elif isinstance(forbidden_context, list):
            required = forbidden_context
        else:
            return False
        return bool(required) and any(_contains(text, str(value)) for value in required)

    def _english_context_allows(self, rule: CompanyRule, term: str, text: str) -> bool:
        if not any(char.isascii() and char.isalpha() for char in term):
            return True
        negatives = self._extra(rule, "english_negative_context", [])
        if isinstance(negatives, list) and any(_contains(text, str(value)) for value in negatives):
            return False
        requirements = self._extra(rule, "english_required_context_for_short_form", {})
        if isinstance(requirements, dict) and term in requirements and isinstance(requirements[term], list):
            return any(_contains(text, str(value)) for value in requirements[term])
        return True

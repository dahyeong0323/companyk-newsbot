"""High-precision event clustering and deterministic representative selection.

Article deduplication remains a separate fast path.  This module deliberately
prefers a visible duplicate over a false event merge.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher
import hashlib
import re
import json
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from companyk_newsbot.dedup.article import normalized_title
from companyk_newsbot.models import Article
from companyk_newsbot.rules import RouteAMatch

EventDecision = Literal["SAME_EVENT", "DIFFERENT_EVENT", "AMBIGUOUS"]


class EventResolverOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["SAME_EVENT", "DIFFERENT_EVENT"]
    short_reason: str


class EventResolver(Protocol):
    def decide(self, left: Article, right: Article) -> EventDecision: ...


class LunaEventResolver:
    """Structured resolver for ambiguous pairs; every technical failure keeps events separate."""
    def __init__(self, client: Any, *, model: str = "gpt-5.6-luna", reasoning_effort: str = "medium") -> None:
        self.client, self.model, self.reasoning_effort = client, model, reasoning_effort

    def decide(self, left: Article, right: Article) -> EventDecision:
        try:
            response = self.client.responses.parse(
                model=self.model,
                reasoning={"effort": self.reasoning_effort},
                text_format=EventResolverOutput,
                input=[
                    {"role": "system", "content": "Decide whether two supplied articles describe the SAME underlying real-world event, not merely the same topic. If evidence is insufficient, choose DIFFERENT_EVENT. Never infer facts outside supplied articles."},
                    {"role": "user", "content": json.dumps({"left": {"title": left.title, "description": left.description, "published_at": left.published_at.isoformat() if left.published_at else None}, "right": {"title": right.title, "description": right.description, "published_at": right.published_at.isoformat() if right.published_at else None}}, ensure_ascii=False)},
                ],
            )
            parsed = getattr(response, "output_parsed", None)
            return parsed.decision if isinstance(parsed, EventResolverOutput) else "DIFFERENT_EVENT"
        except Exception:
            return "DIFFERENT_EVENT"


def article_id(article: Article) -> str:
    value = f"{article.canonical_url}|{article.title}|{article.published_at}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+|[가-힣]+", normalized_title(value)))


def _numbers(value: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?(?:[%a-z]|억|만|천|원|달러)?", normalized_title(value)))


def _text(article: Article) -> str:
    return " ".join(part for part in (article.title, article.description, article.text) if part)


@dataclass(frozen=True)
class EventAnchors:
    tokens: frozenset[str]
    numbers: frozenset[str]

    @classmethod
    def from_article(cls, article: Article) -> "EventAnchors":
        value = _text(article)
        return cls(frozenset(_tokens(value)), frozenset(_numbers(value)))

    def conflicts_with(self, other: "EventAnchors") -> bool:
        return bool(self.numbers and other.numbers and self.numbers != other.numbers)


@dataclass(frozen=True)
class RepresentativeScore:
    source_of_record: int = 0
    trusted_publisher: int = 0
    body_completeness: int = 0
    factual_richness: int = 0
    aggregator_penalty: int = 0
    title_only_penalty: int = 0

    @property
    def total(self) -> int:
        return sum(self.__dict__.values())

    def payload(self) -> dict[str, int]:
        return {**self.__dict__, "total": self.total}


class RepresentativeArticleSelector:
    """Small auditable scoring system; it avoids subjective outlet league tables."""
    SOURCE_OF_RECORD = (".gov", ".go.kr", "sec.gov", "dart.fss", "ir.", "newsroom", "investor")
    AGGREGATORS = ("google", "yahoo", "msn", "news.google", "feed")

    def score(self, article: Article) -> RepresentativeScore:
        url, source = article.canonical_url.casefold(), article.source.casefold()
        official = any(marker in url for marker in self.SOURCE_OF_RECORD)
        aggregate = any(marker in url or marker in source for marker in self.AGGREGATORS)
        text = (article.text or "").strip()
        description = (article.description or "").strip()
        facts = len(_numbers(_text(article)))
        named = len(_tokens(article.title))
        return RepresentativeScore(
            source_of_record=40 if official else 0,
            trusted_publisher=20 if not official and not aggregate and source else 0,
            body_completeness=20 if len(text) >= 160 else (10 if len(description) >= 60 else 0),
            factual_richness=min(10, facts * 2 + (2 if named >= 5 else 0)),
            aggregator_penalty=-30 if aggregate else 0,
            title_only_penalty=-15 if not text and not description else 0,
        )

    def choose(self, matches: Iterable[RouteAMatch]) -> tuple[RouteAMatch, tuple[RouteAMatch, ...], dict[str, RepresentativeScore]]:
        values = tuple(matches)
        scores = {article_id(match.article): self.score(match.article) for match in values}
        ordered = sorted(values, key=lambda match: (-scores[article_id(match.article)].total, -len(match.article.text or ""), match.article.published_at is None, match.article.published_at, match.article.canonical_url))
        return ordered[0], tuple(ordered[1:]), scores


@dataclass(frozen=True)
class EventCluster:
    company: str
    primary: RouteAMatch
    coverage: tuple[RouteAMatch, ...]
    event_id: str = ""
    anchors: EventAnchors | None = None
    representative_scores: dict[str, RepresentativeScore] | None = None
    dedup_decisions: tuple[tuple[str, str, EventDecision], ...] = ()

    @property
    def coverage_count(self) -> int:
        return 1 + len(self.coverage)


class RouteAEventClusterer:
    def __init__(self, *, min_token_similarity: float = 0.5, min_title_ratio: float = 0.72, event_window_hours: int = 72, selector: RepresentativeArticleSelector | None = None, resolver: EventResolver | None = None) -> None:
        self.min_token_similarity, self.min_title_ratio = min_token_similarity, min_title_ratio
        self.event_window = timedelta(hours=event_window_hours)
        self.selector = selector or RepresentativeArticleSelector()
        self.resolver = resolver

    def cluster(self, matches: Iterable[RouteAMatch]) -> list[EventCluster]:
        clusters: list[list[RouteAMatch]] = []
        for match in sorted(matches, key=lambda value: (value.company.casefold(), value.article.published_at is None, value.article.published_at, value.article.canonical_url)):
            placed = False
            for members in clusters:
                if members[0].company != match.company:
                    continue
                decisions = [self._decision(existing, match) for existing in members]
                # Joining requires compatibility with every existing member: no transitive leakage.
                resolved = [self.resolver.decide(existing.article, match.article) if decision == "AMBIGUOUS" and self.resolver else decision for existing, decision in zip(members, decisions)]
                if resolved and all(decision == "SAME_EVENT" for decision in resolved):
                    members.append(match); placed = True; break
            if not placed:
                clusters.append([match])
        result: list[EventCluster] = []
        for members in clusters:
            primary, coverage, scores = self.selector.choose(members)
            ids = sorted(article_id(member.article) for member in members)
            event_id = hashlib.sha256((members[0].company + "|" + "|".join(ids)).encode()).hexdigest()[:16]
            result.append(EventCluster(members[0].company, primary, coverage, event_id, EventAnchors.from_article(primary.article), scores))
        return result

    def _decision(self, left: RouteAMatch, right: RouteAMatch) -> EventDecision:
        if left.article.published_at and right.article.published_at and abs(left.article.published_at - right.article.published_at) > self.event_window:
            return "DIFFERENT_EVENT"
        left_title, right_title = normalized_title(left.article.title), normalized_title(right.article.title)
        if left_title == right_title:
            return "SAME_EVENT"
        left_anchors, right_anchors = EventAnchors.from_article(left.article), EventAnchors.from_article(right.article)
        if left_anchors.conflicts_with(right_anchors):
            return "DIFFERENT_EVENT"
        lt = _tokens(left_title).difference(_tokens(left.company)); rt = _tokens(right_title).difference(_tokens(right.company))
        if not lt or not rt:
            return "AMBIGUOUS"
        jaccard = len(lt & rt) / len(lt | rt)
        ratio = SequenceMatcher(None, left_title, right_title).ratio()
        # Shared distinctive numeric anchors make paraphrased funding/filing
        # headlines safe to merge even when their wording order differs.
        shared_tokens = lt & rt
        if (bool(left_anchors.numbers & right_anchors.numbers) and len(shared_tokens) >= 2) or ratio >= 0.9:
            return "SAME_EVENT"
        return "AMBIGUOUS"

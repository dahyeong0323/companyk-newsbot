"""Lightweight, deterministic anchors for high-precision event identity."""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from companyk_newsbot.dedup.article import normalized_title
from companyk_newsbot.models import Article


_ACTION_TERMS = {
    "funding": ("raise", "raises", "raised", "funding", "financing", "investment round", "투자 유치", "투자유치", "펀딩"),
    "acquisition": ("acquire", "acquires", "acquired", "acquisition", "buyout", "buys", "merger", "인수", "합병"),
    "partnership": ("partnership", "partners", "partnered", "agreement", "contract", "collaboration", "제휴", "협약", "계약", "협력"),
    "ipo_filing": ("ipo filing", "files for ipo", "filed for ipo", "listing application", "상장 예비심사", "상장예비심사", "상장 신청"),
    "ipo_pricing": ("ipo pricing", "prices ipo", "priced ipo", "공모가 확정", "공모가격"),
    "commercial_launch": ("commercial launch", "launches", "launched", "product launch", "출시", "상용화"),
    "regulatory_approval": ("regulatory approval", "fda approval", "approved", "approves", "허가", "승인"),
    "regulatory_application": ("applies for", "seeks approval", "submits application", "신청", "허가 신청"),
    "termination": ("terminates", "terminated", "cancels", "cancelled", "ends partnership", "해지", "종료", "철회"),
    "clinical_trial": ("clinical trial", "phase 1", "phase 2", "phase 3", "임상", "시험"),
    "penalty": ("penalty", "fine", "sanction", "과징금", "벌금", "제재"),
}

_MILESTONE_TERMS = {
    "filing": ("filing", "files for", "application", "예비심사", "신청"),
    "pricing": ("pricing", "priced", "공모가", "가격 확정"),
    "signed": ("signed", "signs", "체결", "계약"),
    "launch": ("launch", "launched", "출시", "상용화"),
    "approval": ("approval", "approved", "허가", "승인"),
    "trial_start": ("trial begins", "trial starts", "임상 개시", "시험 개시"),
    "trial_result": ("trial results", "topline", "임상 결과", "시험 결과"),
    "closing": ("closing", "closed", "거래 종결", "인수 완료"),
}

_STOPWORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "into", "of", "on", "the", "to", "with",
    "announces", "announced", "company", "regulator", "update", "new", "대한", "관련", "발표",
}

_EN_COUNTERPARTY = re.compile(
    r"(?:with|from|versus|vs\.?|acquisition of|merger with|contract with|agreement with)\s+"
    r"([A-Z][A-Za-z0-9.&-]*(?:\s+[A-Z][A-Za-z0-9.&-]*){0,2})"
)
_KO_COUNTERPARTY = re.compile(r"([가-힣A-Za-z0-9][가-힣A-Za-z0-9.&-]{1,30})(?:와|과|와의|과의)\s*(?:협력|제휴|계약|협약|파트너십)")
_DATE_PATTERNS = (
    re.compile(r"\b20\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?\b"),
    re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,?\s+20\d{2})?\b", re.I),
    re.compile(r"\b20\d{2}년\s*\d{1,2}월(?:\s*\d{1,2}일)?|\b\d{1,2}월\s*\d{1,2}일\b"),
)
_AMOUNT = re.compile(r"(?:[$€£₩]\s?\d[\d,.]*(?:\s?(?:k|m|mn|b|bn|million|billion))?|\d[\d,.]*\s?(?:억원|백만원|만원|원|달러|usd|krw|eur|million|billion|mn|bn))", re.I)
_PERCENTAGE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|percent|퍼센트)(?!\w)", re.I)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")


def _normalized_phrase(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _phrase_hits(text: str, mapping: dict[str, tuple[str, ...]]) -> frozenset[str]:
    folded = _normalized_phrase(text)
    return frozenset(name for name, phrases in mapping.items() if any(_normalized_phrase(phrase) in folded for phrase in phrases))


def _tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+|[가-힣]+", normalized_title(value)))


@dataclass(frozen=True)
class EventAnchors:
    action_terms: frozenset[str]
    counterparties: frozenset[str]
    milestone_terms: frozenset[str]
    explicit_date_tokens: frozenset[str]
    numeric_tokens: frozenset[str]
    amount_tokens: frozenset[str]
    percentage_tokens: frozenset[str]
    subject_terms: frozenset[str]
    event_families: frozenset[str] = frozenset()

    @classmethod
    def from_article(cls, article: Article, *, event_families: tuple[str, ...] = ()) -> "EventAnchors":
        text = " ".join(part for part in (article.title, article.description, article.text) if part)
        dates = frozenset(_normalized_phrase(match.group(0)) for pattern in _DATE_PATTERNS for match in pattern.finditer(text))
        amounts = frozenset(_normalized_phrase(match.group(0)).replace(",", "") for match in _AMOUNT.finditer(text))
        percentages = frozenset(_normalized_phrase(match.group(0)) for match in _PERCENTAGE.finditer(text))
        counterparties = frozenset(
            _normalized_phrase(match.group(1))
            for pattern in (_EN_COUNTERPARTY, _KO_COUNTERPARTY)
            for match in pattern.finditer(text)
        )
        actions = _phrase_hits(text, _ACTION_TERMS)
        milestones = _phrase_hits(text, _MILESTONE_TERMS)
        excluded = _STOPWORDS | set(actions) | set(milestones)
        subjects = frozenset(token for token in _tokens(article.title) if token not in excluded and not token.isdigit())
        return cls(
            action_terms=actions,
            counterparties=counterparties,
            milestone_terms=milestones,
            explicit_date_tokens=dates,
            numeric_tokens=frozenset(_NUMBER.findall(_normalized_phrase(text))),
            amount_tokens=amounts,
            percentage_tokens=percentages,
            subject_terms=subjects,
            event_families=frozenset(family for family in event_families if family and family != "none"),
        )

    def conflict_reason(self, other: "EventAnchors") -> str | None:
        checks = (
            ("amount_conflict", self.amount_tokens, other.amount_tokens),
            ("percentage_conflict", self.percentage_tokens, other.percentage_tokens),
            ("counterparty_conflict", self.counterparties, other.counterparties),
            ("action_conflict", self.action_terms, other.action_terms),
            ("milestone_conflict", self.milestone_terms, other.milestone_terms),
            ("explicit_date_conflict", self.explicit_date_tokens, other.explicit_date_tokens),
            ("event_family_conflict", self.event_families, other.event_families),
        )
        for reason, left, right in checks:
            if left and right and left.isdisjoint(right):
                return reason
        return None

    def distinctive_overlap(self, other: "EventAnchors") -> frozenset[str]:
        overlap: set[str] = set()
        for prefix, left, right in (
            ("amount", self.amount_tokens, other.amount_tokens),
            ("percentage", self.percentage_tokens, other.percentage_tokens),
            ("counterparty", self.counterparties, other.counterparties),
            ("action", self.action_terms, other.action_terms),
            ("milestone", self.milestone_terms, other.milestone_terms),
            ("date", self.explicit_date_tokens, other.explicit_date_tokens),
        ):
            overlap.update(f"{prefix}:{value}" for value in left & right)
        return frozenset(overlap)

    def payload(self) -> dict[str, list[str]]:
        return {
            "action_terms": sorted(self.action_terms),
            "counterparties": sorted(self.counterparties),
            "milestone_terms": sorted(self.milestone_terms),
            "explicit_date_tokens": sorted(self.explicit_date_tokens),
            "numeric_tokens": sorted(self.numeric_tokens),
            "amount_tokens": sorted(self.amount_tokens),
            "percentage_tokens": sorted(self.percentage_tokens),
            "subject_terms": sorted(self.subject_terms),
            "event_families": sorted(self.event_families),
        }

    def signature_tokens(self) -> frozenset[str]:
        return frozenset().union(
            self.action_terms,
            self.counterparties,
            self.milestone_terms,
            self.explicit_date_tokens,
            self.amount_tokens,
            self.percentage_tokens,
            self.subject_terms,
        )

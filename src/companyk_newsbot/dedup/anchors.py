"""Canonical, conservative event anchors used as high-precision guardrails."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import re
import unicodedata

from companyk_newsbot.dedup.article import normalized_title
from companyk_newsbot.models import Article


_ACTION_TERMS = {
    "funding": ("raise", "raises", "raised", "funding", "financing", "investment round", "투자 유치", "투자유치", "펀딩"),
    "investment": ("invests in", "investment in", "stake investment", "지분 투자"),
    "acquisition": ("acquire", "acquires", "acquired", "acquisition", "buyout", "buys", "merger", "인수", "합병"),
    "partnership": ("partnership", "partners with", "partnered with", "collaboration", "제휴", "협력", "파트너십"),
    "contract": ("agreement", "contract", "협약", "계약"),
    "ipo_filing": ("ipo filing", "files for ipo", "filed for ipo", "listing application", "상장 예비심사", "상장예비심사", "상장 신청"),
    "ipo_pricing": ("ipo pricing", "prices ipo", "priced ipo", "공모가 확정", "공모가격"),
    "commercial_launch": ("commercial launch", "launches", "launched", "product launch", "출시", "상용화"),
    "regulatory_approval": ("regulatory approval", "fda approval", "approval", "approved", "approves", "허가", "승인"),
    "regulatory_application": ("applies for", "seeks approval", "submits application", "신청", "허가 신청"),
    "termination": ("terminates", "terminated", "cancels", "cancelled", "ends partnership", "해지", "종료", "철회"),
    "trial_result": ("trial results", "trial result", "topline", "임상 결과", "시험 결과"),
    "clinical_trial": ("clinical trial", "phase 1", "phase 2", "phase 3", "임상", "시험"),
    "penalty": ("penalty", "fine", "sanction", "과징금", "벌금", "제재"),
}

_MILESTONE_TERMS = {
    "filing": ("filing", "files for", "application", "예비심사", "신청"),
    "pricing": ("pricing", "prices", "priced", "공모가", "가격 확정"),
    "signed": ("signed", "signs", "체결"),
    "launch": ("launch", "launched", "launches", "출시", "상용화"),
    "approval": ("approval", "approved", "approves", "허가", "승인"),
    "trial_start": ("trial begins", "trial starts", "임상 개시", "시험 개시"),
    "trial_result": ("trial results", "trial result", "topline", "임상 결과", "시험 결과"),
    "closing": ("closing", "closed", "거래 종결", "인수 완료"),
}

_STOPWORDS = {
    "a", "an", "and", "at", "by", "for", "from", "in", "into", "of", "on", "the", "to", "with",
    "announces", "announced", "company", "regulator", "update", "new", "대한", "관련", "발표",
}

_EN_COUNTERPARTY_SEGMENT = re.compile(
    r"(?:partnership with|partners with|partnered with|from|versus|vs\.?|acquisition of|merger with|contract with|agreement with|with)\s+"
    r"([A-Z][A-Za-z0-9.'-]*(?:\s+(?:(?:and|&)\s+)?[A-Z][A-Za-z0-9.'-]*|\s*,\s*[A-Z][A-Za-z0-9.'-]*){0,8})"
)
_EN_PARTY_SPLIT = re.compile(r"\s*(?:,|\band\b|&)\s*", re.I)
_KO_LIST_MEMBER = re.compile(r"([가-힣A-Za-z0-9.&-]{2,30})(?=\s*(?:와|과|·|,))")
_KO_FINAL_MEMBER = re.compile(r"([가-힣A-Za-z0-9.&-]{2,30})(?:와|과)?\s*(?=(?:협력|제휴|계약|협약|파트너십))")

_ISO_DATE = re.compile(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_KO_DATE = re.compile(r"\b(20\d{2})년\s*(\d{1,2})월\s*(\d{1,2})일\b")
_KO_DAY_OF_MONTH = re.compile(r"(?<!\d)([1-9]|[12]\d|3[01])일(?=\s|$|[,.…'\"])")
_MONTH_DATE = re.compile(
    r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2}),?\s+(20\d{2})\b",
    re.I,
)
_MONTHS = {name: index for index, names in enumerate(
    (("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "september"), ("oct", "october"), ("nov", "november"), ("dec", "december")),
    start=1,
) for name in names}

_NUMBER_TEXT = r"\d+(?:,\d{3})*(?:\.\d+)?"
_KRW_KO = re.compile(rf"({_NUMBER_TEXT})\s*(천만|백만|천|만|억|조)?\s*원", re.I)
_KRW_WON = re.compile(rf"({_NUMBER_TEXT})\s*(thousand|million|billion|trillion|k|m|mn|b|bn)?\s*won\b", re.I)
_KRW_PREFIX = re.compile(rf"(?:₩|krw\s*)({_NUMBER_TEXT})\s*(thousand|million|billion|trillion|k|m|mn|b|bn)?", re.I)
_USD_PREFIX = re.compile(rf"(?:\$|usd\s*)({_NUMBER_TEXT})\s*(thousand|million|billion|trillion|k|m|mn|b|bn)?", re.I)
_USD_SUFFIX = re.compile(rf"({_NUMBER_TEXT})\s*(thousand|million|billion|trillion|k|m|mn|b|bn)?\s*(?:dollars?|usd)\b", re.I)
_PERCENTAGE = re.compile(rf"\b({_NUMBER_TEXT})\s*(?:%|percent|pct)(?!\w)", re.I)
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")

_KO_MULTIPLIERS = {None: Decimal(1), "천": Decimal(1_000), "만": Decimal(10_000), "백만": Decimal(1_000_000), "천만": Decimal(10_000_000), "억": Decimal(100_000_000), "조": Decimal(1_000_000_000_000)}
_EN_MULTIPLIERS = {None: Decimal(1), "": Decimal(1), "k": Decimal(1_000), "thousand": Decimal(1_000), "m": Decimal(1_000_000), "mn": Decimal(1_000_000), "million": Decimal(1_000_000), "b": Decimal(1_000_000_000), "bn": Decimal(1_000_000_000), "billion": Decimal(1_000_000_000), "trillion": Decimal(1_000_000_000_000)}


def _normalized_phrase(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _phrase_position(text: str, phrase: str) -> int | None:
    folded, target = _normalized_phrase(text), _normalized_phrase(phrase)
    if not target:
        return None
    if re.fullmatch(r"[a-z0-9 ]+", target):
        match = re.search(rf"(?<![a-z0-9]){re.escape(target)}(?![a-z0-9])", folded)
        return match.start() if match else None
    position = folded.find(target)
    return position if position >= 0 else None


def _ordered_hits(text: str, mapping: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    positions: list[tuple[int, str]] = []
    for name, phrases in mapping.items():
        found = [position for phrase in phrases if (position := _phrase_position(text, phrase)) is not None]
        if found:
            positions.append((min(found), name))
    return tuple(name for _, name in sorted(positions))


def _tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+|[가-힣]+", normalized_title(value)))


def _counterparties(title: str) -> frozenset[str]:
    parties: set[str] = set()
    for match in _EN_COUNTERPARTY_SEGMENT.finditer(title):
        for value in _EN_PARTY_SPLIT.split(match.group(1)):
            normalized = _normalized_phrase(value)
            if normalized:
                parties.add(normalized)
    for pattern in (_KO_LIST_MEMBER, _KO_FINAL_MEMBER):
        parties.update(_normalized_phrase(match.group(1)) for match in pattern.finditer(title))
    return frozenset(parties)


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", ""))
    except InvalidOperation:
        return None


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def _amounts(text: str) -> frozenset[str]:
    values: set[str] = set()
    patterns = (
        (_KRW_KO, "KRW", _KO_MULTIPLIERS),
        (_KRW_WON, "KRW", _EN_MULTIPLIERS),
        (_KRW_PREFIX, "KRW", _EN_MULTIPLIERS),
        (_USD_PREFIX, "USD", _EN_MULTIPLIERS),
        (_USD_SUFFIX, "USD", _EN_MULTIPLIERS),
    )
    for pattern, currency, multipliers in patterns:
        for match in pattern.finditer(text):
            number = _decimal(match.group(1))
            unit = (match.group(2) or "").casefold() if match.lastindex and match.lastindex >= 2 else ""
            multiplier = multipliers.get(unit if unit else None, multipliers.get(unit))
            if number is not None and multiplier is not None:
                values.add(f"{currency}:{_decimal_text(number * multiplier)}")
    return frozenset(values)


def _percentages(text: str) -> frozenset[str]:
    values: set[str] = set()
    for match in _PERCENTAGE.finditer(text):
        number = _decimal(match.group(1))
        if number is not None:
            values.add(_decimal_text(number / Decimal(100)))
    return frozenset(values)


def _dates(text: str, *, reference_date: date | None = None) -> frozenset[str]:
    values: set[str] = set()
    candidates: list[tuple[int, int, int]] = []
    candidates.extend((int(match.group(1)), int(match.group(2)), int(match.group(3))) for match in _ISO_DATE.finditer(text))
    candidates.extend((int(match.group(1)), int(match.group(2)), int(match.group(3))) for match in _KO_DATE.finditer(text))
    candidates.extend((int(match.group(3)), _MONTHS[match.group(1).casefold()], int(match.group(2))) for match in _MONTH_DATE.finditer(text))
    for year, month, day in candidates:
        try:
            values.add(date(year, month, day).isoformat())
        except ValueError:
            continue
    # Korean headlines commonly use a day-of-month without the year/month
    # (for example, "20일 첫 시험비행"). Resolve only a nearby date relative
    # to the article publication date; distant dates are too ambiguous to use
    # as a deduplication identity anchor.
    if reference_date is not None:
        for match in _KO_DAY_OF_MONTH.finditer(text):
            day = int(match.group(1))
            candidates = []
            for month_offset in (-1, 0, 1):
                month = reference_date.month + month_offset
                year = reference_date.year
                if month < 1:
                    year, month = year - 1, month + 12
                elif month > 12:
                    year, month = year + 1, month - 12
                try:
                    candidates.append(date(year, month, day))
                except ValueError:
                    continue
            if candidates:
                resolved = min(candidates, key=lambda value: abs((value - reference_date).days))
                if abs((resolved - reference_date).days) <= 14:
                    values.add(resolved.isoformat())
    return frozenset(values)


@dataclass(frozen=True)
class EventAnchors:
    action_terms: frozenset[str]
    primary_action_terms: frozenset[str]
    secondary_action_terms: frozenset[str]
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
        text = "\n".join(part for part in (article.title, article.description, article.text) if part)
        ordered_actions = _ordered_hits(article.title, _ACTION_TERMS)
        all_actions = tuple(dict.fromkeys((*ordered_actions, *_ordered_hits(text, _ACTION_TERMS))))
        # "시험" in aerospace reporting means a flight/test launch, not a
        # clinical trial. Treating it as the latter creates a false action
        # identity and prevents otherwise identical launch coverage merging.
        aerospace_context = any(term in text for term in ("로켓", "발사", "우주", "준궤도", "위성", "시험비행"))
        if aerospace_context:
            all_actions = tuple(action for action in all_actions if action != "clinical_trial")
        primary = frozenset(all_actions[:1])
        secondary = frozenset(action for action in all_actions if action not in primary)
        actions = frozenset(all_actions)
        milestones = frozenset(_ordered_hits(article.title, _MILESTONE_TERMS))
        excluded = _STOPWORDS | set(actions) | set(milestones)
        subjects = frozenset(token for token in _tokens(article.title) if token not in excluded and not token.isdigit())
        return cls(
            action_terms=actions,
            primary_action_terms=primary,
            secondary_action_terms=secondary,
            counterparties=_counterparties(article.title),
            milestone_terms=milestones,
            explicit_date_tokens=_dates(text, reference_date=article.published_at.date() if article.published_at else None),
            numeric_tokens=frozenset(_NUMBER.findall(_normalized_phrase(text))),
            amount_tokens=_amounts(text),
            percentage_tokens=_percentages(text),
            subject_terms=subjects,
            event_families=frozenset(family for family in event_families if family and family != "none"),
        )

    def conflict_reason(self, other: "EventAnchors") -> str | None:
        checks = (
            ("amount_conflict", self.amount_tokens, other.amount_tokens),
            ("percentage_conflict", self.percentage_tokens, other.percentage_tokens),
            ("counterparty_conflict", self.counterparties, other.counterparties),
            ("action_conflict", self.primary_action_terms, other.primary_action_terms),
            ("milestone_conflict", self.milestone_terms, other.milestone_terms),
            ("explicit_date_conflict", self.explicit_date_tokens, other.explicit_date_tokens),
            ("event_family_conflict", self.event_families, other.event_families),
        )
        for reason, left, right in checks:
            if left and right and left.isdisjoint(right):
                return reason
        return None

    def has_partial_distinctive_mismatch(self, other: "EventAnchors") -> bool:
        for left, right in (
            (self.amount_tokens, other.amount_tokens),
            (self.percentage_tokens, other.percentage_tokens),
            (self.counterparties, other.counterparties),
            (self.primary_action_terms, other.primary_action_terms),
            (self.milestone_terms, other.milestone_terms),
            (self.explicit_date_tokens, other.explicit_date_tokens),
        ):
            if left and right and left != right:
                return True
        return False

    def shared_identity_categories(self, other: "EventAnchors") -> frozenset[str]:
        shared = {
            name
            for name, left, right in (
                ("amount", self.amount_tokens, other.amount_tokens),
                ("percentage", self.percentage_tokens, other.percentage_tokens),
                ("counterparty", self.counterparties, other.counterparties),
                ("date", self.explicit_date_tokens, other.explicit_date_tokens),
            )
            if left and right and left == right
        }
        return frozenset(shared)

    def payload(self) -> dict[str, list[str]]:
        return {
            "action_terms": sorted(self.action_terms),
            "primary_action_terms": sorted(self.primary_action_terms),
            "secondary_action_terms": sorted(self.secondary_action_terms),
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
            self.primary_action_terms,
            self.counterparties,
            self.milestone_terms,
            self.explicit_date_tokens,
            self.amount_tokens,
            self.percentage_tokens,
            self.subject_terms,
        )

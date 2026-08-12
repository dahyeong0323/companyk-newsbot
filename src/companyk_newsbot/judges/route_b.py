"""Structured OpenAI judge for pre-filtered Route B exposure candidates."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from companyk_newsbot.rules.route_b import RouteBCandidate


PROMPT_VERSION = "route_b_causal_materiality_v1"

SYSTEM_PROMPT = """You judge one pre-filtered Company K external-impact news candidate.
Return a decision only for the supplied company-specific exposure. Do not infer
new exposures or widen the exposure into general sector news.

Qualify only when the article describes a real, material external event that:
1. fits the registered exposure subject;
2. fits one of its allowed event families;
3. has a credible company-specific causal mechanism; and
4. is more than broad industry commentary.

If it does not qualify, set qualifies=false, materiality=none,
event_family=none, impact_direction=neutral, and select one rejection_reason.
Use only the article and registered exposure supplied. Be concise and factual."""


class JudgeOutput(BaseModel):
    """The version-controlled structured contract supplied to the model."""

    model_config = ConfigDict(extra="forbid")

    qualifies: bool
    company: str = Field(min_length=1)
    exposure_id: str = Field(min_length=1)
    event_family: str
    materiality: Literal["high", "medium", "low", "none"]
    impact_direction: Literal["positive", "negative", "mixed", "neutral"]
    causal_mechanism: str = Field(min_length=1)
    rejection_reason: Literal[
        "none",
        "broad_industry_only",
        "wrong_context",
        "non_material",
        "wrong_jurisdiction",
        "weak_causal_link",
        "duplicate_event",
    ]


class JudgeError(RuntimeError):
    """Raised for unavailable, malformed, or contract-invalid judge results."""


class ResponsesParser(Protocol):
    def parse(self, **kwargs: Any) -> Any: ...


class OpenAIResponsesClient(Protocol):
    responses: ResponsesParser


@dataclass(frozen=True)
class JudgedRouteBCandidate:
    candidate: RouteBCandidate
    decision: JudgeOutput
    prompt_version: str
    model: str


class RouteBCausalMaterialityJudge:
    """Submit only Route B candidates to a strict, typed structured-output call."""

    def __init__(self, client: OpenAIResponsesClient, *, model: str, reasoning_effort: str = "medium") -> None:
        if not model.strip():
            raise ValueError("OpenAI model must not be blank")
        self._client = client
        self.model = model
        self.reasoning_effort = reasoning_effort

    @classmethod
    def from_environment(cls, *, timeout: float | None = None) -> "RouteBCausalMaterialityJudge":
        model = os.getenv("OPENAI_MODEL", "gpt-5.6-sol").strip()
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dependency installation is verified separately
            raise JudgeError("OpenAI SDK is not installed") from exc
        client = OpenAI(timeout=timeout) if timeout is not None else OpenAI()
        return cls(client, model=model, reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "medium"))

    def judge(self, candidate: RouteBCandidate) -> JudgedRouteBCandidate:
        response = self._client.responses.parse(
            model=self.model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": self._candidate_payload(candidate)},
            ],
            text_format=JudgeOutput,
            reasoning={"effort": self.reasoning_effort},
        )
        parsed = getattr(response, "output_parsed", None)
        if not isinstance(parsed, JudgeOutput):
            raise JudgeError("OpenAI response did not contain a parsed Route B judge decision")
        self._validate_contract(candidate, parsed)
        return JudgedRouteBCandidate(candidate=candidate, decision=parsed, prompt_version=PROMPT_VERSION, model=self.model)

    @staticmethod
    def _candidate_payload(candidate: RouteBCandidate) -> str:
        article = candidate.article
        return json.dumps(
            {
                "registered_exposure": {
                    "company": candidate.company,
                    "exposure_id": candidate.exposure_id,
                    "subject": candidate.exposure_subject,
                    "allowed_event_families": candidate.allowed_event_families,
                },
                "article": {
                    "title": article.title,
                    "description": article.description,
                    "text": article.text,
                    "source": article.source,
                    "url": article.canonical_url,
                    "published_at": article.published_at.isoformat() if article.published_at else None,
                },
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _validate_contract(candidate: RouteBCandidate, decision: JudgeOutput) -> None:
        if decision.company != candidate.company:
            raise JudgeError("judge returned a company different from the candidate exposure")
        if decision.exposure_id != candidate.exposure_id:
            raise JudgeError("judge returned an exposure_id different from the candidate exposure")
        if decision.qualifies:
            if decision.event_family not in candidate.allowed_event_families:
                raise JudgeError("qualified decision used an event family not allowed by the exposure")
            if decision.materiality == "none" or decision.rejection_reason != "none":
                raise JudgeError("qualified decision must include materiality and no rejection reason")
        elif (
            decision.event_family != "none"
            or decision.materiality != "none"
            or decision.impact_direction != "neutral"
            or decision.rejection_reason == "none"
        ):
            raise JudgeError("rejected decision must use the required neutral/none fields and a rejection reason")

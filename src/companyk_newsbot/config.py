"""Typed loading and structural validation for the frozen FINAL keyword map."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError, field_validator, model_validator


class ConfigLoadError(ValueError):
    """Raised when a keyword map cannot be parsed or does not meet its contract."""


class Evidence(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_type: str = Field(min_length=1)
    url: HttpUrl


class ExposureSubject(BaseModel):
    model_config = ConfigDict(extra="allow")

    canonical: str = Field(min_length=1)
    query_terms: list[str] = Field(min_length=1)

    @field_validator("query_terms")
    @classmethod
    def query_terms_are_not_blank(cls, values: list[str]) -> list[str]:
        if any(not item.strip() for item in values):
            raise ValueError("query terms must not be blank")
        return values


class Exposure(BaseModel):
    model_config = ConfigDict(extra="allow")

    exposure_id: str = Field(min_length=1)
    type: str = Field(min_length=1)
    subject: ExposureSubject
    valid_from: date
    evidence: Evidence
    allowed_event_families: list[str] = Field(min_length=1)
    required_event_context: list[str] = Field(min_length=1)
    likely_impact_mechanisms: list[str] = Field(min_length=1)


class ZeroExposureClosure(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: bool
    reason: str = Field(min_length=1)
    review_date: date


class CompanyRule(BaseModel):
    model_config = ConfigDict(extra="allow")

    aliases: list[str]
    external_exposures: list[Exposure] | None = None
    no_justified_external_exposure: ZeroExposureClosure | None = None

    @model_validator(mode="after")
    def has_explicit_exposure_state(self) -> "CompanyRule":
        has_exposures = bool(self.external_exposures)
        has_zero_closure = self.no_justified_external_exposure is not None
        if has_exposures == has_zero_closure:
            raise ValueError(
                "must contain either non-empty external_exposures or "
                "no_justified_external_exposure, but not both"
            )
        if has_zero_closure and not self.no_justified_external_exposure.status:
            raise ValueError("no_justified_external_exposure.status must be true")
        return self


class ExternalImpactLogic(BaseModel):
    model_config = ConfigDict(extra="allow")

    event_families: dict[str, str] = Field(min_length=1)
    matching_rules: dict[str, Any] = Field(min_length=1)
    query_registry: dict[str, Any]
    causal_judge: dict[str, Any]

    @model_validator(mode="after")
    def matching_rules_exist_for_every_event_family(self) -> "ExternalImpactLogic":
        missing = set(self.event_families).difference(self.matching_rules)
        if missing:
            raise ValueError(f"event families missing matching rules: {sorted(missing)}")
        return self


class KeywordMapConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: str = Field(min_length=1)
    name: str = Field(min_length=1)
    company_rules: dict[str, CompanyRule] = Field(min_length=1)
    external_impact_logic: ExternalImpactLogic

    @model_validator(mode="after")
    def validate_exposure_ids_and_event_families(self) -> "KeywordMapConfig":
        seen_ids: set[str] = set()
        event_families = set(self.external_impact_logic.event_families)
        for company_name, company in self.company_rules.items():
            if not company_name.strip():
                raise ValueError("company_rules must not have a blank company name")
            for exposure in company.external_exposures or []:
                if exposure.exposure_id in seen_ids:
                    raise ValueError(f"duplicate exposure_id: {exposure.exposure_id}")
                seen_ids.add(exposure.exposure_id)
                unknown = set(exposure.allowed_event_families).difference(event_families)
                if unknown:
                    raise ValueError(
                        f"exposure {exposure.exposure_id} references unknown event families: {sorted(unknown)}"
                    )
        return self


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "keyword_map_FINAL.yaml"


def load_keyword_map(path: Path | str = DEFAULT_CONFIG_PATH) -> KeywordMapConfig:
    """Load the map, retaining all unmodelled frozen fields while validating runtime essentials."""
    source = Path(path)
    if not source.is_file():
        raise ConfigLoadError(f"keyword map does not exist: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigLoadError(f"could not parse keyword map {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigLoadError("keyword map root must be a mapping")
    try:
        return KeywordMapConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigLoadError(f"invalid keyword map {source}: {exc}") from exc

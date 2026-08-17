"""Typed Route A portfolio registry, query planning, and legacy adapters."""
from __future__ import annotations

from hashlib import sha256
from datetime import UTC, datetime
from pathlib import Path
import re
import unicodedata

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from companyk_newsbot.config import ConfigLoadError, KeywordMapConfig


def normalized_identity(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(r"\s+", "", value)
    return re.sub(r"(?:\(주\)|주식회사|inc\.?|ltd\.?|limited)$", "", value).strip(" ,.")


_FORMER_RE = re.compile(r"\(구\s*[,\.]\s*(.+)\)\s*$", re.IGNORECASE)
_LEGAL_PREFIX_RE = re.compile(r"^(?:\(주\)|㈜|주식회사)\s*", re.IGNORECASE)
_LEGAL_SUFFIX_RE = re.compile(
    r"\s*(?:\(주\)|㈜|주식회사|,?\s*Pte\.?\s*Ltd\.?|,?\s*GmbH|,?\s*LLC|,?\s*LLP|,?\s*PLC|,?\s*Inc\.?|,?\s*Ltd\.?|\s+Limited)$",
    re.IGNORECASE,
)


def parse_source_name(source_name: str) -> tuple[str, str | None]:
    """Return the current legal name and an explicitly encoded former name."""
    value = unicodedata.normalize("NFKC", source_name).strip()
    match = _FORMER_RE.search(value)
    if not match:
        return value, None
    current = value[: match.start()].strip()
    former = match.group(1).strip()
    if not current or not former:
        raise ValueError(f"malformed former-name syntax: {source_name}")
    return current, former


def display_name(legal_name: str) -> str:
    value = _LEGAL_PREFIX_RE.sub("", legal_name).strip()
    previous = None
    while value != previous:
        previous = value
        value = _LEGAL_SUFFIX_RE.sub("", value).strip(" ,.")
    return value or legal_name.strip()


def _unique_terms(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = unicodedata.normalize("NFKC", value).strip()
        key = value.casefold()
        if value and key not in seen:
            seen.add(key); output.append(value)
    return output


def stable_company_id(name: str) -> str:
    return "company-" + sha256(normalized_identity(name).encode("utf-8")).hexdigest()[:16]


class RegistrySource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workbook: str = Field(min_length=1)
    sheet: str = Field(min_length=1)
    column: str = Field(min_length=1)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: str = Field(min_length=1)
    company_count: int = Field(ge=1)


class AmbiguityRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_context: list[str] = Field(default_factory=list)
    negative_context: list[str] = Field(default_factory=list)
    forbidden_standalone: list[str] = Field(default_factory=list)
    required_context_for_forbidden: dict[str, list[str]] | list[str] = Field(default_factory=dict)
    english_negative_context: list[str] = Field(default_factory=list)
    english_required_context_for_short_form: dict[str, list[str]] = Field(default_factory=dict)


class CorporateIdentityMetadata(BaseModel):
    """Stable non-personal facts anchoring a portfolio record to one legal entity."""

    model_config = ConfigDict(extra="forbid")
    corporate_registry_number: str = Field(pattern=r"^\d{6}-\d{7}$")
    registered_head_office: str = Field(min_length=1)
    website: str = Field(pattern=r"^https://")
    business_purposes: list[str] = Field(min_length=1)
    source_document: str = Field(min_length=1)


class PortfolioCompany(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{7,63}$")
    display_name: str = Field(min_length=1)
    source_name: str = Field(min_length=1)
    legal_names: list[str] = Field(min_length=1)
    former_names: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(min_length=1)
    match_terms: list[str] = Field(min_length=1)
    ambiguity: AmbiguityRules = Field(default_factory=AmbiguityRules)
    identity_metadata: CorporateIdentityMetadata | None = None

    @field_validator("legal_names", "former_names", "search_terms", "match_terms")
    @classmethod
    def terms_are_clean_and_unique(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("registry terms must not be blank")
        normalized = [unicodedata.normalize("NFKC", value).strip() for value in values]
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise ValueError("registry terms must be unique after normalization")
        return normalized


class PortfolioRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = Field(min_length=1)
    source: RegistrySource
    companies: list[PortfolioCompany] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> "PortfolioRegistry":
        if self.source.company_count != len(self.companies):
            raise ValueError("source.company_count must equal companies length")
        ids = [company.company_id for company in self.companies]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate company_id")
        identities: dict[str, str] = {}
        terms: dict[str, str] = {}
        for company in self.companies:
            identity = normalized_identity(company.display_name)
            if not identity:
                raise ValueError("company identity normalizes to blank")
            if identity in identities:
                raise ValueError(f"normalized company identity conflict: {company.display_name} / {identities[identity]}")
            identities[identity] = company.display_name
            for term in (*company.search_terms, *company.match_terms):
                key = normalized_identity(term)
                owner = terms.get(key)
                if owner and owner != company.company_id:
                    raise ValueError(f"cross-company term conflict: {term}")
                terms[key] = company.company_id
        return self

    @classmethod
    def from_legacy(cls, config: KeywordMapConfig) -> "PortfolioRegistry":
        companies = []
        for name, rule in config.company_rules.items():
            extra = rule.model_extra or {}
            ambiguity = {
                "required_context": extra.get("required_context", []),
                "negative_context": extra.get("negative_terms", []),
                "forbidden_standalone": extra.get("forbidden_standalone", []),
                "required_context_for_forbidden": extra.get("required_context_for_forbidden", {}),
                "english_negative_context": extra.get("english_negative_context", []),
                "english_required_context_for_short_form": extra.get("english_required_context_for_short_form", {}),
            }
            company_id = "company-" + sha256(normalized_identity(name).encode()).hexdigest()[:16]
            terms = list(dict.fromkeys([name, *rule.aliases]))
            companies.append({"company_id": company_id, "display_name": name, "source_name": name,
                "legal_names": [name], "former_names": [], "search_terms": [name], "match_terms": terms,
                "ambiguity": ambiguity})
        return cls.model_validate({"schema_version": "legacy-adapter-1", "source": {
            "workbook": "legacy-keyword-map", "sheet": "company_rules", "column": "company_name",
            "source_sha256": "0" * 64, "generated_at": "legacy", "company_count": len(companies)}, "companies": companies})


DEFAULT_PORTFOLIO_REGISTRY_PATH = Path.cwd() / "config" / "portfolio_registry.yaml"


def load_portfolio_registry(path: Path | str = DEFAULT_PORTFOLIO_REGISTRY_PATH) -> PortfolioRegistry:
    source = Path(path)
    if not source.is_file():
        raise ConfigLoadError(f"portfolio registry does not exist: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        return PortfolioRegistry.model_validate(raw)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigLoadError(f"invalid portfolio registry {source}: {exc}") from exc


class DirectQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: tuple[str, ...]
    company_ids_by_query: dict[str, tuple[str, ...]]
    uncovered_company_ids: tuple[str, ...] = ()


def build_direct_query_plan(registry: PortfolioRegistry) -> DirectQueryPlan:
    queries: list[str] = []
    owners: dict[str, list[str]] = {}
    for company in registry.companies:
        for term in company.search_terms:
            key = unicodedata.normalize("NFKC", term).casefold().strip()
            if key not in owners:
                queries.append(term)
                owners[key] = []
            owners[key].append(company.company_id)
    covered = {company_id for values in owners.values() for company_id in values}
    uncovered = tuple(company.company_id for company in registry.companies if company.company_id not in covered)
    return DirectQueryPlan(queries=tuple(queries), company_ids_by_query={key: tuple(value) for key, value in owners.items()}, uncovered_company_ids=uncovered)

from __future__ import annotations

from pathlib import Path

import pytest

from companyk_newsbot.config import ConfigLoadError, load_keyword_map


def write_map(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "map.yaml"
    path.write_text(body, encoding="utf-8")
    return path


VALID_MAP = """
schema_version: "1"
name: test-map
external_impact_logic:
  event_families: {policy_regulatory: "policy"}
  matching_rules: {policy_regulatory: {}}
  query_registry: {}
  causal_judge: {}
company_rules:
  Example Co:
    aliases: [Example]
    external_exposures:
      - exposure_id: example-policy
        type: policy
        subject: {canonical: Example policy, query_terms: [Example law]}
        valid_from: "2024-01-01"
        evidence: {source_type: official, url: "https://example.com/evidence"}
        allowed_event_families: [policy_regulatory]
        required_event_context: [rule]
        likely_impact_mechanisms: [compliance_cost]
"""


def test_loads_valid_map(tmp_path: Path) -> None:
    config = load_keyword_map(write_map(tmp_path, VALID_MAP))
    assert config.company_rules["Example Co"].external_exposures[0].exposure_id == "example-policy"


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("schema_version: \"1\"\nname: test-map\ncompany_rules: {}\n", "external_impact_logic"),
        (VALID_MAP.replace("policy_regulatory]", "unknown_family]"), "unknown event families"),
    ],
)
def test_rejects_malformed_required_structure(tmp_path: Path, replacement: str, message: str) -> None:
    with pytest.raises(ConfigLoadError, match=message):
        load_keyword_map(write_map(tmp_path, replacement))


def test_rejects_duplicate_exposure_ids(tmp_path: Path) -> None:
    duplicated = VALID_MAP.replace("  Example Co:\n", "  Example Co:\n").replace(
        "        likely_impact_mechanisms: [compliance_cost]\n",
        "        likely_impact_mechanisms: [compliance_cost]\n  Other Co:\n    aliases: [Other]\n    external_exposures:\n      - exposure_id: example-policy\n        type: policy\n        subject: {canonical: Other policy, query_terms: [Other law]}\n        valid_from: '2024-01-01'\n        evidence: {source_type: official, url: 'https://example.com/other'}\n        allowed_event_families: [policy_regulatory]\n        required_event_context: [rule]\n        likely_impact_mechanisms: [compliance_cost]\n",
    )
    with pytest.raises(ConfigLoadError, match="duplicate exposure_id"):
        load_keyword_map(write_map(tmp_path, duplicated))


def test_rejects_missing_explicit_exposure_state(tmp_path: Path) -> None:
    missing = VALID_MAP.replace(
        "    external_exposures:\n      - exposure_id: example-policy\n        type: policy\n        subject: {canonical: Example policy, query_terms: [Example law]}\n        valid_from: \"2024-01-01\"\n        evidence: {source_type: official, url: \"https://example.com/evidence\"}\n        allowed_event_families: [policy_regulatory]\n        required_event_context: [rule]\n        likely_impact_mechanisms: [compliance_cost]\n",
        "",
    )
    with pytest.raises(ConfigLoadError, match="either non-empty external_exposures"):
        load_keyword_map(write_map(tmp_path, missing))

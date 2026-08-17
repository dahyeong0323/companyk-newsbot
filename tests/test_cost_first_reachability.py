from __future__ import annotations

import pytest

from companyk_newsbot import e2e
from companyk_newsbot.judges import CascadeSettings, JudgeError


def test_cost_first_classifier_rejects_sol_model_configuration(monkeypatch) -> None:
    monkeypatch.setenv("ROUTE_B_LUNA_MODEL", "gpt-5.6-sol")
    with pytest.raises(JudgeError, match="must not use Sol"):
        CascadeSettings.from_environment()


def test_cost_first_editorial_rejects_sol_model_configuration(monkeypatch) -> None:
    monkeypatch.setenv("NEWSBOT_COST_FIRST_PIPELINE", "true")
    with pytest.raises(e2e.E2EExecutionError, match="must not use Sol"):
        e2e._require_non_sol_model("editorial model", "gpt-5.6-sol")


def test_rollback_switch_allows_frozen_sol_configuration(monkeypatch) -> None:
    monkeypatch.setenv("NEWSBOT_COST_FIRST_PIPELINE", "false")
    e2e._require_non_sol_model("editorial model", "gpt-5.6-sol")

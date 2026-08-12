from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companyk_newsbot import main
from companyk_newsbot.state import JsonStateStore, RunState


def test_json_state_store_persists_run_ledger_atomically(tmp_path) -> None:
    store = JsonStateStore(tmp_path / "state")
    saved = store.record_run(mode="shadow", status="success", details={"items": 2})
    assert saved.last_successful_run
    loaded = store.load()
    assert loaded.run_ledger[-1]["items"] == 2
    assert json.loads(store.path.read_text(encoding="utf-8"))["last_successful_run"]


def test_only_successful_delivery_advances_production_checkpoint(tmp_path) -> None:
    store = JsonStateStore(tmp_path)
    first = datetime(2026, 8, 12, 1, tzinfo=UTC)
    delivered = datetime(2026, 8, 12, 5, tzinfo=UTC)

    store.record_run(mode="live", status="failed", at=first)
    store.record_run(mode="e2e_test", status="success", at=first)
    store.record_run(mode="full_shadow", status="success", checkpoint="shadow", at=first)
    assert store.last_delivery_datetime() is None

    state = store.record_run(mode="live", status="success", checkpoint="delivery", at=delivered)
    assert state.last_successful_delivery_run == delivered.isoformat()
    assert state.last_shadow_run == first.isoformat()
    assert store.last_delivery_datetime() == delivered


def test_state_store_retains_sent_fingerprints_for_idempotency(tmp_path) -> None:
    store = JsonStateStore(tmp_path)
    assert store.was_sent("event-1", kind="event") is False
    store.mark_sent("event-1", kind="event")
    store.mark_sent("event-1", kind="event")
    assert store.was_sent("event-1", kind="event") is True
    assert store.load().sent_event_fingerprints == ["event-1"]


def test_main_records_shadow_run_without_sending(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "shadow")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert main.main() == 0
    assert JsonStateStore(tmp_path).load().run_ledger[-1]["phase"] == "pre_delivery_validation"


def test_main_blocks_live_mode(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "live")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="live mode is blocked"):
        main.main()


def test_main_full_shadow_uses_full_coverage_without_delivery(monkeypatch, tmp_path) -> None:
    observed = {}

    def fake_run(config, store, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(status="success", log_payload=lambda: {"profile": kwargs["profile"]})

    monkeypatch.setenv("RUN_MODE", "full_shadow")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(main, "run_real_e2e", fake_run)

    assert main.main() == 0
    assert observed == {"profile": "full_shadow", "deliver": False}
    assert JsonStateStore(tmp_path).load().run_ledger[-1]["phase"] == "full_shadow_non_delivery"


def test_main_records_inconclusive_smoke_without_checkpoint(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "e2e_test")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        main,
        "run_real_e2e",
        lambda *args, **kwargs: SimpleNamespace(
            status="inconclusive",
            log_payload=lambda: {"status": "inconclusive", "final_items": 0},
        ),
    )

    assert main.main() == 2
    state = JsonStateStore(tmp_path).load()
    assert state.run_ledger[-1]["status"] == "inconclusive"
    assert state.last_successful_delivery_run is None

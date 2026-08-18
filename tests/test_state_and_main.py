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


def test_state_store_marks_a_delivery_batch_in_one_state_save(tmp_path, monkeypatch) -> None:
    store = JsonStateStore(tmp_path)
    saves = 0
    original_save = store.save

    def counted_save(state):
        nonlocal saves
        saves += 1
        original_save(state)

    monkeypatch.setattr(store, "save", counted_save)
    store.mark_sent_many(("event-1", "event-2", "event-1"), kind="event")

    assert saves == 1
    assert store.load().sent_event_fingerprints == ["event-1", "event-2"]


def test_main_records_shadow_run_without_sending(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "shadow")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert main.main() == 0
    assert JsonStateStore(tmp_path).load().run_ledger[-1]["phase"] == "pre_delivery_validation"


def test_main_live_requires_explicit_enablement(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "live")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="PRODUCTION_EMAIL_ENABLED"):
        main.main()


def test_main_live_schedule_guard_never_invokes_delivery(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "live")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PRODUCTION_EMAIL_ENABLED", "true")
    monkeypatch.setattr(main, "_production_schedule_window_is_open", lambda: False)
    monkeypatch.setattr(main, "run_real_e2e", lambda *args, **kwargs: pytest.fail("delivery must not run"))
    assert main.main() == 0
    assert JsonStateStore(tmp_path).load().run_ledger[-1]["phase"] == "production_schedule_guard"


def test_main_live_advances_delivery_checkpoint_only_after_delivery(monkeypatch, tmp_path) -> None:
    observed = {}

    def fake_run(config, store, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(status="success", delivery_id="production-delivery", log_payload=lambda: {"final_items": 1})

    monkeypatch.setenv("RUN_MODE", "live")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PRODUCTION_EMAIL_ENABLED", "true")
    monkeypatch.setattr(main, "_production_schedule_window_is_open", lambda: True)
    monkeypatch.setattr(main, "run_real_e2e", fake_run)
    assert main.main() == 0
    assert observed == {"profile": "production", "deliver": True}
    state = JsonStateStore(tmp_path).load()
    assert state.run_ledger[-1]["phase"] == "production_delivery"
    assert state.last_successful_delivery_run is not None


def test_main_live_inconclusive_keeps_delivery_checkpoint_unchanged(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "live")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PRODUCTION_EMAIL_ENABLED", "true")
    monkeypatch.setattr(main, "_production_schedule_window_is_open", lambda: True)
    monkeypatch.setattr(
        main,
        "run_real_e2e",
        lambda *args, **kwargs: SimpleNamespace(status="inconclusive", delivery_id=None, log_payload=lambda: {"final_items": 0}),
    )
    assert main.main() == 2
    assert JsonStateStore(tmp_path).load().last_successful_delivery_run is None


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


def test_main_records_inconclusive_full_shadow_without_shadow_checkpoint(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "full_shadow")
    monkeypatch.setenv("SHADOW_TEST_EMAIL", "true")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(
        main,
        "run_real_e2e",
        lambda *args, **kwargs: SimpleNamespace(
            status="inconclusive",
            delivery_id=None,
            production_delivery_checkpoint_before=None,
            log_payload=lambda: {"status": "inconclusive", "collection_coverage_status": "INCONCLUSIVE"},
        ),
    )

    assert main.main() == 2
    state = JsonStateStore(tmp_path).load()
    assert state.run_ledger[-1]["phase"] == "full_shadow_inconclusive"
    assert state.last_shadow_run is None
    assert state.last_successful_delivery_run is None

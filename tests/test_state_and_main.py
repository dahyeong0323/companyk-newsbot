from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from companyk_newsbot import main
from companyk_newsbot.state import GcsJsonStateStore, JsonStateStore, RunState, state_store_from_environment


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


class FakePreconditionFailed(Exception):
    pass


class FakeBlob:
    def __init__(self, payload: str | None = None, generation: int = 7) -> None:
        self.payload = payload
        self.generation = generation if payload is not None else None
        self.uploads: list[dict[str, object]] = []
        self.fail_precondition = False

    def exists(self) -> bool:
        return self.payload is not None

    def download_as_text(self, *, encoding: str) -> str:
        assert encoding == "utf-8"
        assert self.payload is not None
        return self.payload

    def upload_from_string(self, data: str, *, content_type: str, if_generation_match: int) -> None:
        if self.fail_precondition:
            raise FakePreconditionFailed()
        self.uploads.append({"data": data, "content_type": content_type, "if_generation_match": if_generation_match})
        self.payload = data
        self.generation = (self.generation or 0) + 1


class FakeGcsClient:
    def __init__(self, blob: FakeBlob) -> None:
        self.blob_instance = blob
        self.bucket_names: list[str] = []
        self.object_names: list[str] = []

    def bucket(self, bucket_name: str):
        self.bucket_names.append(bucket_name)
        return self

    def blob(self, object_name: str) -> FakeBlob:
        self.object_names.append(object_name)
        return self.blob_instance


def gcs_store(blob: FakeBlob) -> GcsJsonStateStore:
    return GcsJsonStateStore(
        "test-state-bucket",
        "production/newsbot_state.json",
        client=FakeGcsClient(blob),
        precondition_exception=FakePreconditionFailed,
    )


def test_gcs_state_missing_object_starts_empty_and_first_save_uses_generation_zero() -> None:
    blob = FakeBlob()
    store = gcs_store(blob)

    assert store.load() == RunState()
    store.mark_sent_many(("event-1", "event-2"), kind="event")

    assert blob.uploads[0]["if_generation_match"] == 0
    assert RunState(**json.loads(blob.payload)).sent_event_fingerprints == ["event-1", "event-2"]


def test_gcs_state_loads_existing_object_and_uses_loaded_generation_for_save() -> None:
    blob = FakeBlob(json.dumps({"sent_article_fingerprints": ["article-1"]}), generation=42)
    store = gcs_store(blob)

    assert store.was_sent("article-1", kind="article") is True
    store.mark_sent("article-2", kind="article")

    assert blob.uploads[-1]["if_generation_match"] == 42
    assert RunState(**json.loads(blob.payload)).sent_article_fingerprints == ["article-1", "article-2"]


def test_gcs_state_invalid_json_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="GCS state object is invalid"):
        gcs_store(FakeBlob("not-json")).load()
    with pytest.raises(RuntimeError, match="GCS state object is invalid"):
        gcs_store(FakeBlob(json.dumps({"sent_event_fingerprints": "not-a-list"}))).load()


def test_gcs_state_precondition_conflict_never_overwrites() -> None:
    blob = FakeBlob()
    store = gcs_store(blob)
    store.load()
    blob.fail_precondition = True

    with pytest.raises(RuntimeError, match="changed concurrently"):
        store.mark_sent("event-1", kind="event")
    assert blob.payload is None


def test_gcs_state_records_delivery_checkpoint() -> None:
    blob = FakeBlob()
    delivered = datetime(2026, 8, 12, 5, tzinfo=UTC)

    state = gcs_store(blob).record_run(mode="live", status="success", checkpoint="delivery", at=delivered)

    assert state.last_successful_delivery_run == delivered.isoformat()
    assert RunState(**json.loads(blob.payload)).last_successful_delivery_run == delivered.isoformat()


def test_state_store_factory_defaults_to_filesystem_and_validates_gcs(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("STATE_BACKEND", raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert isinstance(state_store_from_environment(), JsonStateStore)
    monkeypatch.setenv("STATE_BACKEND", "gcs")
    monkeypatch.delenv("STATE_GCS_BUCKET", raising=False)
    with pytest.raises(RuntimeError, match="STATE_GCS_BUCKET"):
        state_store_from_environment()
    monkeypatch.setenv("STATE_GCS_BUCKET", "test-state-bucket")
    monkeypatch.setenv("STATE_GCS_OBJECT", "")
    gcs_store = state_store_from_environment()
    assert isinstance(gcs_store, GcsJsonStateStore)
    assert gcs_store.object_name == "newsbot_state.json"
    monkeypatch.setenv("STATE_BACKEND", "unknown")
    with pytest.raises(RuntimeError, match="filesystem or gcs"):
        state_store_from_environment()


def test_main_uses_state_store_factory(monkeypatch, tmp_path) -> None:
    store = JsonStateStore(tmp_path)
    monkeypatch.setenv("RUN_MODE", "shadow")
    monkeypatch.setattr(main, "state_store_from_environment", lambda: store)

    assert main.main() == 0
    assert store.load().run_ledger[-1]["phase"] == "pre_delivery_validation"


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

"""Durable state stores for local filesystems and Google Cloud Storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any, Protocol


@dataclass
class RunState:
    sent_article_fingerprints: list[str] = field(default_factory=list)
    sent_event_fingerprints: list[str] = field(default_factory=list)
    last_successful_run: str | None = None
    last_successful_delivery_run: str | None = None
    last_shadow_run: str | None = None
    run_ledger: list[dict[str, Any]] = field(default_factory=list)


class JsonStateStore:
    """Atomic JSON persistence; mount `STATE_DIR=/data` on Railway for durability."""

    def __init__(self, state_dir: Path | str) -> None:
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "newsbot_state.json"

    def load(self) -> RunState:
        if not self.path.exists():
            return RunState()
        try:
            return RunState(**json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"state file is invalid: {self.path}") from exc

    def save(self, state: RunState) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def record_run(
        self,
        *,
        mode: str,
        status: str,
        details: dict[str, Any] | None = None,
        checkpoint: str | None = None,
        at: datetime | None = None,
    ) -> RunState:
        state = self.load()
        timestamp = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        state.run_ledger = [*state.run_ledger[-49:], {"timestamp": timestamp, "mode": mode, "status": status, **(details or {})}]
        if status == "success":
            state.last_successful_run = timestamp
            if checkpoint == "delivery":
                state.last_successful_delivery_run = timestamp
            elif checkpoint == "shadow":
                state.last_shadow_run = timestamp
        elif checkpoint is not None:
            raise ValueError("failed or inconclusive runs cannot advance a checkpoint")
        self.save(state)
        return state

    def last_delivery_datetime(self) -> datetime | None:
        value = self.load().last_successful_delivery_run
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise RuntimeError("last successful delivery checkpoint must be timezone-aware")
        return parsed.astimezone(UTC)

    def was_sent(self, fingerprint: str, *, kind: str) -> bool:
        state = self.load()
        fingerprints = self._fingerprints(state, kind)
        return fingerprint in fingerprints

    def mark_sent(self, fingerprint: str, *, kind: str, limit: int = 5000) -> RunState:
        """Record a sent article/event only after successful delivery in a later step."""
        return self.mark_sent_many((fingerprint,), kind=kind, limit=limit)

    def mark_sent_many(self, fingerprints_to_mark: list[str] | tuple[str, ...], *, kind: str, limit: int = 5000) -> RunState:
        """Persist a delivery batch with one load/save transaction.

        A successful email can contain many items.  Recording the whole batch
        at once narrows the post-delivery crash window and avoids repeatedly
        replacing the state file for one message.
        """
        if any(not fingerprint for fingerprint in fingerprints_to_mark):
            raise ValueError("fingerprint must not be blank")
        state = self.load()
        fingerprints = self._fingerprints(state, kind)
        changed = False
        for fingerprint in dict.fromkeys(fingerprints_to_mark):
            if fingerprint not in fingerprints:
                fingerprints.append(fingerprint)
                changed = True
        if changed:
            del fingerprints[:-limit]
            self.save(state)
        return state

    @staticmethod
    def _fingerprints(state: RunState, kind: str) -> list[str]:
        if kind == "article":
            return state.sent_article_fingerprints
        if kind == "event":
            return state.sent_event_fingerprints
        raise ValueError("kind must be article or event")


class _GcsBlob(Protocol):
    generation: int | str | None

    def exists(self) -> bool: ...
    def download_as_text(self, *, encoding: str) -> str: ...
    def upload_from_string(self, data: str, *, content_type: str, if_generation_match: int) -> None: ...


class _GcsBucket(Protocol):
    def blob(self, blob_name: str) -> _GcsBlob: ...


class _GcsClient(Protocol):
    def bucket(self, bucket_name: str) -> _GcsBucket: ...


class GcsJsonStateStore(JsonStateStore):
    """GCS-backed state with generation-precondition writes.

    It deliberately does not use a Cloud Storage FUSE mount: state updates rely
    on object generations, so concurrent job executions fail closed instead of
    silently overwriting fingerprints or delivery checkpoints.
    """

    def __init__(
        self,
        bucket_name: str,
        object_name: str = "newsbot_state.json",
        *,
        client: _GcsClient | None = None,
        precondition_exception: type[Exception] | None = None,
    ) -> None:
        if not bucket_name.strip():
            raise ValueError("STATE_GCS_BUCKET must not be blank")
        if not object_name.strip():
            raise ValueError("STATE_GCS_OBJECT must not be blank")
        # Retained only for compatibility with the non-critical artifact path.
        self.state_dir = Path(os.getenv("STATE_DIR", ".state"))
        self.path = self.state_dir / "newsbot_state.json"
        self.bucket_name = bucket_name.strip()
        self.object_name = object_name.strip()
        self._client = client
        self._generation = 0
        self._precondition_exception = precondition_exception

    def _storage_client(self) -> _GcsClient:
        if self._client is None:
            try:
                from google.cloud import storage
            except ImportError as exc:  # pragma: no cover - exercised in deployment packaging
                raise RuntimeError("STATE_BACKEND=gcs requires google-cloud-storage") from exc
            self._client = storage.Client()
        return self._client

    def _blob(self) -> _GcsBlob:
        return self._storage_client().bucket(self.bucket_name).blob(self.object_name)

    def _precondition_error_type(self) -> type[Exception]:
        if self._precondition_exception is not None:
            return self._precondition_exception
        try:
            from google.api_core.exceptions import PreconditionFailed
        except ImportError as exc:  # pragma: no cover - exercised in deployment packaging
            raise RuntimeError("STATE_BACKEND=gcs requires google-cloud-storage") from exc
        return PreconditionFailed

    @staticmethod
    def _generation_from(blob: _GcsBlob) -> int:
        generation = blob.generation
        if generation is None:
            raise RuntimeError("GCS state object did not return a generation")
        try:
            return int(generation)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("GCS state object returned an invalid generation") from exc

    @staticmethod
    def _validated_state(payload: object) -> RunState:
        if not isinstance(payload, dict):
            raise TypeError("state must be a JSON object")
        state = RunState(**payload)
        fingerprints = (state.sent_article_fingerprints, state.sent_event_fingerprints)
        timestamps = (state.last_successful_run, state.last_successful_delivery_run, state.last_shadow_run)
        if any(not isinstance(values, list) or any(not isinstance(value, str) for value in values) for values in fingerprints):
            raise TypeError("fingerprints must be string lists")
        if any(value is not None and not isinstance(value, str) for value in timestamps):
            raise TypeError("checkpoints must be strings or null")
        if not isinstance(state.run_ledger, list) or any(not isinstance(entry, dict) for entry in state.run_ledger):
            raise TypeError("run ledger must be a list of objects")
        return state

    def load(self) -> RunState:
        blob = self._blob()
        if not blob.exists():
            self._generation = 0
            return RunState()
        try:
            state = self._validated_state(json.loads(blob.download_as_text(encoding="utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError(f"GCS state object is invalid: gs://{self.bucket_name}/{self.object_name}") from exc
        self._generation = self._generation_from(blob)
        return state

    def save(self, state: RunState) -> None:
        blob = self._blob()
        payload = json.dumps(state.__dict__, ensure_ascii=False, indent=2)
        try:
            blob.upload_from_string(
                payload,
                content_type="application/json; charset=utf-8",
                if_generation_match=self._generation,
            )
        except self._precondition_error_type() as exc:
            raise RuntimeError("GCS state changed concurrently; refusing to overwrite newer state") from exc
        self._generation = self._generation_from(blob)


def state_store_from_environment() -> JsonStateStore:
    """Select the Railway-compatible default or an explicitly configured GCS store."""
    backend = os.getenv("STATE_BACKEND", "filesystem").strip().casefold() or "filesystem"
    if backend == "filesystem":
        return JsonStateStore(os.getenv("STATE_DIR", ".state"))
    if backend == "gcs":
        bucket = os.getenv("STATE_GCS_BUCKET", "").strip()
        if not bucket:
            raise RuntimeError("STATE_BACKEND=gcs requires STATE_GCS_BUCKET")
        object_name = os.getenv("STATE_GCS_OBJECT", "").strip() or "newsbot_state.json"
        return GcsJsonStateStore(bucket, object_name)
    raise RuntimeError("STATE_BACKEND must be filesystem or gcs")

"""Small durable JSON state store for local folders or a mounted Railway Volume."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any


@dataclass
class RunState:
    sent_article_fingerprints: list[str] = field(default_factory=list)
    sent_event_fingerprints: list[str] = field(default_factory=list)
    last_successful_run: str | None = None
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

    def record_run(self, *, mode: str, status: str, details: dict[str, Any] | None = None) -> RunState:
        state = self.load()
        timestamp = datetime.now(UTC).isoformat()
        state.run_ledger = [*state.run_ledger[-49:], {"timestamp": timestamp, "mode": mode, "status": status, **(details or {})}]
        if status == "success":
            state.last_successful_run = timestamp
        self.save(state)
        return state

    def was_sent(self, fingerprint: str, *, kind: str) -> bool:
        state = self.load()
        fingerprints = self._fingerprints(state, kind)
        return fingerprint in fingerprints

    def mark_sent(self, fingerprint: str, *, kind: str, limit: int = 5000) -> RunState:
        """Record a sent article/event only after successful delivery in a later step."""
        if not fingerprint:
            raise ValueError("fingerprint must not be blank")
        state = self.load()
        fingerprints = self._fingerprints(state, kind)
        if fingerprint not in fingerprints:
            fingerprints.append(fingerprint)
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

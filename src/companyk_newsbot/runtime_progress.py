"""Small atomic progress sidecar for kill-safe Full Shadow diagnostics."""
from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from threading import Lock
from time import monotonic


class RuntimeProgress:
    def __init__(self, path: Path) -> None:
        self.path, self.lock, self.started = path, Lock(), monotonic()
        self.payload: dict[str, object] = {"started_at": datetime.now(UTC).isoformat(), "status": "running",
            "last_stage": "initialized", "last_item_id": None,
            "stages": {stage: {"started": 0, "completed": 0, "failed": 0} for stage in ("identity", "grouping", "materiality", "grounding")}}
        self._write()

    def event(self, stage: str, outcome: str, item_id: str | None = None) -> None:
        with self.lock:
            stages = self.payload["stages"]
            if not isinstance(stages, dict) or stage not in stages or outcome not in {"started", "completed", "failed"}:
                return
            values = stages[stage]
            if isinstance(values, dict): values[outcome] = int(values[outcome]) + 1
            self.payload["last_stage"] = stage
            self.payload["last_item_id"] = item_id
            self._write()

    def finish(self, status: str) -> None:
        with self.lock:
            self.payload.update({"status": status, "ended_at": datetime.now(UTC).isoformat(),
                "elapsed_seconds": round(monotonic() - self.started, 3)})
            self._write()

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as handle:
            json.dump(self.payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            temporary = Path(handle.name)
        temporary.replace(self.path)

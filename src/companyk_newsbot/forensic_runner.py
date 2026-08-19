"""Run Full Shadow or downstream replay in a diagnostic child process."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from time import monotonic


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        temporary = Path(handle.name)
    temporary.replace(path)


def _latest_journal(artifact_dir: Path) -> dict[str, object] | None:
    paths = sorted((item for item in artifact_dir.glob("full_shadow_*.json") if not item.name.endswith(".runtime.json")), key=lambda item: item.stat().st_mtime)
    if not paths:
        return None
    try:
        return json.loads(paths[-1].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_json(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("full-shadow", "downstream-replay"))
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.artifact_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = args.artifact_dir / f"{args.mode}_{stamp}.stdout.log"
    stderr_path = args.artifact_dir / f"{args.mode}_{stamp}.stderr.log"
    diagnostic_path = args.artifact_dir / f"{args.mode}_{stamp}.process.json"
    environment = os.environ.copy()
    environment.update({"ROUTE_B_ENABLED": "false", "SHADOW_TEST_EMAIL": "false", "PRODUCTION_EMAIL_ENABLED": "false",
        # Full Shadow logs Korean article content; force a file-safe encoding
        # instead of inheriting a Windows console code page such as cp949.
        "PYTHONIOENCODING": "utf-8"})
    if args.mode == "full-shadow":
        environment.update({"RUN_MODE": "full_shadow", "ARTIFACT_DIR": str(args.artifact_dir)})
        command = [sys.executable, "-m", "companyk_newsbot.main"]
    else:
        if args.artifact is None or args.output is None:
            parser.error("downstream-replay requires --artifact and --output")
        command = [sys.executable, "-m", "companyk_newsbot.downstream_replay", "--artifact", str(args.artifact), "--output", str(args.output)]
    started_at = datetime.now(UTC)
    started = monotonic()
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        child = subprocess.Popen(command, stdout=stdout, stderr=stderr, env=environment)
        exit_code = child.wait()
    journal = _latest_journal(args.artifact_dir) if args.mode == "full-shadow" else None
    if args.mode == "full-shadow":
        progress_path = next(iter(sorted(args.artifact_dir.glob("full_shadow_*.runtime.json"), key=lambda item: item.stat().st_mtime, reverse=True)), None)
    else:
        progress_path = args.output.with_suffix(".runtime.json") if args.output else None
    progress = _read_json(progress_path)
    diagnostic = {"mode": args.mode, "started_at": started_at.isoformat(), "ended_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": round(monotonic() - started, 3), "process_exit_code": exit_code,
        "stdout_path": str(stdout_path), "stderr_path": str(stderr_path),
        "final_journal_stage": journal.get("last_completed_stage") if journal else None,
        "journal_status": journal.get("run_status") if journal else None,
        "runtime_progress_path": str(progress_path) if progress_path else None,
        "runtime_progress": progress}
    _atomic_json(diagnostic_path, diagnostic)
    print(json.dumps(diagnostic, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

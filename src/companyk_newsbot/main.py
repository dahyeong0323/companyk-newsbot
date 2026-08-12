"""Railway-compatible short-lived batch entry point."""

import json
import os
from datetime import date
from pathlib import Path

from .config import load_keyword_map
from .email import HtmlEmailRenderer, ResendEmailSender, ResendSettings
from .e2e import E2EExecutionError, run_real_e2e
from .state import JsonStateStore
from .replay import run_replay, write_replay_report


def main() -> int:
    load_keyword_map()
    mode = os.getenv("RUN_MODE", "shadow").lower()
    if os.getenv("E2E_TEST_TRIGGER", "").strip().lower() == "true":
        mode = "e2e_test"
    if mode not in {"local", "test", "shadow", "e2e_test", "full_shadow", "cascade_eval", "live"}:
        raise ValueError("RUN_MODE must be local, test, shadow, e2e_test, full_shadow, cascade_eval, or live")
    if mode == "live":
        raise RuntimeError("live mode is blocked until delivery is implemented and explicitly enabled")
    store = JsonStateStore(os.getenv("STATE_DIR", ".state"))
    if mode == "cascade_eval":
        source = os.getenv("CASCADE_REPLAY_ARTIFACT", "").strip()
        target = os.getenv("CASCADE_REPLAY_OUTPUT", "artifacts/luna_replay.json").strip()
        if not source:
            raise RuntimeError("CASCADE_REPLAY_ARTIFACT must point to a preserved Sol-only full-shadow JSON")
        report = __import__("asyncio").run(run_replay(Path(source)))
        write_replay_report(report, Path(target))
        print(json.dumps({"event": "cascade_replay_complete", "output_path": target, **report}, ensure_ascii=False, sort_keys=True))
        return 0
    if mode in {"e2e_test", "full_shadow"}:
        profile = "smoke" if mode == "e2e_test" else "full_shadow"
        try:
            result = run_real_e2e(load_keyword_map(), store, profile=profile, deliver=profile == "smoke")
        except E2EExecutionError as exc:
            store.record_run(mode=mode, status="failed", details={"stage": exc.stage, "message": str(exc)})
            raise
        phase = "real_e2e_smoke" if profile == "smoke" else "full_shadow_non_delivery"
        checkpoint = "shadow" if profile == "full_shadow" and result.status == "success" else None
        state = store.record_run(
            mode=mode,
            status=result.status,
            details={"phase": phase, **result.log_payload()},
            checkpoint=checkpoint,
        )
        if profile == "full_shadow":
            before = getattr(result, "production_delivery_checkpoint_before", None)
            after = state.last_successful_delivery_run
            print(
                json.dumps(
                    {
                        "event": "full_shadow_checkpoint_complete",
                        "email_sent": False,
                        "production_delivery_checkpoint_before": before,
                        "production_delivery_checkpoint_after": after,
                        "production_delivery_checkpoint_unchanged": before == after,
                        "shadow_checkpoint": state.last_shadow_run,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return 0 if result.status == "success" else 2
    if mode == "test":
        rendered = HtmlEmailRenderer().render([], report_date=date.today())
        sender = ResendEmailSender(ResendSettings.from_environment())
        try:
            delivery_id = sender.send(rendered)
        finally:
            sender.close()
        store.record_run(mode=mode, status="success", details={"phase": "delivery_test", "delivery_id": delivery_id})
        print("Test email accepted by Resend.")
        return 0
    store.record_run(mode=mode, status="success", details={"phase": "pre_delivery_validation"})
    print(f"Configuration valid. Pre-delivery {mode} run recorded; no email was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

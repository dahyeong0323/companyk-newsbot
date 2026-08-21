"""Railway-compatible short-lived batch entry point."""

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path

from .config import load_keyword_map
from .portfolio_registry import load_portfolio_registry
from .email import HtmlEmailRenderer, email_sender_from_settings, email_settings_from_environment
from .e2e import E2EExecutionError, run_real_e2e
from .state import state_store_from_environment
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def _enabled(name: str) -> bool:
    return os.getenv(name, "false").strip().casefold() in {"1", "true", "yes", "on"}


def _production_schedule_window_is_open(now: datetime | None = None) -> bool:
    """Block deployment-time live starts; Railway's scheduled start may drift a few minutes."""
    local = (now or datetime.now(UTC)).astimezone(KST)
    return (local.hour == 7 and local.minute >= 55) or (local.hour == 8 and local.minute <= 15)
from .replay import run_replay, write_replay_report


def main() -> int:
    mode = os.getenv("RUN_MODE", "shadow").lower()
    if os.getenv("E2E_TEST_TRIGGER", "").strip().lower() == "true":
        mode = "e2e_test"
    if mode not in {"local", "test", "shadow", "e2e_test", "full_shadow", "cascade_eval", "live"}:
        raise ValueError("RUN_MODE must be local, test, shadow, e2e_test, full_shadow, cascade_eval, or live")
    store = state_store_from_environment()
    if mode == "live":
        if not _enabled("PRODUCTION_EMAIL_ENABLED"):
            raise RuntimeError("live mode requires PRODUCTION_EMAIL_ENABLED=true")
        if _enabled("ROUTE_B_ENABLED"):
            raise RuntimeError("live mode requires ROUTE_B_ENABLED=false")
        if not _production_schedule_window_is_open():
            store.record_run(
                mode=mode,
                status="skipped",
                details={"phase": "production_schedule_guard", "timezone": "Asia/Seoul"},
            )
            print("Production schedule guard blocked a non-scheduled start; no email was sent.")
            return 0
        try:
            runtime_config = load_portfolio_registry(
                os.getenv("PORTFOLIO_REGISTRY_PATH", "config/portfolio_registry.yaml")
            )
            result = run_real_e2e(runtime_config, store, profile="production", deliver=True)
        except E2EExecutionError as exc:
            store.record_run(mode=mode, status="failed", details={"stage": exc.stage, "message": str(exc)})
            raise
        checkpoint = "delivery" if result.status == "success" and getattr(result, "delivery_id", None) else None
        store.record_run(
            mode=mode,
            status=result.status,
            details={"phase": "production_delivery", **result.log_payload()},
            checkpoint=checkpoint,
        )
        return 0 if result.status == "success" else 2
    if mode == "cascade_eval":
        # The replay command is the only default-off Route B operation that may
        # intentionally validate/load legacy classifier configuration.
        load_keyword_map()
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
        shadow_test_email = (
            profile == "full_shadow"
            and os.getenv("SHADOW_TEST_EMAIL", "false").strip().casefold() in {"1", "true", "yes", "on"}
        )
        try:
            route_b_enabled = os.getenv("ROUTE_B_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"}
            runtime_config = (
                load_keyword_map()
                if route_b_enabled
                else load_portfolio_registry(os.getenv("PORTFOLIO_REGISTRY_PATH", "config/portfolio_registry.yaml"))
            )
            result = run_real_e2e(
                runtime_config, store, profile=profile,
                deliver=profile == "smoke" or shadow_test_email,
            )
        except E2EExecutionError as exc:
            store.record_run(mode=mode, status="failed", details={"stage": exc.stage, "message": str(exc)})
            raise
        phase = (
            "full_shadow_inconclusive"
            if profile == "full_shadow" and result.status == "inconclusive"
            else "real_e2e_smoke"
            if profile == "smoke"
            else "full_shadow_test_delivery"
            if shadow_test_email
            else "full_shadow_non_delivery"
        )
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
                        "production_email_sent": False,
                        "shadow_test_email_sent": getattr(result, "delivery_id", None) is not None,
                        "shadow_test_delivery_id": getattr(result, "delivery_id", None),
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
        load_portfolio_registry(os.getenv("PORTFOLIO_REGISTRY_PATH", "config/portfolio_registry.yaml"))
        rendered = HtmlEmailRenderer().render([], report_date=date.today())
        sender = email_sender_from_settings(email_settings_from_environment())
        try:
            delivery_id = sender.send(rendered)
        finally:
            sender.close()
        store.record_run(mode=mode, status="success", details={"phase": "delivery_test", "delivery_id": delivery_id})
        print("Test email accepted by the configured provider.")
        return 0
    if os.getenv("ROUTE_B_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"}:
        load_keyword_map()
    else:
        load_portfolio_registry(os.getenv("PORTFOLIO_REGISTRY_PATH", "config/portfolio_registry.yaml"))
    store.record_run(mode=mode, status="success", details={"phase": "pre_delivery_validation"})
    print(f"Configuration valid. Pre-delivery {mode} run recorded; no email was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

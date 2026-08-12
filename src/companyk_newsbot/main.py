"""Railway-compatible short-lived batch entry point."""

import os
from datetime import date

from .config import load_keyword_map
from .email import HtmlEmailRenderer, ResendEmailSender, ResendSettings
from .state import JsonStateStore


def main() -> int:
    load_keyword_map()
    mode = os.getenv("RUN_MODE", "shadow").lower()
    if mode not in {"local", "test", "shadow", "live"}:
        raise ValueError("RUN_MODE must be local, test, shadow, or live")
    if mode == "live":
        raise RuntimeError("live mode is blocked until delivery is implemented and explicitly enabled")
    store = JsonStateStore(os.getenv("STATE_DIR", ".state"))
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

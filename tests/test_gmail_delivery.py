from __future__ import annotations

from base64 import urlsafe_b64decode
from email import policy
from email.parser import BytesParser

import httplib2
import pytest
from googleapiclient.errors import HttpError

from companyk_newsbot import main
from companyk_newsbot.e2e import E2EExecutionError
from companyk_newsbot.email import (
    DeliveryError,
    GmailEmailSender,
    GmailSettings,
    RenderedEmail,
    ResendEmailSender,
    email_delivery_stage,
    email_sender_from_settings,
    email_settings_from_environment,
)
from companyk_newsbot.state import JsonStateStore


def _gmail_environment(monkeypatch) -> None:
    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("GMAIL_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh-token")
    monkeypatch.setenv("NEWSBOT_RECIPIENT", "first@example.com, second@example.com, first@example.com")
    monkeypatch.setenv("EMAIL_FROM", "Company K Newsbot <ckpnewsbot@gmail.com>")


def test_provider_selection_preserves_resend_by_default(monkeypatch) -> None:
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("NEWSBOT_RECIPIENT", "first@example.com,second@example.com")
    monkeypatch.setenv("EMAIL_FROM", "Bot <onboarding@resend.dev>")

    settings = email_settings_from_environment()

    assert isinstance(email_sender_from_settings(settings), ResendEmailSender)


def test_provider_selection_uses_gmail_only_when_requested(monkeypatch) -> None:
    _gmail_environment(monkeypatch)

    settings = email_settings_from_environment()

    assert isinstance(settings, GmailSettings)
    assert settings.recipients == ("first@example.com", "second@example.com")
    assert email_delivery_stage(settings) == "gmail_delivery"


def test_unknown_provider_fails_fast(monkeypatch) -> None:
    monkeypatch.setenv("EMAIL_PROVIDER", "carrier-pigeon")

    with pytest.raises(DeliveryError, match="EMAIL_PROVIDER"):
        email_settings_from_environment()


def test_resend_diagnostic_stage_is_preserved(monkeypatch) -> None:
    monkeypatch.delenv("EMAIL_PROVIDER", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("NEWSBOT_RECIPIENT", "first@example.com")
    monkeypatch.setenv("EMAIL_FROM", "Bot <onboarding@resend.dev>")

    assert email_delivery_stage(email_settings_from_environment()) == "resend_delivery"


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_ID"),
        ("GMAIL_CLIENT_SECRET", "GMAIL_CLIENT_SECRET"),
        ("GMAIL_REFRESH_TOKEN", "GMAIL_REFRESH_TOKEN"),
    ],
)
def test_gmail_requires_each_runtime_credential(monkeypatch, missing: str, message: str) -> None:
    _gmail_environment(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(DeliveryError, match=message):
        email_settings_from_environment()


class _FakeRequest:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self._response = response

    def execute(self):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeMessages:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeRequest(self.response)


class _FakeService:
    def __init__(self, response: dict[str, object] | Exception) -> None:
        self.messages_api = _FakeMessages(response)

    def users(self):
        return self

    def messages(self):
        return self.messages_api


def test_gmail_sender_preserves_rendered_html_and_uses_one_multi_recipient_call(monkeypatch) -> None:
    _gmail_environment(monkeypatch)
    settings = GmailSettings.from_environment()
    service = _FakeService({"id": "gmail-message-123"})
    sender = GmailEmailSender(settings, service=service)
    html = '<p>한글 브리핑 <a href="https://example.com/news">기사 보기</a></p>'

    assert sender.send(RenderedEmail(subject="포트폴리오 데일리 뉴스", html=html)) == "gmail-message-123"

    assert len(service.messages_api.calls) == 1
    call = service.messages_api.calls[0]
    assert call["userId"] == "me"
    raw = call["body"]["raw"]
    assert isinstance(raw, str)
    message = BytesParser(policy=policy.default).parsebytes(urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    assert message["Subject"] == "포트폴리오 데일리 뉴스"
    assert message["From"] == "Company K Newsbot <ckpnewsbot@gmail.com>"
    assert message["To"] == "first@example.com, second@example.com"
    assert html in message.get_body(preferencelist=("html",)).get_content()


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_gmail_api_failures_never_return_delivery_id(monkeypatch, status: int) -> None:
    _gmail_environment(monkeypatch)
    response = httplib2.Response({"status": str(status)})
    sender = GmailEmailSender(GmailSettings.from_environment(), service=_FakeService(HttpError(response, b"error")))

    with pytest.raises(DeliveryError, match="Gmail request failed"):
        sender.send(RenderedEmail(subject="Test", html="<p>Test</p>"))


def test_gmail_network_failure_never_returns_delivery_id(monkeypatch) -> None:
    _gmail_environment(monkeypatch)
    sender = GmailEmailSender(GmailSettings.from_environment(), service=_FakeService(OSError("network unavailable")))

    with pytest.raises(DeliveryError, match="Gmail request failed"):
        sender.send(RenderedEmail(subject="Test", html="<p>Test</p>"))


def test_gmail_failure_error_never_exposes_runtime_secrets(monkeypatch) -> None:
    _gmail_environment(monkeypatch)
    sender = GmailEmailSender(GmailSettings.from_environment(), service=_FakeService(OSError("network unavailable")))

    with pytest.raises(DeliveryError) as raised:
        sender.send(RenderedEmail(subject="Test", html="<p>Test</p>"))

    assert "client-secret" not in str(raised.value)
    assert "refresh-token" not in str(raised.value)


def test_full_shadow_never_requires_gmail_credentials_or_enables_delivery(monkeypatch, tmp_path) -> None:
    observed: dict[str, object] = {}

    def fake_run(*_args, **kwargs):
        observed.update(kwargs)
        return type("Result", (), {"status": "success", "log_payload": lambda self: {"profile": "full_shadow"}})()

    monkeypatch.setenv("RUN_MODE", "full_shadow")
    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("PRODUCTION_EMAIL_ENABLED", "false")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("GMAIL_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GMAIL_REFRESH_TOKEN", raising=False)
    monkeypatch.setattr(main, "run_real_e2e", fake_run)

    assert main.main() == 0
    assert observed == {"profile": "full_shadow", "deliver": False}
    assert JsonStateStore(tmp_path).load().last_successful_delivery_run is None


def test_gmail_delivery_failure_never_advances_production_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUN_MODE", "live")
    monkeypatch.setenv("EMAIL_PROVIDER", "gmail")
    monkeypatch.setenv("PRODUCTION_EMAIL_ENABLED", "true")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_production_schedule_window_is_open", lambda: True)
    monkeypatch.setattr(
        main,
        "run_real_e2e",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(E2EExecutionError("gmail_delivery", "Gmail request failed")),
    )

    with pytest.raises(E2EExecutionError, match="gmail_delivery"):
        main.main()

    state = JsonStateStore(tmp_path).load()
    assert state.last_successful_delivery_run is None
    assert state.run_ledger[-1]["status"] == "failed"
    assert state.run_ledger[-1]["stage"] == "gmail_delivery"

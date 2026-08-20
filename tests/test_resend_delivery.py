from __future__ import annotations

import httpx
import json

from companyk_newsbot.email import RenderedEmail, ResendEmailSender, ResendSettings


def test_resend_sender_posts_html_and_returns_delivery_id() -> None:
    observed_payload = {}
    request = httpx.Request("POST", "https://api.resend.com/emails")
    def handler(value: httpx.Request) -> httpx.Response:
        observed_payload.update(json.loads(value.content))
        return httpx.Response(200, json={"id": "email-123"}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    sender = ResendEmailSender(ResendSettings(api_key="re_test", recipients=("jeremy.cheon@pm.me", "taejin3789@naver.com"), sender="Bot <onboarding@resend.dev>"), client=client)
    assert sender.send(RenderedEmail(subject="Test", html="<p>Test</p>")) == "email-123"
    assert observed_payload["to"] == ["jeremy.cheon@pm.me", "taejin3789@naver.com"]

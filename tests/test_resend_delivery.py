from __future__ import annotations

import httpx

from companyk_newsbot.email import RenderedEmail, ResendEmailSender, ResendSettings


def test_resend_sender_posts_html_and_returns_delivery_id() -> None:
    request = httpx.Request("POST", "https://api.resend.com/emails")
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"id": "email-123"}, request=request)))
    sender = ResendEmailSender(ResendSettings(api_key="re_test", recipient="jeremy.cheon@pm.me", sender="Bot <onboarding@resend.dev>"), client=client)
    assert sender.send(RenderedEmail(subject="Test", html="<p>Test</p>")) == "email-123"

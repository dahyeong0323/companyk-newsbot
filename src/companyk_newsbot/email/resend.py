"""Minimal Resend HTTP delivery client used by the Railway batch job."""

from __future__ import annotations

from dataclasses import dataclass
import os

import httpx

from .renderer import RenderedEmail


class DeliveryError(RuntimeError):
    """Raised when an email cannot be accepted by Resend."""


@dataclass(frozen=True)
class ResendSettings:
    api_key: str
    recipient: str
    sender: str

    @classmethod
    def from_environment(cls) -> "ResendSettings":
        api_key = os.getenv("RESEND_API_KEY", "").strip()
        recipient = os.getenv("NEWSBOT_RECIPIENT", "jeremy.cheon@pm.me").strip()
        sender = os.getenv("EMAIL_FROM", "Company K Newsbot <onboarding@resend.dev>").strip()
        if not api_key:
            raise DeliveryError("RESEND_API_KEY is required for delivery")
        if not recipient or "@" not in recipient:
            raise DeliveryError("NEWSBOT_RECIPIENT must be a valid email address")
        if not sender or "@" not in sender:
            raise DeliveryError("EMAIL_FROM must be a valid sender address")
        return cls(api_key=api_key, recipient=recipient, sender=sender)


class ResendEmailSender:
    endpoint = "https://api.resend.com/emails"

    def __init__(self, settings: ResendSettings, *, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self._client = client or httpx.Client(timeout=20.0)
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def send(self, email: RenderedEmail) -> str:
        try:
            response = self._client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.settings.api_key}"},
                json={"from": self.settings.sender, "to": [self.settings.recipient], "subject": email.subject, "html": email.html},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DeliveryError(f"Resend request failed: {exc}") from exc
        try:
            delivery_id = response.json().get("id")
        except ValueError as exc:
            raise DeliveryError("Resend returned an invalid response") from exc
        if not isinstance(delivery_id, str) or not delivery_id:
            raise DeliveryError("Resend did not return a delivery id")
        return delivery_id

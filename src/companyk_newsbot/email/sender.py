"""Provider selection for the newsbot's delivery boundary."""

from __future__ import annotations

import os
from typing import Protocol

from .gmail import GmailEmailSender, GmailSettings
from .renderer import RenderedEmail
from .resend import DeliveryError, ResendEmailSender, ResendSettings


class EmailDeliverySettings(Protocol):
    """The recipient boundary shared by every delivery provider."""

    recipients: tuple[str, ...]
    sender: str


class EmailSender(Protocol):
    """Minimal transport contract; rendering stays outside this boundary."""

    def send(self, email: RenderedEmail) -> str: ...

    def close(self) -> None: ...


def email_settings_from_environment() -> ResendSettings | GmailSettings:
    """Load only the credentials required by the explicitly selected provider."""
    provider = os.getenv("EMAIL_PROVIDER", "resend").strip().casefold()
    if provider == "resend":
        return ResendSettings.from_environment()
    if provider == "gmail":
        return GmailSettings.from_environment()
    raise DeliveryError("EMAIL_PROVIDER must be either 'resend' or 'gmail'")


def email_sender_from_settings(settings: ResendSettings | GmailSettings) -> EmailSender:
    if isinstance(settings, ResendSettings):
        return ResendEmailSender(settings)
    if isinstance(settings, GmailSettings):
        return GmailEmailSender(settings)
    raise DeliveryError("unsupported email delivery settings")


def email_delivery_stage(settings: ResendSettings | GmailSettings) -> str:
    """Keep existing Resend diagnostics stable while naming Gmail failures precisely."""
    if isinstance(settings, ResendSettings):
        return "resend_delivery"
    if isinstance(settings, GmailSettings):
        return "gmail_delivery"
    raise DeliveryError("unsupported email delivery settings")

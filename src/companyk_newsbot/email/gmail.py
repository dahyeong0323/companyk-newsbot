"""Gmail API delivery adapter for unattended OAuth refresh-token use."""

from __future__ import annotations

from base64 import urlsafe_b64encode
from dataclasses import dataclass
from email.message import EmailMessage
import os
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .renderer import RenderedEmail
from .resend import DeliveryError


GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
_TOKEN_URI = "https://oauth2.googleapis.com/token"


@dataclass(frozen=True)
class GmailSettings:
    client_id: str
    client_secret: str
    refresh_token: str
    recipients: tuple[str, ...]
    sender: str

    @classmethod
    def from_environment(cls) -> "GmailSettings":
        client_id = os.getenv("GMAIL_CLIENT_ID", "").strip()
        client_secret = os.getenv("GMAIL_CLIENT_SECRET", "").strip()
        refresh_token = os.getenv("GMAIL_REFRESH_TOKEN", "").strip()
        raw_recipients = os.getenv("NEWSBOT_RECIPIENT", "jeremy.cheon@pm.me")
        recipients = tuple(dict.fromkeys(value.strip() for value in raw_recipients.split(",") if value.strip()))
        sender = os.getenv("EMAIL_FROM", "Company K Newsbot <ckpnewsbot@gmail.com>").strip()
        if not client_id:
            raise DeliveryError("GMAIL_CLIENT_ID is required for Gmail delivery")
        if not client_secret:
            raise DeliveryError("GMAIL_CLIENT_SECRET is required for Gmail delivery")
        if not refresh_token:
            raise DeliveryError("GMAIL_REFRESH_TOKEN is required for Gmail delivery")
        if not recipients or any("@" not in recipient for recipient in recipients):
            raise DeliveryError("NEWSBOT_RECIPIENT must be a comma-separated list of valid email addresses")
        if not sender or "@" not in sender:
            raise DeliveryError("EMAIL_FROM must be a valid sender address")
        return cls(client_id, client_secret, refresh_token, recipients, sender)


class GmailEmailSender:
    """Send one rendered briefing to its complete recipient list in one API call."""

    def __init__(self, settings: GmailSettings, *, service: Any | None = None) -> None:
        self.settings = settings
        self._service = service or build(
            "gmail",
            "v1",
            credentials=Credentials(
                token=None,
                refresh_token=settings.refresh_token,
                token_uri=_TOKEN_URI,
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                scopes=[GMAIL_SEND_SCOPE],
            ),
            cache_discovery=False,
        )

    def close(self) -> None:
        """Gmail's generated client owns no explicit closeable resource here."""

    def send(self, email: RenderedEmail) -> str:
        message = EmailMessage()
        message["From"] = self.settings.sender
        message["To"] = ", ".join(self.settings.recipients)
        message["Subject"] = email.subject
        message.set_content("Company K Newsbot briefing is available in HTML format.")
        message.add_alternative(email.html, subtype="html")
        raw = urlsafe_b64encode(message.as_bytes()).decode("ascii").rstrip("=")
        try:
            response = self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        except Exception as exc:
            raise DeliveryError("Gmail request failed") from exc
        delivery_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(delivery_id, str) or not delivery_id:
            raise DeliveryError("Gmail did not return a message id")
        return delivery_id

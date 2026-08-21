"""Email rendering and explicit delivery adapters."""

from .gmail import GMAIL_SEND_SCOPE, GmailEmailSender, GmailSettings
from .renderer import EmailNewsItem, HtmlEmailRenderer, RenderedEmail
from .resend import DeliveryError, ResendEmailSender, ResendSettings
from .sender import EmailDeliverySettings, EmailSender, email_delivery_stage, email_sender_from_settings, email_settings_from_environment

__all__ = [
    "DeliveryError", "EmailDeliverySettings", "EmailNewsItem", "EmailSender", "GMAIL_SEND_SCOPE",
    "GmailEmailSender", "GmailSettings", "HtmlEmailRenderer", "RenderedEmail", "ResendEmailSender",
    "ResendSettings", "email_delivery_stage", "email_sender_from_settings", "email_settings_from_environment",
]

"""Email rendering and explicit delivery adapters."""

from .renderer import EmailNewsItem, HtmlEmailRenderer, RenderedEmail
from .resend import DeliveryError, ResendEmailSender, ResendSettings

__all__ = ["DeliveryError", "EmailNewsItem", "HtmlEmailRenderer", "RenderedEmail", "ResendEmailSender", "ResendSettings"]

__all__ = ["EmailNewsItem", "HtmlEmailRenderer", "RenderedEmail"]

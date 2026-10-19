"""Local QR generation for private subscription URLs."""
from __future__ import annotations

from urllib.parse import urlsplit


class SubscriptionQRError(ValueError):
    """Raised when a subscription URL is unsafe or QR generation fails."""


def validate_subscription_url(value: object) -> str:
    if not isinstance(value, str):
        raise SubscriptionQRError("Subscription URL is missing")
    url = value.strip()
    if not url or len(url) > 4096 or any(ord(ch) < 32 for ch in url):
        raise SubscriptionQRError("Subscription URL is invalid")
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            raise SubscriptionQRError("Subscription URL is not a supported web URL")
        _ = parts.port
    except ValueError:
        raise SubscriptionQRError("Subscription URL is malformed") from None
    return url


def make_subscription_qr_png(value: object) -> bytes:
    """Generate QR PNG locally; never send the secret URL to a QR web service."""
    url = validate_subscription_url(value)
    try:
        import io
        import segno
        qr = segno.make(url, error="m", micro=False)
        output = io.BytesIO()
        qr.save(output, kind="png", scale=8, border=4, dark="#111111", light="#ffffff")
        return output.getvalue()
    except Exception as exc:
        raise SubscriptionQRError("Could not generate subscription QR") from exc

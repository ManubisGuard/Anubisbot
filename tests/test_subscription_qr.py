from __future__ import annotations

import unittest
from unittest.mock import patch

from manubisguard.telegram.subscription_qr import (
    SubscriptionQRError, make_subscription_qr_png, validate_subscription_url,
)


class SubscriptionQRValidationTests(unittest.TestCase):
    def test_accepts_https_subscription_url_without_userinfo(self):
        self.assertEqual(validate_subscription_url("https://sub.example/token/abc"), "https://sub.example/token/abc")

    def test_rejects_non_web_url_and_userinfo(self):
        for value in ("file:///etc/passwd", "javascript:alert(1)", "https://user:pass@example.com/sub", "", None):
            with self.subTest(value=value), self.assertRaises(SubscriptionQRError):
                validate_subscription_url(value)

    def test_rejects_control_characters_and_overlong_url(self):
        with self.assertRaises(SubscriptionQRError):
            validate_subscription_url("https://example.com/a\nsecret")
        with self.assertRaises(SubscriptionQRError):
            validate_subscription_url("https://example.com/" + "x" * 4100)

    def test_qr_generation_failure_does_not_echo_secret(self):
        with patch.dict("sys.modules", {"segno": None}):
            with self.assertRaises(SubscriptionQRError) as ctx:
                make_subscription_qr_png("https://sub.example/private-token")
        self.assertNotIn("private-token", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

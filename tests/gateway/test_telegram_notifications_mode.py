"""Tests for Telegram push-notification mode normalization."""

import pytest

from gateway.run import GatewayRunner


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", None),
        (False, "important"),
        ("false", "important"),
        ("off", "important"),
        ("silent", "important"),
        ("important", "important"),
        (True, "all"),
        ("true", "all"),
        ("on", "all"),
        ("all", "all"),
        ("banana", "banana"),
    ],
)
def test_normalize_telegram_notifications_mode(raw, expected):
    assert GatewayRunner._normalize_telegram_notifications_mode(raw) == expected

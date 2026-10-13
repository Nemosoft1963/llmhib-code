"""Regression tests for backend credential availability checks."""

import pytest

from src.backend_defaults import API_KEY_ENV, is_backend_available


@pytest.mark.parametrize("value", ["", "   ", "\u3000"])
def test_backend_is_unavailable_for_empty_or_whitespace_only_api_key(monkeypatch, value):
    """Whitespace cannot authenticate a documented API-key backend."""
    monkeypatch.setenv(API_KEY_ENV["claude"], value)

    assert is_backend_available("claude") is False

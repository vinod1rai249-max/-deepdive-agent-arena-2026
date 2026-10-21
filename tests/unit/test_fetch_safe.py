import pytest

from core.errors import GuardrailViolation
from retrieval.fetch_safe import assert_safe_url, is_safe_url


def test_rejects_non_http_scheme():
    assert is_safe_url("ftp://example.com/file") is False


def test_rejects_loopback_ip():
    assert is_safe_url("http://127.0.0.1/admin") is False


def test_rejects_link_local_metadata_ip():
    assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False


def test_rejects_private_ip():
    assert is_safe_url("http://10.0.0.5/") is False


def test_raises_guardrail_violation_with_message():
    with pytest.raises(GuardrailViolation):
        assert_safe_url("http://127.0.0.1/")

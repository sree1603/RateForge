import pytest

from app.core.config import get_settings
from app.core.ssrf import validate_upstream_url


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://echo:9000", "http://echo:9000"),
        ("http://echo:9000/", "http://echo:9000"),
        ("https://api.example.com/v1/", "https://api.example.com/v1"),
        ("  https://api.example.com  ", "https://api.example.com"),
        ("http://10.0.0.5:8080/internal", "http://10.0.0.5:8080/internal"),
        ("http://localhost:3000", "http://localhost:3000"),
    ],
)
def test_accepts_normal_upstreams(raw, expected):
    assert validate_upstream_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "echo:9000",
        "ftp://example.com",
        "file:///etc/passwd",
        "gopher://example.com",
        "http://",
        "http://user:pass@example.com",
        "http://example.com@evil.com",
        "http://example.com/path?x=1",
        "http://example.com/path#frag",
        "http://example.com/../admin",
        "http://example.com/a/./b",
        "http://exa mple.com",
        "http://example.com\\@evil.com",
        "http://example.com:0",
        "http://example.com:99999",
        "http://-bad-.example.com",
    ],
)
def test_rejects_malformed_or_dangerous_urls(raw):
    with pytest.raises(ValueError):
        validate_upstream_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "http://169.254.169.254/latest/meta-data",  # cloud metadata service
        "http://169.254.0.1",
        "http://[fe80::1]/",
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped IPv6
        "http://0.0.0.0:8000",
        "http://0.1.2.3",
        "http://224.0.0.1",
        "http://2852039166/",  # 169.254.169.254 as one decimal number
        "http://0xa9fea9fe/",  # ...and as hex
        "http://0251.0376.0251.0376/",  # ...and as octal
        "http://169.254.43518/",  # ...and in mixed form
        "http://metadata.google.internal/",
        "http://METADATA.GOOGLE.INTERNAL./",
    ],
)
def test_blocks_metadata_and_other_unsafe_addresses(raw):
    with pytest.raises(ValueError):
        validate_upstream_url(raw)


def test_private_addresses_can_be_disabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "allow_private_upstreams", False)
    for raw in ("http://10.0.0.5", "http://192.168.1.1", "http://127.0.0.1:8000", "http://[::1]/"):
        with pytest.raises(ValueError):
            validate_upstream_url(raw)
    # public addresses and hostnames are still fine
    assert validate_upstream_url("http://8.8.8.8") == "http://8.8.8.8"
    assert validate_upstream_url("https://api.example.com") == "https://api.example.com"

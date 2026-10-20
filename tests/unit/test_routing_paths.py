import pytest

from app.services.routing import InvalidPath, build_upstream_url, validate_gateway_path


@pytest.mark.parametrize(
    "path",
    [
        "",
        "current",
        "a/b/c",
        "users/",
        "file.tar.gz",
        "v1/items/42",
        "caf\u00e9",
        "a b",
        "..hidden",
        "a..b",
    ],
)
def test_valid_paths_are_accepted(path):
    validate_gateway_path(path)


@pytest.mark.parametrize(
    "path",
    ["..", "../admin", "a/../b", "a/..", ".", "a/./b", "a//b", "//a", "a\\b", "a\x00b", "a\nb"],
)
def test_traversal_and_malformed_paths_are_rejected(path):
    with pytest.raises(InvalidPath):
        validate_gateway_path(path)


@pytest.mark.parametrize(
    "raw",
    [
        b"/gateway/echo/%2e%2e/admin",
        b"/gateway/echo/%2E%2E/admin",
        b"/gateway/echo/%2e/x",
        b"/gateway/echo/a%2fb",
        b"/gateway/echo/a%2Fb",
        b"/gateway/echo/a%5cb",
        b"/gateway/echo/a%00b",
    ],
)
def test_encoded_tricks_are_rejected_via_raw_path(raw):
    # The decoded path may look harmless; the raw bytes are what gives the trick away.
    with pytest.raises(InvalidPath):
        validate_gateway_path("harmless", raw)


def test_raw_path_without_tricks_passes():
    validate_gateway_path("a b", b"/gateway/echo/a%20b")


def test_build_url_simple_join():
    assert (
        build_upstream_url("http://echo:9000", "echo", True, "current")
        == "http://echo:9000/current"
    )
    assert build_upstream_url("http://echo:9000", "echo", True, "") == "http://echo:9000/"
    assert build_upstream_url("http://echo:9000", "echo", True, "a/b/") == "http://echo:9000/a/b/"


def test_build_url_respects_base_path():
    assert (
        build_upstream_url("https://api.example.com/v1", "w", True, "x")
        == "https://api.example.com/v1/x"
    )
    assert (
        build_upstream_url("https://api.example.com/v1", "w", True, "")
        == "https://api.example.com/v1"
    )


def test_build_url_without_stripping_prefix():
    assert (
        build_upstream_url("http://up:1", "weather", False, "current")
        == "http://up:1/weather/current"
    )
    assert build_upstream_url("http://up:1", "weather", False, "") == "http://up:1/weather"


def test_build_url_encodes_every_segment():
    url = build_upstream_url("http://up:1", "p", True, "a b/c?d=e/f#g/100%")
    assert url == "http://up:1/a%20b/c%3Fd=e/f%23g/100%25"
    assert "?" not in url and "#" not in url


def test_build_url_encodes_percent_so_there_is_no_double_decoding():
    # A decoded literal "%2e%2e" must reach the upstream as the text "%2e%2e", not as "..".
    assert build_upstream_url("http://up:1", "p", True, "%2e%2e/x") == "http://up:1/%252e%252e/x"


def test_build_url_encodes_unicode():
    assert build_upstream_url("http://up:1", "p", True, "caf\u00e9") == "http://up:1/caf%C3%A9"

from app.core.config import get_settings
from app.core.security import (
    generate_api_key,
    hash_api_key,
    parse_key_prefix,
    verify_admin_token,
)


def test_generated_key_has_expected_shape():
    key = generate_api_key()
    assert key.raw.startswith("rf_")
    assert key.raw.split("_", 2)[1] == key.prefix
    assert len(key.prefix) == 12
    assert len(key.key_hash) == 64


def test_keys_are_unique():
    assert len({generate_api_key().raw for _ in range(200)}) == 200


def test_hash_is_deterministic_and_not_the_key():
    key = generate_api_key()
    assert hash_api_key(key.raw) == key.key_hash
    assert key.raw not in key.key_hash


def test_changing_one_character_changes_the_hash():
    key = generate_api_key()
    tampered = key.raw[:-1] + ("a" if key.raw[-1] != "a" else "b")
    assert hash_api_key(tampered) != key.key_hash


def test_parse_prefix_accepts_generated_keys():
    key = generate_api_key()
    assert parse_key_prefix(key.raw) == key.prefix


def test_parse_prefix_handles_underscores_in_secret():
    assert parse_key_prefix("rf_0123456789ab_se_cr-et_x") == "0123456789ab"


def test_parse_prefix_rejects_malformed_keys():
    for bad in [
        "",
        "rf",
        "rf_",
        "rf_abc_secret",
        "xx_0123456789ab_s",
        "rf_0123456789ZZ_s",
        "plain",
    ]:
        assert parse_key_prefix(bad) is None, bad


def test_admin_token_check():
    assert verify_admin_token(get_settings().admin_token)
    assert not verify_admin_token("wrong")
    assert not verify_admin_token("")

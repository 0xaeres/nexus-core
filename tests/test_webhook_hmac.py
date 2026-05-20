import hashlib
import hmac

from nexus.api.routes.webhooks import verify_signature


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_correct_signature_accepted() -> None:
    secret = "topsecret"
    body = b'{"foo":"bar"}'
    assert verify_signature(secret, body, _sign(secret, body))


def test_wrong_secret_rejected() -> None:
    body = b'{"foo":"bar"}'
    assert not verify_signature("right", body, _sign("wrong", body))


def test_tampered_body_rejected() -> None:
    secret = "k"
    original = b'{"a":1}'
    tampered = b'{"a":2}'
    assert not verify_signature(secret, tampered, _sign(secret, original))


def test_missing_header_rejected_when_secret_set() -> None:
    assert not verify_signature("k", b"x", None)


def test_malformed_header_rejected() -> None:
    assert not verify_signature("k", b"x", "md5=deadbeef")


def test_empty_secret_disables_check_for_dev() -> None:
    assert verify_signature("", b"anything", None)
    assert verify_signature("", b"anything", "sha256=garbage")

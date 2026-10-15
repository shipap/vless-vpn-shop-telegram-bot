"""OIDC diagnostics expose controlled outcomes, never credentials or identity values."""

import json
import logging
import time
from types import SimpleNamespace

import fakeredis
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sunrise_core.errors import DomainError
from sunrise_infrastructure.security import digest
from sunrise_infrastructure.telegram_auth import OIDC_ISSUER, TelegramAuth, validate_oidc


@pytest.fixture
def signed_profile():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    moment = int(time.time())
    claims = {
        "iss": OIDC_ISSUER,
        "aud": "synthetic-client",
        "sub": "PRIVATE_SUBJECT_VALUE",
        "id": 70000000000000001,
        "iat": moment,
        "exp": moment + 300,
        "nonce": "PRIVATE_NONCE_VALUE",
        "preferred_username": "private_username",
    }
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    keys = {"keys": [jwk | {"kid": "PRIVATE_KEY_ID", "alg": "RS256", "use": "sig"}]}
    return key, claims, keys, moment


def diagnostics(caplog, event):
    return [
        json.loads(record.getMessage().removeprefix(event + " "))
        for record in caplog.records
        if record.name == "sunrise_infrastructure.telegram_auth"
        and record.getMessage().startswith(event + " ")
    ]


@pytest.mark.parametrize("invalid", ["nonce", "missing_id", "signature"])
def test_jwt_denial_logs_only_controlled_reason_and_boolean_metadata(caplog, signed_profile, invalid):
    key, claims, keys, moment = signed_profile
    expected_nonce = "PRIVATE_EXPECTED_NONCE" if invalid == "nonce" else claims["nonce"]
    if invalid == "missing_id":
        claims.pop("id")
    if invalid == "signature":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "PRIVATE_KEY_ID"})
    with caplog.at_level(logging.WARNING, logger="sunrise_infrastructure.telegram_auth"):
        with pytest.raises(DomainError) as error:
            validate_oidc(token, keys, "synthetic-client", expected_nonce, moment)
    assert error.value.code == "telegram_auth_invalid"
    rows = diagnostics(caplog, "telegram_oidc_validation_denied")
    assert len(rows) == 1
    row = rows[0]
    assert (
        row["reason"]
        == {"nonce": "claim_values", "missing_id": "missing_id", "signature": "signature"}[invalid]
    )
    assert row["alg_rs256"] is True and row["kid_string"] is True
    assert row["nonce_present"] is True and row["aud_string"] is True
    assert row["id_present"] is (invalid != "missing_id")
    assert row["id_in_range"] is (invalid != "missing_id")
    assert row["username_present"] is True
    assert row["username_string"] is True
    assert row["username_current_pattern_matches"] is True
    assert all(type(value) is bool for field, value in row.items() if field != "reason")
    for private in [token, "PRIVATE_", "private_username", "70000000000000001", "synthetic-client"]:
        assert private not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_callback_denial_logs_fixed_stage_and_preserves_original_exception_and_flow_consumption(caplog):
    failure = DomainError("PRIVATE_ERROR_CODE", "PRIVATE_EXCEPTION_MESSAGE", 503)

    class FailingTransport:
        def token(self, **kwargs):
            raise failure

    auth = TelegramAuth.__new__(TelegramAuth)
    auth.redis = fakeredis.FakeRedis()
    auth.settings = SimpleNamespace(
        telegram_login_client_id="PRIVATE_CLIENT_ID",
        telegram_login_client_secret="PRIVATE_CLIENT_SECRET",  # noqa: S106 - synthetic redaction marker
        telegram_login_redirect_uri="https://synthetic.invalid/callback",
    )
    auth.transport = FailingTransport()
    flow_key = "sunrise:oidc-flow:" + digest("PRIVATE_STATE")
    auth.redis.set(
        flow_key,
        json.dumps(
            {
                "browser_hash": digest("PRIVATE_BROWSER"),
                "expires_at": int(time.time()) + 600,
                "nonce": "PRIVATE_NONCE",
                "verifier": "PRIVATE_VERIFIER",
                "documents": [],
            }
        ),
    )
    with caplog.at_level(logging.WARNING, logger="sunrise_infrastructure.telegram_auth"):
        with pytest.raises(DomainError) as error:
            auth._callback("PRIVATE_CODE", "PRIVATE_STATE", "PRIVATE_BROWSER", "192.0.2.50")
    assert error.value is failure
    assert auth.redis.get(flow_key) is None
    assert diagnostics(caplog, "telegram_oidc_callback_denied") == [
        {"stage": "token_exchange", "reason": "application_denied"}
    ]
    assert "PRIVATE_" not in caplog.text and "192.0.2.50" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_successful_jwt_validation_adds_no_diagnostic_log(caplog, signed_profile):
    key, claims, keys, moment = signed_profile
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "PRIVATE_KEY_ID"})
    with caplog.at_level(logging.WARNING, logger="sunrise_infrastructure.telegram_auth"):
        identity = validate_oidc(token, keys, "synthetic-client", claims["nonce"], moment)
    assert identity.telegram_id == claims["id"]
    assert diagnostics(caplog, "telegram_oidc_validation_denied") == []


@pytest.mark.parametrize("profile_id", ["1", "70000000000000001", "9223372036854775807"])
def test_signed_oidc_canonical_decimal_id_is_lossless(signed_profile, profile_id):
    key, claims, keys, moment = signed_profile
    claims["id"] = profile_id
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "PRIVATE_KEY_ID"})
    identity = validate_oidc(token, keys, "synthetic-client", claims["nonce"], moment)
    assert type(identity.telegram_id) is int
    assert identity.telegram_id == int(profile_id)


@pytest.mark.parametrize(
    "profile_id",
    [True, False, 1.5, 0, -1, "0", "01", "+1", " 1", "1 ", "1.0", "١", "１", "9223372036854775808", "1" * 20],
)
def test_signed_oidc_noncanonical_ids_are_rejected(signed_profile, profile_id):
    key, claims, keys, moment = signed_profile
    claims["id"] = profile_id
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "PRIVATE_KEY_ID"})
    with pytest.raises(DomainError) as error:
        validate_oidc(token, keys, "synthetic-client", claims["nonce"], moment)
    assert error.value.code == "telegram_auth_invalid"


def test_decimal_id_diagnostics_remain_boolean_and_redacted(caplog, signed_profile):
    key, claims, keys, moment = signed_profile
    claims["id"] = "70000000000000001"
    token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "PRIVATE_KEY_ID"})
    with caplog.at_level(logging.WARNING, logger="sunrise_infrastructure.telegram_auth"):
        with pytest.raises(DomainError):
            validate_oidc(token, keys, "synthetic-client", "PRIVATE_WRONG_NONCE", moment)
    row = diagnostics(caplog, "telegram_oidc_validation_denied")[0]
    assert row["id_integer"] is False
    assert row["id_string"] is True and row["id_canonical_decimal"] is True
    assert all(type(value) is bool for field, value in row.items() if field != "reason")
    assert "70000000000000001" not in caplog.text and "PRIVATE_" not in caplog.text

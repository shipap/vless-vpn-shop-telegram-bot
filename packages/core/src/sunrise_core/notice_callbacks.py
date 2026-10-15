"""Signed links to review a saved device removal; transport still authorizes the actor."""

import hashlib
import hmac
import re
from uuid import UUID


def owner_notice_callback(operation, event_id, secret):
    if operation not in {"p", "w", "b"} or not isinstance(secret, str) or len(secret) < 32:
        raise ValueError("owner_notice_callback_invalid")
    payload = f"hw1:{operation}:{UUID(event_id).hex}"
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return payload + ":" + signature


def parse_owner_notice_callback(value, secret):
    if not isinstance(value, str) or not re.fullmatch(r"hw1:[pwb]:[a-f0-9]{32}:[a-f0-9]{16}", value):
        return None
    _, operation, identifier, _ = value.split(":")
    try:
        expected = owner_notice_callback(operation, identifier, secret)
    except (ValueError, TypeError, AttributeError):
        return None
    return (operation, str(UUID(identifier))) if hmac.compare_digest(value, expected) else None

"""Read-only membership proof for the isolated Remnawave callback relay."""

import hmac

from sqlalchemy import String, cast, literal, select
from sunrise_infrastructure.db import Account, Setting, Subscription
from sunrise_infrastructure.panel_v3 import registered_owner

from .webhook_router import remna_resource_digest


def verified_binding_in(session, subscription, panel, policy):
    record = session.get(Setting, "panel_test:" + subscription.id, populate_existing=True)
    value = record.value if record and isinstance(record.value, dict) else {}
    binding = value.get("binding")
    account = session.get(Account, subscription.account_id)
    return bool(
        isinstance(binding, dict)
        and binding.get("user_id") == str(subscription.panel_user_id)
        and registered_owner(session, value, subscription, account, panel)
        and value.get("scope_id", policy.scope.scope_id) == policy.scope.scope_id
    )


def admitted_resource_in(session, commerce, settings, kind, resource_digest, secret):
    """Match an opaque identifier only against signed, current test ownership."""
    policy, panel = commerce.runtime_policy, commerce.panel_provider
    if (
        not policy
        or not policy.scoped_payments
        or panel is None
        or getattr(panel, "contract", None) != "3.4_id"
        or not secret
        or kind not in {"user", "node"}
    ):
        raise ValueError("scoped_remna_probe_denied")
    policy.require_current(session)
    if kind == "node":
        allowed = (
            set(policy.scope.panel.node_uuids)
            & set(settings.panel_test_node_uuids)
            & set(panel.nodes)
        )
        result = any(
            hmac.compare_digest(resource_digest, remna_resource_digest(secret, "node", node))
            for node in allowed
        )
    else:
        candidates = list(
            session.scalars(
                select(Subscription)
                .join(
                    Setting,
                    Setting.key == literal("scoped_test_account:") + cast(Subscription.account_id, String),
                )
                .where(
                    Subscription.environment == "scoped_live",
                    Subscription.panel_user_id.is_not(None),
                    Setting.value["scope_id"].as_string() == policy.scope.scope_id,
                )
                .limit(1001)
            )
        )
        if len(candidates) > 1000:
            raise ValueError("scoped_remna_probe_capacity_denied")
        matches = [
            sub
            for sub in candidates
            if hmac.compare_digest(
                resource_digest, remna_resource_digest(secret, "user", str(sub.panel_user_id))
            )
        ]
        if len(matches) > 1:
            raise ValueError("scoped_remna_probe_ambiguous")
        if matches:
            policy.require_subscription_in(session, matches[0])
            if not verified_binding_in(session, matches[0], panel, policy):
                raise ValueError("scoped_remna_binding_denied")
        result = bool(matches)
    policy.require_current(session)
    return result

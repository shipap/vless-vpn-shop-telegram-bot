from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace

from redis.exceptions import RedisError
from sqlalchemy import String, cast, func, or_, select, text
from sunrise_core.errors import DomainError, missing
from sunrise_core.refunds import paid_entitlement_matches

from . import panel_entitlements
from .commerce import (
    account_dto,
    credit_dto,
    iso,
    product_dto,
    successful_order_history_clause,
    successful_payment_history_clause,
)
from .db import (
    Account,
    Agreement,
    Alias,
    Audit,
    Block,
    Challenge,
    CreditLedger,
    Device,
    Job,
    Ledger,
    Order,
    Payment,
    Product,
    PromoClaim,
    PromoUse,
    ProviderOperation,
    Quote,
    Refund,
    Segment,
    Session,
    Setting,
    Subscription,
    now,
    uid,
)


class Admin:
    def __init__(self, commerce, guard, accounts=None):
        self.commerce, self.db, self.guard = commerce, commerce.db, guard
        self.accounts = accounts or commerce.accounts

    def _bot_actor(self, session, actor):
        account = self.commerce._account(session, actor, True)
        if not self.accounts or not self.accounts.is_bot_admin(account):
            raise missing()
        return account

    def bot_promos(self, actor):
        with self.db.transaction() as session:
            self._bot_actor(session, actor)
            items = []
            aliases = dict(session.execute(select(Alias.old_id, Alias.canonical_id)).all())
            for promo in self.commerce._setting(session, "discounts", {}).get("promos", []):
                code = promo["code"]
                uses = session.execute(
                    select(PromoUse.account_id, PromoUse.state).where(PromoUse.code == code)
                ).all()
                claims = list(session.scalars(select(PromoClaim.account_id).where(PromoClaim.code == code)))
                activated = set(claims) | {account for account, _ in uses}
                activated.update(
                    session.scalars(
                        select(Audit.actor_id).where(Audit.action == "promo_activate", Audit.target == code)
                    )
                )
                activated.update(
                    session.scalars(
                        select(Account.id).where(Account.pending_promo["code"].as_string() == code)
                    )
                )
                activated = {
                    aliases.get(identifier, identifier) for identifier in activated if identifier is not None
                }
                redeemed = sum(state == "used" for _, state in uses) + len(claims)
                occupied = sum(state != "released" for _, state in uses) + len(claims)
                historical_usage_unknown = False
                if self.commerce.live:
                    from .promos import legacy_usage_in

                    legacy = legacy_usage_in(session, self.commerce, int(actor), code)
                    occupied = legacy.baseline + legacy.extra_global_count
                    registry = session.get(Setting, "legacy_promo_usage").value
                    source_ids = registry.get("codes", {}).get(code, {}).get("used_account_ids", [])
                    activated.update(
                        aliases.get(int(identifier), int(identifier)) for identifier in source_ids
                    )
                    # Imported global consumption is authoritative, even when identities
                    # of historical activators cannot be recovered individually.
                    historical_usage_unknown = legacy.baseline > 0
                limit = promo.get("total_limit")
                items.append(
                    dict(promo)
                    | {
                        "activated_users": len(activated),
                        "redeemed_count": redeemed,
                        "reserved_count": sum(state == "reserved" for _, state in uses),
                        "historical_usage_unknown": historical_usage_unknown,
                        "remaining_uses": max(0, limit - occupied) if limit is not None else None,
                    }
                )
            return items

    def delete_bot_promo(self, actor, code, reason):
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину изменения.", 400)
        with self.db.transaction() as session:
            account = self._bot_actor(session, actor)
            row = session.scalar(select(Setting).where(Setting.key == "discounts").with_for_update())
            old = row.value if row else {}
            promo = next((item for item in old.get("promos", []) if item["code"] == code), None)
            archive = session.get(Setting, "promo_archive")
            archived = archive.value if archive else {}
            if promo is None:
                if code in archived:
                    return {"code": code, "deleted": True}
                raise missing()
            value = old | {"promos": [item for item in old["promos"] if item["code"] != code]}
            entry = {"terms": promo, "deleted_at": iso(now()), "actor_id": str(account.id)}
            if archive:
                archive.value = archived | {code: entry}
            else:
                session.add(Setting(key="promo_archive", value={code: entry}))
            row.value = value
            self.audit(session, account.id, "bot_promo_delete", code, reason, old, value)
            return {"code": code, "deleted": True}

    def save_bot_promo(self, actor, values, reason):
        from .promos import validate_promo

        promo = validate_promo(values)
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину изменения.", 400)
        with self.db.transaction() as session:
            account = self._bot_actor(session, actor)
            if promo["reward_type"] == "free_days":
                product = session.get(Product, promo["product_id"])
                if not product or not product.available:
                    raise DomainError("promo_product_unavailable", "Выберите доступный тариф подарка.", 400)
            row = session.scalar(select(Setting).where(Setting.key == "discounts").with_for_update())
            old = row.value if row else {}
            archived = session.get(Setting, "promo_archive")
            if archived and promo["code"] in archived.value:
                raise DomainError("promo_archived", "Удалённый код нельзя использовать повторно.", 409)
            promos = old.get("promos", [])
            updated = [existing for existing in promos if existing["code"] != promo["code"]] + [promo]
            if len(updated) > 100:
                raise DomainError("promo_limit", "Достигнут лимит промокодов.", 400)
            value = old | {"promos": updated}
            if row:
                row.value = value
            else:
                session.add(Setting(key="discounts", value=value))
            self.audit(session, account.id, "bot_promo_save", promo["code"], reason, old, value)
            return promo

    def _save_setting(self, actor, key, value, reason):
        if key in {
            "production_admission",
            "production_runtime_gates",
            "scoped_test_admission",
            "promo_archive",
        } or key.startswith(
            ("legacy_operation", "import_retention:", "legacy_promo_usage", "scoped_test_account:")
        ):
            raise DomainError(
                "deployment_settings_denied",
                "Настройки допуска окружения изменяет оператор развёртывания.",
                403,
            )
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину изменения.", 400)
        with self.db.transaction() as s:
            s.execute(select(func.pg_advisory_xact_lock(617045503)))
            row = (
                s.scalar(select(Setting).where(Setting.key == key).with_for_update())
                if key == "public_content"
                else s.get(Setting, key)
            )
            before = row.value if row else {}
            if key == "public_content":
                from .catalog import validate_public_content

                if not isinstance(before, dict) or not isinstance(value, dict):
                    raise DomainError("content_invalid", "Проверьте структуру контента.", 400)
                value = validate_public_content(before | value)
            if key == "payment_methods":
                from .commerce import validate_payment_methods

                value = validate_payment_methods(value) | {
                    "version": (before.get("version", 0) if isinstance(before, dict) else 0) + 1
                }
            if row:
                row.value = value
            else:
                s.add(Setting(key=key, value=value))
            self.audit(s, actor, key + "_update", key, reason, before, value)
            return value

    def public_content(self):
        with self.db.transaction() as s:
            return self.commerce._setting(s, "public_content", {"faq": [], "how_to_connect": []})

    def panel_event_routes(self, actor):
        from .panel_event_routes import policy_in, registration_context
        from .staff_permissions import StaffPermissions

        settings = self.accounts.settings if self.accounts else self.commerce.public_settings
        panel = self.commerce.panel_provider
        with self.db.transaction() as session:
            StaffPermissions(self.accounts).require_in(session, actor, "settings_write", bot=True)
            context = registration_context(panel, settings)
            policy = policy_in(session, panel, settings)
            return {
                "configured": policy is not None,
                "policy": policy.model_dump() if policy else None,
                "registration_context": context,
                "approved_node_uuids": sorted(
                    (
                        set(self.commerce.runtime_policy.scope.panel.node_uuids)
                        if self.commerce.live
                        else set(getattr(settings, "panel_test_node_uuids", ()))
                    )
                    & panel.nodes
                )
                if context
                else [],
                "approved_product_ids": sorted(session.scalars(select(Product.id).where(Product.available)))
                if context
                else [],
            }

    def save_panel_event_routes(self, actor, value, reason):
        from pydantic import ValidationError

        from .panel_event_routes import PanelEventRoutes, validate_scope
        from .staff_permissions import StaffPermissions

        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину изменения.", 400)
        try:
            policy = PanelEventRoutes.model_validate(value)
        except ValidationError:
            raise DomainError(
                "panel_event_routes_invalid", "Проверьте явные связи серверов и тарифов.", 400
            ) from None
        settings = self.accounts.settings if self.accounts else self.commerce.public_settings
        with self.db.transaction() as session:
            StaffPermissions(self.accounts).require_in(session, actor, "settings_write", bot=True)
            session.execute(select(func.pg_advisory_xact_lock(617045503)))
            validate_scope(session, self.commerce.panel_provider, settings, policy)
            row = session.scalar(select(Setting).where(Setting.key == "panel_event_routes").with_for_update())
            before = row.value if row else {}
            value = policy.model_dump()
            if row:
                row.value = value
            else:
                session.add(Setting(key="panel_event_routes", value=value))
            self.audit(
                session, actor, "panel_event_routes_update", "panel_event_routes", reason, before, value
            )
            return value

    def panel_mapping(self):
        with self.db.transaction() as s:
            return self.commerce._setting(
                s, "panel_mapping", {"external_squad_uuid": None, "project_prefix": ""}
            )

    def update_panel_mapping(self, actor, external_squad_uuid, project_prefix, reason):
        import re
        from uuid import UUID

        try:
            if external_squad_uuid is not None:
                external_squad_uuid = str(UUID(external_squad_uuid))
            if not isinstance(project_prefix, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]{1,23}", project_prefix
            ):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise DomainError(
                "panel_mapping_invalid",
                "Проверьте UUID внешней группы и префикс: 2–24 латинских символа, цифры, подчёркивание.",
                400,
            ) from None
        return self._save_setting(
            actor,
            "panel_mapping",
            {"external_squad_uuid": external_squad_uuid, "project_prefix": project_prefix},
            reason,
        )

    def update_public_content(self, actor, value, reason):
        return self._save_setting(actor, "public_content", value, reason)

    def telegram_theme(self):
        with self.db.transaction() as s:
            return self.commerce._setting(s, "telegram_theme", {"icons": {}, "styles": {}})

    def update_telegram_theme(self, actor, value, reason):
        from .catalog import validate_presentation

        if (
            not isinstance(value, dict)
            or set(value) != {"icons", "styles"}
            or not isinstance(value["icons"], dict)
            or not isinstance(value["styles"], dict)
            or len(value["icons"]) > 200
        ):
            raise DomainError("theme_invalid", "Проверьте значки и семантические стили.", 400)
        for key, icon in value["icons"].items():
            if not isinstance(key, str) or not 1 <= len(key) <= 64:
                raise DomainError("theme_invalid", "Проверьте название значка.", 400)
            validate_presentation({"icon": icon})
        if set(value["styles"]) - {"primary", "success", "danger", "neutral", "admin"} or any(
            v not in {"primary", "success", "danger", "neutral"} for v in value["styles"].values()
        ):
            raise DomainError("theme_invalid", "Проверьте семантические стили.", 400)
        return self._save_setting(actor, "telegram_theme", value, reason)

    def refund_policy(self):
        with self.db.transaction() as s:
            return self.commerce._setting(
                s, "refund_policy", {"max_age_hours": 24, "max_traffic_bytes": 100_000_000}
            )

    def update_refund_policy(self, actor, max_age_hours, max_traffic_bytes, reason):
        if (
            type(max_age_hours) is not int
            or type(max_traffic_bytes) is not int
            or not 0 <= max_age_hours <= 24
            or not 0 <= max_traffic_bytes <= 100_000_000
        ):
            raise DomainError(
                "refund_policy_invalid", "Пороги можно только ужесточить: не более 24 часов и 100 МБ.", 400
            )
        return self._save_setting(
            actor,
            "refund_policy",
            {"max_age_hours": max_age_hours, "max_traffic_bytes": max_traffic_bytes},
            reason,
        )

    def refund_reviews(self, page=1, page_size=25):
        from .refund_reviews import RefundReviews

        return RefundReviews(self.commerce).list(page, page_size)

    def resolve_refund(
        self, actor, review_id, action, reason, ends_at=None, expected_subscription_version=None
    ):
        from .refund_reviews import RefundReviews

        return RefundReviews(self.commerce).resolve(
            actor, review_id, action, reason, ends_at, expected_subscription_version
        )

    def items(self, section):
        return self.list_items(section, page_size=200)["items"]

    def _product_row(self, product, periods):
        return product_dto(product, periods) | {
            "internal_squad_uuid": product.internal_squad_uuid,
            "mapping_type": "internal_squad",
            "recurring_plan_mapping": (product.presentation or {}).get("recurring_plan_mapping", {}),
            "presentation_overrides": product.presentation or {},
        }

    def _row(self, x, *, periods=None):
        if isinstance(x, Account):
            return account_dto(x) | {
                "bonus_balance": str(x.bonus_balance),
                **credit_dto(x),
                "created_at": iso(x.created_at),
            }
        if isinstance(x, Product):
            if periods is None:
                with self.db.transaction() as session:
                    periods = self.commerce._setting(session, "periods", [])
            return self._product_row(x, periods)
        if isinstance(x, Subscription):
            with self.db.transaction() as session:
                restriction = session.get(Setting, "restriction:" + x.id)
                suspended = bool(restriction and restriction.value.get("suspended"))
                dispute = session.get(Setting, "dispute:" + x.id)
                job = session.scalar(
                    select(Job)
                    .where(
                        Job.kind == "provision",
                        Job.payload["subscription_id"].as_string() == x.id,
                        Job.payload["version"].as_integer() == x.version,
                    )
                    .order_by(Job.id.desc())
                    .limit(1)
                )
                provision = (
                    {"job_id": job.id, "state": job.state, "last_error": job.last_error} if job else None
                )
            return {
                "id": x.id,
                "account_id": str(x.account_id),
                "status": x.status,
                "name": x.name,
                "expires_at": iso(x.expires_at),
                "product_id": x.product_id,
                "devices_limit": x.devices_limit,
                "version": x.version,
                "environment": x.environment,
                "panel_mapping_present": bool(x.panel_uuid),
                "desired_panel_status": "DISABLED" if suspended else "ACTIVE",
                "restriction": {"suspended": suspended},
                "dispute": {
                    "enabled": bool(dispute and dispute.value.get("enabled")),
                    "reason": dispute.value.get("reason") if dispute else None,
                },
                "provisioning": provision,
            }
        if isinstance(x, Order):
            return self.commerce._order_dto(x) | {
                "account_id": str(x.account_id),
                "created_at": iso(x.created_at),
                "source": x.source,
            }
        if isinstance(x, Payment):
            return {
                "id": x.id,
                "provider": x.provider,
                "provider_id": x.provider_id,
                "order_id": x.order_id,
                "account_id": str(x.account_id),
                "status": x.status,
                "amount": str(x.amount),
                "currency": x.currency,
                "fee": str(x.fee) if x.fee is not None else None,
                "fee_currency": x.fee_currency,
                "net_usdt": str(x.net_usdt) if x.net_usdt is not None else None,
                "paid_at": iso(x.paid_at),
                "environment": x.environment,
            }
        if isinstance(x, Block):
            scope = {
                "wrong_code": "account_email",
                "ip_login_failures": "ip",
                "mfa_failures": "account",
                "route_scan": "ip",
            }.get(x.rule, "context")
            subject = x.context
            if scope == "account_email":
                with self.db.transaction() as session:
                    challenge = session.scalar(
                        select(Challenge)
                        .where(Challenge.context == x.context)
                        .order_by(Challenge.expires_at.desc())
                        .limit(1)
                    )
                    subject = challenge.email if challenge else "Не найдено"
            return {
                "id": x.id,
                "rule": x.rule,
                "scope": scope,
                "subject": subject,
                "count": x.count,
                "threshold": x.threshold,
                "started_at": iso(x.started_at),
                "ends_at": iso(x.ends_at),
                "active": x.revoked_at is None and x.ends_at > now(),
                "source": "application",
                "reason": x.rule,
            }
        if isinstance(x, Agreement):
            return {
                "id": x.id,
                "subscription_id": x.subscription_id,
                "account_id": str(x.account_id),
                "state": x.state,
                "amount": str(x.consent_amount),
                "currency": x.currency,
                "next_charge_at": iso(x.next_charge_at),
                "provider_id": x.provider_id,
                "created_at": iso(x.created_at),
                "consent": {k: v for k, v in x.consent.items() if k not in {"pay_url", "request"}},
            }
        if isinstance(x, Setting):
            return (
                {"id": x.key, "content": x.value}
                if x.key.startswith("content:")
                else {"id": x.key, "value": x.value}
            )
        if isinstance(x, Job):
            return {
                "id": x.id,
                "kind": x.kind,
                "state": x.state,
                "attempts": x.attempts,
                "last_error": x.last_error,
                "available_at": iso(x.available_at),
                "recipient_count": len(x.payload.get("recipients", [])),
                "delivered_count": len(x.payload.get("recipients", [])) if x.state == "done" else 0,
            }
        raise missing()

    def list_items(self, section, search=None, page=1, page_size=25):
        if page < 1 or not 1 <= page_size <= 200:
            raise DomainError("page_invalid", "Проверьте номер и размер страницы.", 400)
        models = {
            "users": Account,
            "catalog": Product,
            "subscriptions": Subscription,
            "orders": Order,
            "payments": Payment,
            "security": Block,
            "recurring": Agreement,
            "content": Setting,
            "discounts": Setting,
            "broadcasts": Job,
            "operations": Job,
        }
        model = models.get(section)
        if not model:
            raise missing()
        query = select(model)
        if section == "subscriptions":
            from .subscription_archive import unarchived_subscription_clause

            query = query.where(unarchived_subscription_clause())
        if section == "content":
            query = query.where(Setting.key.like("content:%"))
        if section == "discounts":
            query = query.where(Setting.key.in_(["discounts", "periods"]))
        if section == "broadcasts":
            query = query.where(Job.kind == "broadcast")
        if section == "operations":
            query = query.where(Job.state != "done")
        if search:
            # Literal substring matching: wildcard characters cannot broaden a search.
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            fields = {
                Account: [Account.id, Account.email, Account.telegram_id, Account.telegram_username],
                Product: [Product.id, Product.name, Product.code],
                Subscription: [Subscription.id, Subscription.account_id, Subscription.name],
                Order: [Order.id, Order.account_id, Order.status],
                Payment: [Payment.id, Payment.provider_id, Payment.account_id, Payment.order_id],
                Block: [Block.id, Block.rule, Block.context],
                Agreement: [Agreement.id, Agreement.account_id, Agreement.provider_id],
                Setting: [Setting.key],
                Job: [Job.id, Job.kind, Job.state],
            }[model]
            conditions = [cast(f, String).ilike(pattern, escape="\\") for f in fields]
            if model is Account:
                conditions.append(func.lower(Account.telegram_username) == search.lstrip("@").lower())
                conditions.append(
                    Account.id.in_(
                        select(Payment.account_id).where(
                            or_(Payment.id == search, Payment.provider_id == search)
                        )
                    )
                )
            query = query.where(or_(*conditions))
        order = next(
            (
                getattr(model, name)
                for name in ("created_at", "paid_at", "started_at", "available_at", "id", "key")
                if hasattr(model, name)
            ),
            None,
        )
        with self.db.transaction() as s:
            total = s.scalar(select(func.count()).select_from(query.subquery()))
            rows = list(
                s.scalars(query.order_by(order.desc()).offset((page - 1) * page_size).limit(page_size))
            )
            periods = self.commerce._setting(s, "periods", []) if model is Product else None
            result = [self._row(x, periods=periods) for x in rows]
            if section == "operations" and page == 1 and not search:
                capability = s.get(Setting, "bot_capabilities")
                result.insert(
                    0,
                    {
                        "id": "bot_capabilities",
                        "kind": "capability",
                        "state": "observed" if capability else "not_tested",
                        **(
                            capability.value
                            if capability
                            else {
                                "rich_messages": False,
                                "custom_emoji": False,
                                "reason": "Возможности тестового бота ещё не проверены",
                            }
                        ),
                    },
                )
            return {"items": result, "total": total, "page": page, "page_size": page_size}

    def user_detail(self, account_id, actor_id=None, settings=None, page=1, page_size=25):
        if type(page) is not int or type(page_size) is not int or page < 1 or not 1 <= page_size <= 50:
            raise DomainError("page_invalid", "Проверьте номер страницы.", 400)
        from .moderation import DeviceModeration
        from .subscription_archive import unarchived_subscription_clause

        with self.db.transaction() as s:
            a = self.commerce._account(s, account_id, customer=False)
            subscriptions = select(Subscription.id).where(Subscription.account_id == a.id)
            totals = {}

            def paged(key, query, serializer):
                totals[key] = s.scalar(select(func.count()).select_from(query.order_by(None).subquery()))
                return [
                    serializer(x) for x in s.scalars(query.offset((page - 1) * page_size).limit(page_size))
                ]

            result = {
                **DeviceModeration(self.commerce).summary(s, a),
                "can_moderate": DeviceModeration.can_moderate(
                    s.get(Account, int(actor_id)) if actor_id else None, settings
                )
                if settings
                else False,
                "account": self._row(a),
                "identities": {
                    "email": a.email,
                    "email_verified": a.email_verified,
                    "telegram_id": str(a.telegram_id) if a.telegram_id else None,
                    "telegram_username": a.telegram_username,
                    "aliases": paged(
                        "aliases",
                        select(Alias.old_id).where(Alias.canonical_id == a.id).order_by(Alias.old_id),
                        str,
                    ),
                },
                "history_limit": page_size,
            }
            sections = [
                (
                    "subscriptions",
                    select(Subscription)
                    .where(Subscription.account_id == a.id, unarchived_subscription_clause())
                    .order_by(Subscription.id),
                    self._row,
                ),
                (
                    "orders",
                    select(Order)
                    .where(Order.account_id == a.id, successful_order_history_clause())
                    .order_by(Order.created_at.desc(), Order.id),
                    self._row,
                ),
                (
                    "payments",
                    select(Payment)
                    .where(Payment.account_id == a.id, successful_payment_history_clause())
                    .order_by(Payment.paid_at.desc(), Payment.id),
                    self._row,
                ),
                (
                    "ledger",
                    select(Ledger)
                    .where(Ledger.account_id == a.id)
                    .order_by(Ledger.created_at.desc(), Ledger.id),
                    lambda x: {
                        "id": x.id,
                        "operation": x.operation,
                        "amount": str(x.amount),
                        "created_at": iso(x.created_at),
                    },
                ),
                (
                    "devices",
                    select(Device)
                    .where(Device.subscription_id.in_(subscriptions.where(unarchived_subscription_clause())))
                    .order_by(Device.id),
                    lambda x: {
                        "id": x.id,
                        "subscription_id": x.subscription_id,
                        "name": x.name,
                        "revoked": x.revoked,
                        "last_seen_at": iso(x.last_seen_at),
                    },
                ),
                (
                    "sessions",
                    select(Session)
                    .where(Session.account_id == a.id)
                    .order_by(Session.expires_at.desc(), Session.id),
                    lambda x: {
                        "expires_at": iso(x.expires_at),
                        "confirmed_at": iso(x.confirmed_at),
                        "mfa_verified": x.mfa_at is not None,
                    },
                ),
                (
                    "blocks",
                    select(Block)
                    .where(Block.context == "account:" + str(a.id))
                    .order_by(Block.started_at.desc(), Block.id),
                    self._row,
                ),
                (
                    "audit",
                    select(Audit)
                    .where(or_(Audit.target == str(a.id), Audit.target.in_(subscriptions)))
                    .order_by(Audit.created_at.desc(), Audit.id),
                    lambda x: {
                        "id": x.id,
                        "action": x.action,
                        "target": x.target,
                        "reason": x.reason,
                        "created_at": iso(x.created_at),
                    },
                ),
                (
                    "agreements",
                    select(Agreement)
                    .where(Agreement.account_id == a.id)
                    .order_by(Agreement.created_at.desc(), Agreement.id),
                    lambda x: {
                        "id": x.id,
                        "subscription_id": x.subscription_id,
                        "state": x.state,
                        "amount": str(x.consent_amount),
                        "currency": x.currency,
                        "next_charge_at": iso(x.next_charge_at),
                    },
                ),
            ]
            for key, query, serializer in sections:
                result[key] = paged(key, query, serializer)
            result["pagination"] = {"page": page, "page_size": page_size, "totals": totals}
            return result

    def user_invitees(self, account_id, page=1, page_size=10):
        if type(page) is not int or type(page_size) is not int or page < 1 or not 1 <= page_size <= 50:
            raise DomainError("page_invalid", "Проверьте номер страницы.", 400)
        with self.db.transaction() as s:
            account = self.commerce._account(s, account_id, customer=False)
            query = select(Account).where(Account.referrer_id == account.id)
            total = s.scalar(select(func.count()).select_from(query.subquery()))
            return {
                "items": [
                    account_dto(a) | {"created_at": iso(a.created_at)}
                    for a in s.scalars(
                        query.order_by(Account.id).offset((page - 1) * page_size).limit(page_size)
                    )
                ],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    def payment_detail(self, pid):
        with self.db.transaction() as s:
            p = s.scalar(
                select(Payment)
                .where(or_(Payment.id == pid, Payment.provider_id == pid))
                .order_by(Payment.paid_at.desc())
                .limit(1)
            )
            if not p:
                raise missing()
            o = s.get(Order, p.order_id)
            q = s.get(Quote, o.quote_id)
            dispute = s.get(Setting, "dispute:" + o.subscription_id) if o.subscription_id else None
            return {
                "payment": self._row(p),
                "order": self._row(o),
                "quote": q.snapshot,
                "dispute": {
                    key: dispute.value.get(key) if dispute else (False if key == "enabled" else None)
                    for key in ("enabled", "reason", "payment_id")
                },
                "refund_review_context": self._refund_review_context(s, p, o),
                "entitlement_decisions": [
                    {
                        "id": event.id,
                        "created_at": iso(event.created_at),
                        "reason": event.reason,
                        **{
                            key: event.details.get(key)
                            for key in (
                                "action",
                                "kind",
                                "full_refund",
                                "remaining_expires_at",
                                "automatic_recalculation",
                                "usage_source",
                            )
                        },
                    }
                    for event in s.scalars(
                        select(Audit)
                        .where(Audit.action == "refund_entitlement_decision", Audit.target == p.id)
                        .order_by(Audit.created_at.desc())
                    )
                ],
                "refunds": [
                    {
                        "id": x.id,
                        "operation_id": x.operation_id,
                        "amount": str(x.amount),
                        "occurred_at": iso(x.occurred_at),
                    }
                    for x in s.scalars(select(Refund).where(Refund.payment_id == p.id))
                ],
                "jobs": [self._row(x) for x in s.scalars(select(Job).where(Job.key == "provision:" + o.id))],
            }

    def _refund_review_context(self, session, payment, order):
        refunded = session.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0)).where(Refund.payment_id == payment.id)
        )
        moment, segments = now(), []
        if order.subscription_id:
            for segment in session.scalars(
                select(Segment)
                .where(
                    Segment.subscription_id == order.subscription_id,
                    Segment.kind != "refunded",
                    Segment.ends_at > moment,
                )
                .order_by(Segment.starts_at)
            ):
                source_order = session.get(Order, segment.order_id) if segment.order_id else None
                quote = session.get(Quote, source_order.quote_id) if source_order else None
                seconds = max(0, int((segment.ends_at - max(moment, segment.starts_at)).total_seconds()))
                snapshot = quote.snapshot if quote else {}
                days = snapshot.get("basis", {}).get("period_days")
                known = (
                    type(days) is int
                    and days > 0
                    and segment.paid_value >= 0
                    and paid_entitlement_matches(segment, snapshot)
                )
                value = (
                    (segment.paid_value * Decimal(seconds) / Decimal(days * 86400)).quantize(
                        Decimal("0.01"), rounding=ROUND_DOWN
                    )
                    if known
                    else None
                )
                segments.append(
                    {
                        "segment_id": segment.id,
                        "order_id": segment.order_id,
                        "starts_at": iso(segment.starts_at),
                        "ends_at": iso(segment.ends_at),
                        "remaining_seconds": seconds,
                        "remaining_service_value": str(value) if known else None,
                        "currency": snapshot.get("currency"),
                        "valuation_source": "immutable_paid_quote" if known else None,
                        "unavailable_reason": None if known else "paid_basis_missing_or_changed",
                    }
                )
        return {
            "external_refundable_amount": str((payment.amount - refunded).quantize(Decimal("0.01"))),
            "external_currency": payment.currency,
            "provider_fee": str(payment.fee) if payment.fee is not None else None,
            "fee_currency": payment.fee_currency,
            "segments": segments,
            "automatic_compensation": False,
            "calculated_at": iso(moment),
        }

    def resolve_dispute(self, actor, payment_id, reason):
        if len(reason.strip()) < 3:
            raise DomainError("reason_required", "Укажите причину решения.", 400)
        with self.db.transaction() as s:
            payment = s.get(Payment, payment_id)
            order = s.get(Order, payment.order_id) if payment else None
            flag = (
                s.scalar(
                    select(Setting).where(Setting.key == "dispute:" + order.subscription_id).with_for_update()
                )
                if order and order.subscription_id
                else None
            )
            if not flag or flag.value.get("payment_id") != payment_id:
                raise missing()
            before = flag.value
            flag.value = before | {"enabled": False, "resolved_by": str(actor), "resolved_at": iso(now())}
            self.audit(s, actor, "dispute_resolution", payment_id, reason, before, flag.value)
            return {
                "payment_id": payment_id,
                "subscription_id": order.subscription_id,
                "dispute_state": "resolved",
                "rights_changed": False,
            }

    def content_versions(self, key):
        with self.db.transaction() as s:
            return [
                {
                    "id": x.id,
                    "version": x.details["after"]["version"],
                    "nodes": x.details["after"]["nodes"],
                    "reason": x.reason,
                    "created_at": iso(x.created_at),
                    "actor_id": str(x.actor_id) if x.actor_id else None,
                }
                for x in s.scalars(
                    select(Audit)
                    .where(Audit.action == "content_update", Audit.target == key)
                    .order_by(Audit.created_at.desc())
                    .limit(200)
                )
            ]

    def discount_preview(self, values):
        from sunrise_core.pricing import calculate

        with self.db.transaction() as s:
            product = s.get(Product, values["product_id"])
            if not product:
                raise missing()
            period = next(
                (p for p in self.commerce._setting(s, "periods", []) if p["days"] == values["period_days"]),
                None,
            )
            if not period:
                raise DomainError("period_invalid", "Выберите доступный срок.", 400)
            policy = self.commerce._setting(s, "discounts", {})
            cap_value = values.get("cap") if values.get("cap") is not None else policy.get("cap")
            bonus_cap_value = (
                values.get("bonus_cap") if values.get("bonus_cap") is not None else policy.get("bonus_cap")
            )
            if cap_value is None or bonus_cap_value is None:
                raise DomainError(
                    "pricing_policy_unavailable", "Условия оплаты требуют настройки владельцем.", 503
                )
            base = product.price * Decimal(str(period["months"]))
            configured_percent = values.get("period_discount_percent")
            period_percent = Decimal(
                str(period["discount_percent"] if configured_percent is None else configured_percent)
            )
            secondary = Decimal(str(values.get("secondary_percent", "0")))
            cap = Decimal(str(cap_value))
            bonus_cap = Decimal(str(bonus_cap_value))
            try:
                price = calculate(
                    base,
                    period_percent,
                    [secondary],
                    cap,
                    bonus_cap,
                    Decimal(values.get("bonus_amount", "0")),
                )
            except ValueError:
                raise DomainError("discount_invalid", "Проверьте суммы и проценты.", 400) from None
            return {
                "base": str(price.base),
                "period_discount_percent": str(period_percent),
                "secondary_percent": str(secondary),
                "cap": str(cap),
                "discounted": str(price.discounted),
                "bonus": str(price.bonus),
                "external": str(price.external),
                "currency": product.currency,
                "fee": {"source": "unknown", "amount": None},
                "breakdown": [
                    {"label": "Стоимость периода", "amount": str(price.base)},
                    {
                        "label": "Последовательные скидки с ограничением",
                        "amount": str(price.base - price.discounted),
                    },
                    {"label": "Бонусная часть", "amount": str(price.bonus)},
                    {"label": "Внешняя оплата без неизвестной комиссии", "amount": str(price.external)},
                ],
            }

    def catalog_impact(self, pid, values):
        from uuid import UUID

        from .catalog import PRESENTATION_FIELDS, validate_presentation

        with self.db.transaction() as s:
            product = s.get(Product, pid)
            if not product:
                raise missing()
            periods = self.commerce._setting(s, "periods", [])
            before = self._row(product, periods=periods)
            # A detached value object recomputes the same effective presentation without
            # mutating the ORM product or materializing inherited fields in storage.
            projected = SimpleNamespace(
                **{column.key: getattr(product, column.key) for column in Product.__table__.columns}
            )
            presentation = {key: value for key, value in values.items() if key in PRESENTATION_FIELDS}
            validate_presentation(presentation)
            projected.presentation = (product.presentation or {}) | presentation
            for key, value in values.items():
                if key not in PRESENTATION_FIELDS and (value is not None or key == "extra_device_price"):
                    setattr(
                        projected,
                        key,
                        Decimal(value)
                        if key in {"price", "extra_device_price"} and value is not None
                        else value,
                    )
            for key in ("price", "extra_device_price"):
                value = getattr(projected, key)
                if value is not None and (not Decimal(value).is_finite() or Decimal(value) <= 0):
                    raise DomainError("price_invalid", "Цена должна быть положительной.", 400)
            after = self._product_row(projected, periods)
            if values.get("internal_squad_uuid"):
                try:
                    UUID(values["internal_squad_uuid"])
                except ValueError:
                    raise DomainError("mapping_invalid", "Неверный UUID внутренней группы.", 400) from None
            count = s.scalar(
                select(func.count()).select_from(Subscription).where(Subscription.product_id == pid)
            )
            changed = before.get("internal_squad_uuid") != after.get("internal_squad_uuid")
            return {
                "product_id": pid,
                "before": before,
                "after": after,
                "affected_subscriptions": count,
                "existing_paid_rights_preserved": True,
                "mapping_changed": changed,
                "requires_external_mapping_verification": changed,
                "mapping_type": "internal_squad",
            }

    def estimate_broadcast(self, nodes, recipients, allowlist):
        from sunrise_core.content import validate_content

        try:
            validate_content(nodes)
        except ValueError as exc:
            raise DomainError("content_invalid", str(exc), 400) from None
        selected = set(recipients)
        allowed = selected & set(allowlist)
        return {
            "recipient_count": len(selected),
            "allowed_count": len(allowed),
            "denied_count": len(selected - allowed),
            "allowed": bool(selected) and selected == allowed and self.commerce.local_test,
        }

    def broadcast_detail(self, jid):
        with self.db.transaction() as s:
            job = s.get(Job, jid)
            if not job or job.kind != "broadcast":
                raise missing()
            return self._row(job) | {
                "environment": job.payload.get("environment"),
                "delivery_mode": "local_file" if self.commerce.local_test else "provider",
                "content": job.payload.get("nodes", []),
            }

    def maintenance(self, actor, enabled, reason):
        with self.db.transaction() as s:
            s.execute(select(func.pg_advisory_xact_lock(617045501)))
            row = s.scalar(select(Setting).where(Setting.key == "maintenance").with_for_update())
            old = row.value if row else {"enabled": False}
            value = {"enabled": enabled, "changed_at": iso(now())}
            if row:
                row.value = value
            else:
                s.add(Setting(key="maintenance", value=value))
            self.audit(s, actor, "maintenance", "maintenance", reason, old, value)
            return value

    def acknowledge_provider_operation(self, actor, operation_id, token, reason):
        from .operational_monitor import (
            provider_acknowledgeable_clause,
            provider_review_binding,
            provider_review_token,
        )
        from .staff_permissions import StaffPermissions

        with self.db.transaction() as session:
            StaffPermissions(self.accounts).require_in(session, actor, "operations_manage", bot=True)
            order = session.scalar(
                select(Order)
                .join(ProviderOperation, ProviderOperation.order_id == Order.id)
                .where(ProviderOperation.id == operation_id)
                .with_for_update(of=Order)
            )
            if not order:
                raise missing()
            operation = session.get(ProviderOperation, operation_id, with_for_update=True)
            eligible = session.scalar(
                select(ProviderOperation.id)
                .join(Order, Order.id == ProviderOperation.order_id)
                .where(ProviderOperation.id == operation_id, provider_acknowledgeable_clause())
            )
            if not eligible or token != provider_review_token(operation, order):
                raise DomainError(
                    "operation_review_stale",
                    "Операция изменилась. Обновите очередь и проверьте её заново.",
                    409,
                )
            binding = provider_review_binding(operation, order)
            marker = session.get(Setting, "op-review:" + operation_id)
            result = {"operation_id": operation_id, "acknowledged": True, "payment_state": operation.state}
            if marker and marker.value.get("version") == 1 and marker.value.get("binding") == binding:
                return result
            value = {
                "version": 1,
                "binding": binding,
                "actor_id": str(actor),
                "reviewed_at": now().isoformat(),
            }
            previous = marker.value if marker else None
            if marker:
                marker.value = value
            else:
                session.add(Setting(key="op-review:" + operation_id, value=value))
            self.audit(session, actor, "provider_operation_reviewed", operation_id, reason, previous, value)
            return result

    def health(self):
        from .operational_monitor import OperationalMonitor

        with self.db.transaction() as s:
            s.execute(text("SELECT 1"))
            queues = [
                {"kind": k, "state": state, "count": count}
                for k, state, count in s.execute(
                    select(Job.kind, Job.state, func.count()).group_by(Job.kind, Job.state)
                )
            ]
            oldest = s.scalar(select(func.min(Job.available_at)).where(Job.state == "pending"))
            backup = s.get(Setting, "verified_backup")
            maintenance = s.get(Setting, "maintenance")
            backup_status = (
                backup.value
                if backup
                else {"state": "not_verified", "message": "Восстановление резервной копии ещё не проверено"}
            )
            issues = OperationalMonitor(self.db, self.commerce.public_settings).snapshot_in(s)
            from .retention import policy_in

            retention_policy = policy_in(s).model_dump()
        try:
            redis_ok = bool(self.guard.redis.ping())
        except RedisError:
            redis_ok = False
        return {
            "database": {"state": "ok"},
            "redis": {"state": "ok" if redis_ok else "unavailable"},
            "queue": {"items": queues, "oldest_pending_at": iso(oldest)},
            "backup": backup_status,
            "maintenance": maintenance.value if maintenance else {"enabled": False},
            "checked_at": iso(now()),
            "issues": issues,
            "subscription_retention": retention_policy,
        }

    def audit(self, s, actor, action, target, reason, before, after):
        if self.accounts is not None:
            from .staff_permissions import StaffPermissions

            capability = {
                "bot_promo_save": "promos_write",
                "bot_promo_delete": "promos_write",
                "broadcast_create": "broadcasts_write",
                "broadcast_stop": "broadcasts_write",
                "bot_broadcast_draft": "broadcasts_write",
                "bot_broadcast_start": "broadcasts_write",
                "bot_broadcast_stop": "broadcasts_write",
                "utm_create": "campaigns_write",
                "message_create": "messages_write",
                "catalog_create": "catalog_write",
                "catalog_update": "catalog_write",
                "bonus_adjustment": "wallet_write",
                "credit_adjustment": "wallet_write",
                "security_unblock": "security_manage",
                "sessions_revoke": "security_manage",
                "compensation": "subscriptions_manage",
                "subscription_operation": "subscriptions_manage",
                "retry_provisioning": "operations_manage",
                "job_retry": "operations_manage",
                "provider_operation_reviewed": "operations_manage",
            }.get(action, "settings_write")
            StaffPermissions(self.accounts).require_in(s, actor, capability, bot=True)
            # Website routes enforce fresh TOTP before reaching this shared audit layer.
            # Bot callers are authorized by Telegram identity and capabilities.
        s.add(
            Audit(
                actor_id=actor,
                action=action,
                target=target,
                reason=reason,
                details={"before": before, "after": after},
            )
        )

    def toggle(self, actor, enabled, reason):
        with self.db.transaction() as s:
            row = s.scalar(select(Setting).where(Setting.key == "recurring").with_for_update())
            old = row.value if row else {"enabled": False}
            if not row:
                row = Setting(key="recurring", value={})
                s.add(row)
            row.value = {"enabled": enabled}
            self.audit(s, actor, "recurring_enrollment", "recurring", reason, old, row.value)
        return {"enabled": enabled}

    def catalog_update(self, actor, pid, values, reason):
        from .catalog import PRESENTATION_FIELDS, validate_presentation

        presentation = {k: v for k, v in values.items() if k in PRESENTATION_FIELDS}
        validate_presentation(presentation)
        if (
            set(values)
            - PRESENTATION_FIELDS
            - {
                "name",
                "description",
                "tier_level",
                "currency",
                "price",
                "included_devices",
                "extra_device_price",
                "available",
                "internal_squad_uuid",
                "content",
            }
        ):
            raise DomainError("catalog_invalid", "Неизвестное поле тарифа.", 400)
        with self.db.transaction() as s:
            p = s.scalar(select(Product).where(Product.id == pid).with_for_update())
            if not p:
                raise missing()
            periods = self.commerce._setting(s, "periods", [])
            before = self._row(p, periods=periods)
            values = dict(values)
            if values.get("description") is not None or values.get("content") is not None:
                current_content = p.content if p.content is not None else {}
                incoming_content = values.get("content")
                if not isinstance(current_content, dict) or (
                    incoming_content is not None and not isinstance(incoming_content, dict)
                ):
                    raise DomainError("content_invalid", "Проверьте структуру контента.", 400)
                if incoming_content is not None:
                    values["content"] = current_content | incoming_content
                if values.get("description") is not None:
                    description = values["description"]
                    if not isinstance(description, str) or len(description) > 4000:
                        raise DomainError("catalog_invalid", "Описание: до 4000 символов.", 400)
                    if description != p.description and (
                        "description" not in (incoming_content or {})
                        or incoming_content["description"] == current_content.get("description")
                    ):
                        from sunrise_core.telegram_content import telegram_html_to_content

                        values["content"] = (current_content | (incoming_content or {})) | {
                            "description": telegram_html_to_content(description)
                        }
            p.presentation = (p.presentation or {}) | presentation
            for key, value in values.items():
                if key in PRESENTATION_FIELDS:
                    continue
                if value is None:
                    if key == "extra_device_price":
                        p.extra_device_price = None
                    continue
                if key in {"price", "extra_device_price"}:
                    value = Decimal(value)
                    if value <= 0:
                        raise DomainError("price_invalid", "Цена должна быть положительной.", 400)
                if key == "internal_squad_uuid":
                    from uuid import UUID

                    try:
                        value = str(UUID(value))
                    except ValueError:
                        raise DomainError(
                            "mapping_invalid", "Неверный UUID внутренней группы.", 400
                        ) from None
                if key == "content":
                    from sunrise_core.content import validate_content

                    try:
                        if set(value) - {"name", "description"}:
                            raise ValueError("Неизвестное поле контента")
                        for nodes in value.values():
                            validate_content(nodes)
                    except ValueError as exc:
                        raise DomainError("content_invalid", str(exc), 400) from None
                setattr(p, key, value)
            p.version += 1
            after = self._row(p, periods=periods)
            self.audit(s, actor, "catalog_update", pid, reason, before, after)
            return after

    def catalog_create(self, actor, values, reason):
        import re
        from uuid import UUID

        from .catalog import PRESENTATION_FIELDS, validate_presentation

        presentation = {k: v for k, v in values.items() if k in PRESENTATION_FIELDS}
        validate_presentation(presentation)
        fields = {k: v for k, v in values.items() if k not in PRESENTATION_FIELDS}
        if set(fields) - {
            "code",
            "name",
            "description",
            "tier_level",
            "currency",
            "price",
            "included_devices",
            "extra_device_price",
            "available",
            "internal_squad_uuid",
            "content",
        }:
            raise DomainError("catalog_invalid", "Неизвестное поле тарифа.", 400)
        try:
            if (
                not re.fullmatch(r"[a-z0-9_]{1,64}", fields["code"])
                or not 1 <= len(fields["name"]) <= 200
                or type(fields["tier_level"]) is not int
                or not 1 <= fields["tier_level"] <= 1000
                or type(fields["included_devices"]) is not int
                or not 1 <= fields["included_devices"] <= 100
                or not re.fullmatch(r"[A-Z]{3}", fields["currency"])
            ):
                raise ValueError()
            for key in ("price", "extra_device_price"):
                if fields.get(key) is not None:
                    value = Decimal(fields[key])
                    if not value.is_finite() or value <= 0:
                        raise ValueError()
                    fields[key] = value
            if fields.get("internal_squad_uuid"):
                fields["internal_squad_uuid"] = str(UUID(fields["internal_squad_uuid"]))
            if fields.get("content"):
                from sunrise_core.content import validate_content

                if set(fields["content"]) - {"name", "description"}:
                    raise ValueError()
                for nodes in fields["content"].values():
                    validate_content(nodes)
        except (ValueError, TypeError, KeyError):
            raise DomainError(
                "catalog_invalid", "Проверьте код, название, ранг, цены и контент.", 400
            ) from None
        with self.db.transaction() as s:
            s.execute(select(func.pg_advisory_xact_lock(617045504)))
            if s.scalar(select(Product.id).where(Product.code == fields["code"])):
                raise DomainError("catalog_code_conflict", "Такой код тарифа уже существует.", 409)
            product = Product(**fields, presentation=presentation)
            s.add(product)
            s.flush()
            result = self._row(product, periods=self.commerce._setting(s, "periods", []))
            self.audit(s, actor, "catalog_create", product.id, reason, {}, result)
            return result

    def unblock(self, actor, bid, reason):
        with self.db.transaction() as s:
            b = s.scalar(select(Block).where(Block.id == bid).with_for_update())
            if not b:
                raise missing()
            self.guard.unblock(b.context)
            b.revoked_at = now()
            self.audit(s, actor, "security_unblock", bid, reason, {"active": True}, {"active": False})
        return {"status": "revoked"}

    def retry_provisioning(self, actor, oid, reason):
        with self.db.transaction() as s:
            job = s.scalar(select(Job).where(Job.key == "provision:" + oid).with_for_update())
            if not job:
                raise missing()
            sub = s.get(Subscription, job.payload.get("subscription_id"))
            if sub:
                from .retention import assert_mutation_allowed_in

                assert_mutation_allowed_in(s, sub)
            old = job.state
            job.state = "pending"
            job.available_at = now()
            self.audit(s, actor, "retry_provisioning", oid, reason, {"state": old}, {"state": "pending"})
        return {"status": "pending"}

    def adjust_bonus(self, actor, account_id, amount, operation_id, reason):
        amount = Decimal(amount)
        if not amount.is_finite() or abs(amount) > Decimal(1000000):
            raise DomainError("amount_invalid", "Некорректная сумма.", 400)
        with self.db.transaction() as s:
            a = self.commerce._account(s, account_id, True)
            key = "admin:" + str(a.id) + ":" + operation_id
            prior = self._prior_wallet_operations(s, Ledger, a.id, "admin:", operation_id)
            if prior:
                if len(prior) != 1 or prior[0].amount != amount:
                    raise DomainError("key_conflict", "Ключ уже использован с другой суммой.")
                return {"bonus_balance": str(a.bonus_balance)}
            if a.bonus_balance + amount < a.bonus_reserved:
                raise DomainError("bonus_reserved", "Часть бонусов уже зарезервирована заказом.")
            old = a.bonus_balance
            a.bonus_balance += amount
            s.add(Ledger(account_id=a.id, operation=key, amount=amount))
            self.audit(
                s,
                actor,
                "bonus_adjustment",
                str(a.id),
                reason,
                {"bonus_balance": str(old)},
                {"bonus_balance": str(a.bonus_balance)},
            )
            return {"bonus_balance": str(a.bonus_balance)}

    def _prior_wallet_operations(self, session, model, account_id, prefix, operation_id):
        identities = {
            account_id,
            *session.scalars(select(Alias.old_id).where(Alias.canonical_id == account_id)),
        }
        keys = [prefix + str(identity) + ":" + operation_id for identity in identities]
        return list(
            session.scalars(select(model).where(model.account_id == account_id, model.operation.in_(keys)))
        )

    def revoke_sessions(self, actor, account_id, reason):
        from sqlalchemy import delete

        with self.db.transaction() as s:
            a = self.commerce._account(s, account_id, True)
            count = s.execute(delete(Session).where(Session.account_id == a.id)).rowcount
            self.audit(s, actor, "sessions_revoke", str(a.id), reason, {}, {"count": count})
        return {"revoked": count}

    def adjust_credit(
        self,
        actor,
        account_id,
        amount,
        currency,
        provenance,
        operation_id,
        reason,
        confirm_existing_currency=False,
    ):
        import re

        amount = Decimal(amount)
        if (
            not amount.is_finite()
            or abs(amount) > Decimal(1000000)
            or amount != amount.quantize(Decimal("0.01"))
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or provenance not in {"owner_adjustment", "refund_compensation", "verified_legacy"}
            or not 1 <= len(operation_id) <= 100
            or len(reason.strip()) < 3
        ):
            raise DomainError(
                "credit_adjustment_invalid", "Проверьте сумму, валюту, источник и причину.", 400
            )
        with self.db.transaction() as s:
            account = self.commerce._account(s, account_id, True)
            key = "admin-credit:" + str(account.id) + ":" + operation_id
            prior = self._prior_wallet_operations(s, CreditLedger, account.id, "admin-credit:", operation_id)
            if prior:
                if len(prior) != 1 or (prior[0].amount, prior[0].currency, prior[0].provenance) != (
                    amount,
                    currency,
                    provenance,
                ):
                    raise DomainError("key_conflict", "Ключ использован с другими параметрами.", 409)
                return credit_dto(account)
            if account.credit_currency is None and account.credit_balance != 0:
                if not (confirm_existing_currency and provenance == "verified_legacy" and amount == 0):
                    raise DomainError(
                        "credit_currency_unverified", "Сначала отдельно подтвердите валюту остатка.", 409
                    )
            elif (
                account.credit_currency
                and account.credit_currency != currency
                and account.credit_balance != 0
            ):
                raise DomainError("credit_currency_mismatch", "Конвертация остатка не разрешена.", 409)
            if account.credit_reserved and account.credit_balance + amount < account.credit_reserved:
                raise DomainError("credit_reserved", "Денежный остаток зарезервирован заказом.", 409)
            before = credit_dto(account)
            account.credit_balance += amount
            account.credit_currency = currency
            account.credit_provenance = provenance
            s.add(
                CreditLedger(
                    account_id=account.id,
                    original_account_id=int(account_id),
                    operation=key,
                    amount=amount,
                    currency=currency,
                    provenance=provenance,
                )
            )
            after = credit_dto(account)
            self.audit(s, actor, "credit_adjustment", str(account.id), reason, before, after)
            return after

    def compensate(self, actor, sid, days, reason):
        from datetime import timedelta

        with self.db.transaction() as s:
            hint = s.get(Subscription, sid)
            if not hint:
                raise missing()
            owner = hint.account_id
            locked = self.commerce._lock_accounts_in(s, [owner, getattr(actor, "id", actor)])
            sub = s.scalar(
                select(Subscription)
                .where(Subscription.id == sid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not sub or sub.account_id != locked[owner]:
                raise missing()
            from .retention import assert_mutation_allowed_in

            assert_mutation_allowed_in(s, sub)
            self.commerce._assert_new_operations(s)
            if sub.environment != "local_test" or sub.expires_at is None or not sub.product_id:
                raise DomainError("rights_review", "Права требуют отдельной проверки.")
            prior_segment = s.scalar(
                select(Segment)
                .where(
                    Segment.subscription_id == sid,
                    Segment.product_id == sub.product_id,
                    Segment.kind != "refunded",
                )
                .order_by(Segment.ends_at.desc())
                .limit(1)
            )
            if not prior_segment or "panel_mappings" not in prior_segment.price_basis:
                raise DomainError("rights_review", "Назначение услуги требует отдельной проверки.")
            inherited_mapping = {
                key: prior_segment.price_basis[key]
                for key in ("panel_mappings", "panel_configuration")
                if key in prior_segment.price_basis
            }
            old = iso(sub.expires_at)
            prior_status = panel_entitlements.local_status(s, sub)
            start = max(now(), sub.expires_at)
            sub.expires_at = start + timedelta(days=days)
            s.add(
                Segment(
                    subscription_id=sid,
                    product_id=sub.product_id,
                    starts_at=start,
                    ends_at=sub.expires_at,
                    devices=sub.devices_limit,
                    paid_value=Decimal(0),
                    price_basis=inherited_mapping,
                    kind="compensation",
                )
            )
            sub.version += 1
            sub.status = "provisioning_pending"
            s.add(
                Job(
                    key="compensation:" + uid(),
                    kind="provision",
                    account_id=sub.account_id,
                    payload=self.commerce._provision_payload(
                        s, sub, prior_status=prior_status, origin="owner"
                    ),
                )
            )
            self.audit(
                s,
                actor,
                "subscription_compensation",
                sid,
                reason,
                {"expires_at": old},
                {"expires_at": iso(sub.expires_at), "days": days},
            )
        return {"expires_at": iso(sub.expires_at)}

    def save_content(self, actor, key, nodes, reason):
        import re

        from sunrise_core.content import validate_content

        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", key):
            raise DomainError("content_key_invalid", "Неверный ключ контента.", 400)
        try:
            validate_content(nodes)
        except ValueError as exc:
            raise DomainError("content_invalid", str(exc), 400) from None
        with self.db.transaction() as s:
            row = s.scalar(select(Setting).where(Setting.key == "content:" + key).with_for_update())
            old = row.value if row else {"nodes": [], "version": 0}
            value = {"nodes": nodes, "version": old["version"] + 1}
            if row:
                row.value = value
            else:
                s.add(Setting(key="content:" + key, value=value))
            self.audit(s, actor, "content_update", key, reason, old, value)
            return value

    def save_retention(self, actor, enabled, days_after_expiry, reason):
        from pydantic import ValidationError

        from .retention import RetentionPolicy

        try:
            value = RetentionPolicy.model_validate(
                {"enabled": enabled, "days_after_expiry": days_after_expiry}
            ).model_dump()
        except ValidationError:
            raise DomainError(
                "retention_policy_invalid",
                "Удаление разрешено только через семь дней после окончания срока.",
                400,
            ) from None
        return self._save_setting(actor, "subscription_retention", value, reason)

    def save_discounts(self, actor, values, reason):
        values = {k: v for k, v in values.items() if v is not None}
        milestones = values.get("loyalty_milestones", [])
        if len({x["elapsed_periods"] for x in milestones}) != len(milestones) or any(
            not Decimal(0) <= Decimal(x["percent"]) <= Decimal(100) for x in milestones
        ):
            raise DomainError("discount_invalid", "Проверьте ступени лояльности.", 400)
        ordered = sorted(milestones, key=lambda x: x["elapsed_periods"])
        if any(
            Decimal(a["percent"]) > Decimal(b["percent"]) for a, b in zip(ordered, ordered[1:], strict=False)
        ):
            raise DomainError(
                "discount_invalid", "Скидка лояльности не должна уменьшаться на следующей ступени.", 400
            )
        for key in ("renewal_campaign", "near_expiry_renewal", "referral_welcome"):
            if key in values and not Decimal(0) <= Decimal(values[key]["percent"]) <= Decimal(100):
                raise DomainError("discount_invalid", "Процент кампании вне диапазона.", 400)
        for key in ("cap", "bonus_cap", "referral_percent"):
            if key in values and not Decimal(0) <= Decimal(values[key]) <= Decimal(100):
                raise DomainError("discount_invalid", "Процент должен быть от 0 до 100.", 400)
        if "promos" in values:
            from .promos import validate_promo

            values["promos"] = [validate_promo(promo) for promo in values["promos"]]
            if len({promo["code"] for promo in values["promos"]}) != len(values["promos"]):
                raise DomainError("promo_duplicate", "Коды промокодов не должны повторяться.", 400)
        with self.db.transaction() as s:
            row = s.scalar(select(Setting).where(Setting.key == "discounts").with_for_update())
            for promo in values.get("promos", []):
                archived = s.get(Setting, "promo_archive")
                if archived and promo["code"] in archived.value:
                    raise DomainError("promo_archived", "Удалённый код нельзя использовать повторно.", 409)
                if promo["reward_type"] == "free_days":
                    product = s.get(Product, promo["product_id"])
                    if not product or not product.available:
                        raise DomainError(
                            "promo_product_unavailable", "Выберите доступный тариф подарка.", 400
                        )
            old = row.value if row else {}
            value = old | values
            if row:
                row.value = value
            else:
                s.add(Setting(key="discounts", value=value))
            self.audit(s, actor, "discounts_update", "discounts", reason, old, value)
        return value

    def save_periods(self, actor, periods, reason):
        if len({p["days"] for p in periods}) != len(periods):
            raise DomainError("period_duplicate", "Сроки не должны повторяться.", 400)
        if any(not Decimal(0) <= Decimal(p["discount_percent"]) <= Decimal(100) for p in periods):
            raise DomainError("discount_invalid", "Процент должен быть от 0 до 100.", 400)
        with self.db.transaction() as s:
            row = s.scalar(select(Setting).where(Setting.key == "periods").with_for_update())
            old = row.value if row else []
            if row:
                row.value = periods
            else:
                s.add(Setting(key="periods", value=periods))
            self.audit(s, actor, "periods_update", "periods", reason, old, periods)
        return {"periods": periods}

    def broadcast(self, actor, nodes, recipients, allowlist, reason):
        from sunrise_core.content import validate_content

        try:
            validate_content(nodes)
        except ValueError as exc:
            raise DomainError("content_invalid", str(exc), 400) from None
        recipients = sorted(set(recipients))
        if not self.commerce.local_test or not recipients or not set(recipients) <= set(allowlist):
            raise DomainError("recipient_denied", "Получатели не входят в список разрешённого теста.", 403)
        with self.db.transaction() as s:
            job = Job(
                key="broadcast:" + uid(),
                kind="broadcast",
                account_id=actor,
                payload={"nodes": nodes, "recipients": recipients, "environment": "local_test"},
            )
            s.add(job)
            s.flush()
            self.audit(s, actor, "broadcast_create", job.id, reason, {}, {"recipient_count": len(recipients)})
            return {
                "id": job.id,
                "state": job.state,
                "recipient_count": len(recipients),
                "environment": "local_test",
            }

    def stop_broadcast(self, actor, jid, reason):
        with self.db.transaction() as s:
            job = s.scalar(select(Job).where(Job.id == jid, Job.kind == "broadcast").with_for_update())
            if not job:
                raise missing()
            old = job.state
            if job.state == "pending":
                job.state = "stopped"
            self.audit(s, actor, "broadcast_stop", jid, reason, {"state": old}, {"state": job.state})
            return {"state": job.state}

    def save_registry(self, actor, registry, reason):
        import re

        from sunrise_core.content import validate_content

        if len(registry) > 100:
            raise DomainError("content_invalid", "Слишком много значков.", 400)
        try:
            for key, node in registry.items():
                if not re.fullmatch("[a-z_]{1,40}", key) or node.get("type") != "emoji":
                    raise ValueError("Неверный ключ значка")
                validate_content([node])
        except ValueError as exc:
            raise DomainError("content_invalid", str(exc), 400) from None
        with self.db.transaction() as s:
            row = s.scalar(select(Setting).where(Setting.key == "emoji_registry").with_for_update())
            old = row.value if row else {}
            if row:
                row.value = registry
            else:
                s.add(Setting(key="emoji_registry", value=registry))
            self.audit(s, actor, "emoji_registry", "emoji_registry", reason, old, registry)
        return {"registry": registry}

    def cancel_recurring(self, actor, agreement_id, reason):
        with self.db.transaction() as s:
            agreement = s.get(Agreement, agreement_id)
            if not agreement:
                raise missing()
            if agreement.state == "stopped":
                return {"id": agreement.id, "state": "stopped"}
            owner, sid = agreement.account_id, agreement.subscription_id
            self.audit(
                s,
                actor,
                "agreement_cancel",
                agreement_id,
                reason,
                {"state": agreement.state},
                {"requested": True},
            )
        return self.commerce.cancel_agreement(owner, sid)

    def retry_job(self, actor, jid, reason):
        with self.db.transaction() as s:
            job = s.scalar(select(Job).where(Job.id == jid).with_for_update())
            if not job or job.kind not in {"provision", "notification"}:
                raise missing()
            old = job.state
            job.state = "pending"
            job.available_at = now()
            self.audit(s, actor, "job_retry", jid, reason, {"state": old}, {"state": "pending"})
        return {"status": "pending"}

    def login_events(self, search=None, page=1, page_size=30):
        if page < 1 or not 1 <= page_size <= 100:
            raise DomainError("pagination_invalid", "Проверьте номер страницы.", 400)
        with self.db.transaction() as s:
            query = select(Audit).where(Audit.action == "login_event")
            if search:
                term = search.strip()[:200].replace("%", "\\%").replace("_", "\\_")
                query = query.where(
                    or_(
                        Audit.details["email"].as_string().ilike("%" + term + "%", escape="\\"),
                        Audit.details["ip"].as_string() == search.strip(),
                    )
                )
            total = s.scalar(select(func.count()).select_from(query.subquery()))
            events = s.scalars(
                query.order_by(Audit.created_at.desc(), Audit.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
            return {
                "items": [
                    {
                        "id": x.id,
                        "created_at": iso(x.created_at),
                        "reason": x.reason,
                        **{
                            k: x.details.get(k)
                            for k in ("email", "challenge_id", "account_id", "result", "ip", "ip_provenance")
                        },
                    }
                    for x in events
                ],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    def subscription_operation(self, actor, sid, operation, reason):
        if operation not in {"suspend", "resume", "synchronize"} or len(reason.strip()) < 3:
            raise DomainError("operation_invalid", "Укажите операцию и причину.", 400)
        with self.db.transaction() as s:
            hint = s.get(Subscription, sid)
            if not hint:
                raise missing()
            owner = hint.account_id
            locked = self.commerce._lock_accounts_in(s, [owner, getattr(actor, "id", actor)])
            sub = s.scalar(
                select(Subscription)
                .where(Subscription.id == sid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if not sub or sub.account_id != locked[owner]:
                raise missing()
            from .retention import assert_mutation_allowed_in

            assert_mutation_allowed_in(s, sub)
            self.commerce._assert_new_operations(s)
            if not sub.expires_at or not sub.product_id:
                raise DomainError("rights_unverified", "Сначала подтвердите права подписки.", 409)
            restriction = s.get(Setting, "restriction:" + sid)
            prior_status = panel_entitlements.local_status(s, sub)
            before = bool(restriction and restriction.value.get("suspended"))
            suspended = operation == "suspend" if operation != "synchronize" else before
            if restriction:
                restriction.value = {"suspended": suspended}
            else:
                s.add(Setting(key="restriction:" + sid, value={"suspended": suspended}))
            sub.version += 1
            sub.status = "suspension_pending" if suspended else "provisioning_pending"
            if operation == "resume" and prior_status == "suspended":
                prior_status = "active"
            payload = self.commerce._provision_payload(s, sub, prior_status=prior_status, origin="owner")
            if suspended:
                payload["panel_status"] = "DISABLED"
                payload["desired_subscription_status"] = "suspended"
            job = Job(key="admin-sync:" + uid(), kind="provision", account_id=sub.account_id, payload=payload)
            s.add(job)
            s.flush()
            self.audit(
                s,
                actor,
                "subscription_" + operation,
                sid,
                reason,
                {"suspended": before},
                {"suspended": suspended, "job_id": job.id},
            )
            return {"subscription_id": sid, "operation": operation, "state": "pending", "job_id": job.id}

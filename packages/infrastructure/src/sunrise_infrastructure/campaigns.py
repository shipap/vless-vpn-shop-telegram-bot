"""Реальные веб-переходы с ограниченным сроком доказательства; отдельно от рефералов."""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from sqlalchemy import func, select, text
from sunrise_core.errors import DomainError, missing

from .db import Alias, Audit, CampaignVisit, Job, Payment, Setting, now, uid
from .staff_permissions import StaffPermissions


class Campaigns:
    def __init__(self, commerce, settings):
        self.commerce, self.db, self.settings = commerce, commerce.db, settings
        self.staff = StaffPermissions(commerce.accounts or SimpleNamespace(db=self.db, settings=settings))

    def _row(self, setting):
        identifier = setting.key.removeprefix("utm:")
        landing = getattr(self.settings, "landing_url", None)
        return {
            "id": identifier,
            **setting.value,
            "website_url": landing.rstrip("/") + "/?" + urlencode({"utm_id": identifier})
            if landing
            else None,
        }

    def list(self, actor):
        with self.db.transaction() as session:
            self.staff.require_in(session, actor, "campaigns_read", bot=True)
            return {
                "items": [
                    self._row(row)
                    for row in session.scalars(
                        select(Setting).where(Setting.key.like("utm:%")).order_by(Setting.key).limit(100)
                    )
                ]
            }

    def create(self, actor, values, reason):
        if (
            not all(
                isinstance(values.get(key), str) and 1 <= len(values[key].strip()) <= 200
                for key in ("name", "source", "medium", "campaign")
            )
            or not isinstance(reason, str)
            or not 5 <= len(reason.strip()) <= 500
        ):
            raise DomainError(
                "campaign_invalid", "Заполните название, источник, канал, кампанию и причину.", 400
            )
        identifier = secrets.token_urlsafe(18)
        with self.db.transaction() as session:
            self.staff.require_in(session, actor, "campaigns_write", bot=True)
            bot = getattr(self.settings, "bot_url", None)
            row = Setting(
                key="utm:" + identifier,
                value={
                    **{k: values[k].strip() for k in ("name", "source", "medium", "campaign")},
                    "url": bot + "?start=utm_" + identifier if bot else None,
                    "created_at": now().isoformat(),
                },
            )
            session.add(row)
            session.add(
                Audit(
                    actor_id=int(actor),
                    action="utm_create",
                    target=identifier,
                    reason=reason.strip(),
                    details={},
                )
            )
            return self._row(row)

    def visit(self, campaign_id, visitor_id):
        if (
            not isinstance(campaign_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{24}", campaign_id)
            or not isinstance(visitor_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{16,128}", visitor_id)
        ):
            raise DomainError("campaign_invalid", "Некорректная рекламная ссылка.", 400)
        visitor = hmac.new(
            self.settings.secret_key.encode(), ("visitor:" + visitor_id).encode(), hashlib.sha256
        ).hexdigest()
        proof = hmac.new(
            self.settings.secret_key.encode(),
            ("campaign:" + campaign_id + ":" + visitor).encode(),
            hashlib.sha256,
        ).hexdigest()
        with self.db.transaction() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                {"key": "campaign:" + campaign_id},
            )
            if not session.get(Setting, "utm:" + campaign_id):
                raise missing()
            row = session.scalar(
                select(CampaignVisit)
                .where(CampaignVisit.campaign_id == campaign_id, CampaignVisit.visitor_hash == visitor)
                .with_for_update()
            )
            if not row:
                row = CampaignVisit(
                    campaign_id=campaign_id,
                    visitor_hash=visitor,
                    secret_hash=hashlib.sha256(proof.encode()).hexdigest(),
                )
                session.add(row)
            else:
                row.last_seen_at = now()
            session.flush()
            if row.account_id is not None:
                self._touch_in(session, row, now())
            key = "campaign_hits:" + campaign_id
            hits = session.get(Setting, key)
            if hits:
                hits.value = {"visits": hits.value.get("visits", 0) + 1}
            else:
                session.add(Setting(key=key, value={"visits": 1}))
            return {
                "campaign_id": campaign_id,
                "visit_token": proof,
                "max_age_seconds": 2592000,
                "expires_at": (now() + timedelta(days=30)).isoformat(),
            }

    def attach(self, account_id, visit_tokens):
        if (
            not isinstance(visit_tokens, list)
            or not 1 <= len(visit_tokens) <= 2
            or any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in visit_tokens)
        ):
            raise DomainError("campaign_proof_invalid", "Переход не подтверждён.", 400)
        with self.db.transaction() as session:
            self.commerce._account(session, account_id, True)
            for proof in set(visit_tokens):
                visit = session.scalar(
                    select(CampaignVisit)
                    .where(CampaignVisit.secret_hash == hashlib.sha256(proof.encode()).hexdigest())
                    .with_for_update()
                )
                if (
                    not visit
                    or visit.last_seen_at < now() - timedelta(days=30)
                    or visit.account_id not in {None, int(account_id)}
                ):
                    raise DomainError(
                        "campaign_proof_invalid", "Переход истёк или уже связан с другим аккаунтом.", 400
                    )
                if visit.account_id is None:
                    visit.account_id = int(account_id)
                    self._touch_in(session, visit, now())
            return {"status": "attached"}

    def _touch_in(self, session, visit, attached_at):
        session.add(
            Setting(
                key="campaign_touch:" + uid(),
                value={
                    "account_id": str(visit.account_id),
                    "campaign_id": visit.campaign_id,
                    "visit_id": visit.id,
                    "observed_at": visit.last_seen_at.isoformat(),
                    "attached_at": attached_at.isoformat(),
                },
            )
        )

    def record_paid_in(self, session, order, payment_id):
        """Неизменяемая атрибуция только внутри успешной общей покупки, без backfill."""
        key = "campaign_conversion:" + order.id
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": key})
        if session.get(Setting, key):
            return
        payment = session.get(Payment, payment_id)
        if (
            not payment
            or order.status != "paid"
            or payment.status != "paid"
            or payment.order_id != order.id
            or payment.account_id != order.account_id
            or payment.currency != order.currency
            or payment.amount != order.amount
            or not payment.paid_at
        ):
            raise DomainError("campaign_payment_invalid", "Платёж не связан с оплаченной покупкой.", 409)
        account_ids = [str(order.account_id)]
        account_ids.extend(
            str(value)
            for value in session.scalars(select(Alias.old_id).where(Alias.canonical_id == order.account_id))
        )
        touches = []
        for row in session.scalars(
            select(Setting).where(
                Setting.key.like("campaign_touch:%"), Setting.value["account_id"].as_string().in_(account_ids)
            )
        ):
            value = row.value
            observed, attached = (
                datetime.fromisoformat(value["observed_at"]),
                datetime.fromisoformat(value["attached_at"]),
            )
            if (
                payment.paid_at - timedelta(days=30) <= observed <= payment.paid_at
                and attached <= payment.paid_at
            ):
                touches.append((observed, attached, row.key, value["campaign_id"]))
        touches.sort()
        session.add(
            Setting(
                key=key,
                value={
                    "order_id": order.id,
                    "payment_id": payment.id,
                    "account_id": str(order.account_id),
                    "paid_at": payment.paid_at.isoformat(),
                    "amount": str(payment.amount),
                    "currency": payment.currency,
                    "environment": payment.environment,
                    "first_campaign_id": touches[0][3] if touches else None,
                    "last_campaign_id": touches[-1][3] if touches else None,
                },
            )
        )

    def stats(self, actor, identifier):
        with self.db.transaction() as session:
            self.staff.require_in(session, actor, "campaigns_read", bot=True)
            if not session.get(Setting, "utm:" + identifier):
                raise missing()
            visits = list(
                session.scalars(select(CampaignVisit).where(CampaignVisit.campaign_id == identifier))
            )
            account_ids = {v.account_id for v in visits if v.account_id is not None}
            conversions = list(
                session.scalars(
                    select(Setting).where(
                        Setting.key.like("campaign_conversion:%"),
                        Setting.value["last_campaign_id"].as_string() == identifier,
                    )
                )
            )
            amounts = []
            for conversion in conversions:
                value = conversion.value
                payment = session.get(Payment, value["payment_id"])
                if (
                    payment
                    and payment.status in {"paid", "refunded", "partially_refunded", "chargeback"}
                    and payment.order_id == value["order_id"]
                    and payment.currency == value["currency"] == "RUB"
                    and str(payment.amount) == value["amount"]
                    and payment.environment == value["environment"]
                ):
                    amounts.append(Decimal(value["amount"]))
            hits = session.get(Setting, "campaign_hits:" + identifier)
            starts = session.scalar(
                select(func.count())
                .select_from(Job)
                .where(Job.kind == "utm_start_event", Job.payload["link_id"].as_string() == identifier)
            )
            return {
                "web_visits": hits.value.get("visits", 0) if hits else 0,
                "web_unique_visitors": len(visits),
                "telegram_starts": starts,
                "accounts": len(account_ids),
                "paid_orders": len(amounts),
                "turnover": str(sum(amounts, Decimal("0.00"))),
                "currency": "RUB",
                "attribution": "last_web_touch_at_payment",
            }

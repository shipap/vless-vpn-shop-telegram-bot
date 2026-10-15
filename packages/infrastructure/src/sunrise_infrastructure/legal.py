"""Версии документов и доказательства явного согласия общего аккаунта."""

import hashlib
import json
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sunrise_core.errors import DomainError

from .db import Account, Audit, LegalConsent, Setting

LEGAL_LOCK = 617045502


def unconfigured_documents():
    return [{"document_type": kind, "version": "", "url": ""} for kind in ("offer", "privacy")]


def validate_documents(documents):
    if not isinstance(documents, list) or len(documents) != 2:
        raise DomainError("legal_documents_invalid", "Укажите обе версии юридических документов.", 400)
    result = []
    for document in documents:
        if not isinstance(document, dict) or set(document) != {"document_type", "version", "url"}:
            raise DomainError("legal_documents_invalid", "Проверьте реквизиты документов.", 400)
        kind, version, url = (document[k] for k in ("document_type", "version", "url"))
        if not all(isinstance(v, str) for v in (kind, version, url)):
            raise DomainError("legal_documents_invalid", "Проверьте реквизиты документов.", 400)
        if any(ord(char) < 32 or ord(char) == 127 for char in version + url):
            raise DomainError("legal_documents_invalid", "Проверьте реквизиты документов.", 400)
        parsed = urlsplit(url)
        if (
            not version.strip()
            or len(version) > 100
            or len(url) > 2048
            or parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise DomainError(
                "legal_documents_invalid", "Нужны явная версия и HTTPS-ссылка без учётных данных.", 400
            )
        result.append(document.copy())
    if {d["document_type"] for d in result} != {"offer", "privacy"}:
        raise DomainError("legal_documents_invalid", "Укажите оферту и политику конфиденциальности.", 400)
    return sorted(result, key=lambda d: d["document_type"])


class Legal:
    def __init__(self, commerce):
        self.commerce, self.db = commerce, commerce.db

    def current(self, session):
        row = session.get(Setting, "legal", populate_existing=True)
        documents = row.value.get("documents", []) if row and isinstance(row.value, dict) else []
        try:
            documents = validate_documents(documents)
            configured = True
        except (DomainError, ValueError):
            documents, configured = unconfigured_documents(), False
        revision = hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()[:24]
        return {"configured": configured, "documents": documents, "revision": revision}

    def public(self):
        with self.db.transaction() as s:
            return self.current(s)

    def configure(
        self, actor, documents, reason, *, only_if_missing=False, partial=False, expected_revision=None
    ):
        if not partial:
            documents = validate_documents(documents)
        elif (
            not isinstance(documents, list)
            or len(documents) != 1
            or not isinstance(documents[0], dict)
            or documents[0].get("document_type") not in {"offer", "privacy"}
        ):
            raise DomainError("legal_documents_invalid", "Выберите один юридический документ.", 400)
        if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 500:
            raise DomainError("reason_required", "Укажите причину изменения.", 400)
        with self.db.transaction() as s:
            s.execute(select(func.pg_advisory_xact_lock(LEGAL_LOCK)))
            before = self.current(s)
            if expected_revision is not None and expected_revision != before["revision"]:
                raise DomainError(
                    "legal_versions_stale",
                    "Документы обновились. Загрузите текущие версии перед сохранением.",
                    409,
                )
            if partial:
                kind = documents[0]["document_type"]
                documents = validate_documents(
                    [d for d in before["documents"] if d["document_type"] != kind] + documents
                )
            row = s.get(Setting, "legal")
            if row and only_if_missing:
                return before
            if row:
                row.value = {"documents": documents}
            else:
                s.add(Setting(key="legal", value={"documents": documents}))
            s.flush()
            after = self.current(s)
            s.add(
                Audit(
                    actor_id=actor,
                    action="legal_documents_update",
                    target="legal",
                    reason=reason,
                    details={"before": before, "after": after},
                )
            )
            return after

    def missing(self, page=1, page_size=25):
        if page < 1 or not 1 <= page_size <= 200:
            raise DomainError("page_invalid", "Проверьте страницу.", 400)
        with self.db.transaction() as s:
            current = self.current(s)
            checks = []
            for d in current["documents"]:
                checks.append(
                    select(LegalConsent.id)
                    .where(
                        LegalConsent.account_id == Account.id,
                        LegalConsent.document_type == d["document_type"],
                        LegalConsent.document_version == d["version"],
                        LegalConsent.document_url == d["url"],
                    )
                    .exists()
                )
            query = select(Account).where(~(checks[0] & checks[1]))
            total = s.scalar(select(func.count()).select_from(query.subquery()))
            accounts = s.scalars(query.order_by(Account.id).offset((page - 1) * page_size).limit(page_size))
            return {
                "items": [
                    {
                        "id": str(a.id),
                        "email": a.email,
                        "telegram_id": str(a.telegram_id) if a.telegram_id else None,
                        "missing": self.status_in(s, a)["missing"],
                        "legacy_evidence": a.legacy_consent,
                    }
                    for a in accounts
                ],
                "total": total,
                "page": page,
                "page_size": page_size,
            }

    def status_in(self, s, account):
        current = self.current(s)
        rows = list(
            s.scalars(
                select(LegalConsent)
                .where(LegalConsent.account_id == account.id)
                .order_by(LegalConsent.accepted_at, LegalConsent.id)
            )
        )
        accepted = {(r.document_type, r.document_version, r.document_url) for r in rows}
        missing = [
            d["document_type"]
            for d in current["documents"]
            if (d["document_type"], d["version"], d["url"]) not in accepted
        ]
        return current | {
            "accepted": current["configured"] and not missing,
            "missing": missing,
            "history": [
                {
                    "id": r.id,
                    "user_id": str(r.account_id),
                    "original_user_id": str(r.original_account_id),
                    "document_type": r.document_type,
                    "version": r.document_version,
                    "url": r.document_url,
                    "accepted_at": r.accepted_at.isoformat(),
                    "source": r.source,
                    "context": r.context,
                }
                for r in rows
            ],
            "legacy_evidence": account.legacy_consent,
        }

    def status(self, account_id):
        with self.db.transaction() as s:
            return self.status_in(s, self.commerce._account(s, account_id))

    def require_current(self, s, account):
        s.execute(select(func.pg_advisory_xact_lock(LEGAL_LOCK)))
        state = self.status_in(s, account)
        if not state["configured"]:
            raise DomainError(
                "legal_not_configured",
                "Документы сервиса ожидают подтверждения. Новые покупки временно недоступны.",
                503,
            )
        if not state["accepted"]:
            raise DomainError(
                "legal_consent_required",
                "Ознакомьтесь с текущими документами и подтвердите согласие.",
                409,
                next_action="legal",
            )

    def accept(self, account_id, documents, source, context):
        with self.db.transaction() as s:
            account = self.commerce._account(s, account_id, True)
            return self.accept_in(s, account, documents, source, context)

    def accept_in(self, s, account, documents, source, context):
        """Записывает согласие в транзакции вызывающего действия, включая вход."""
        if source not in {"website", "telegram"} or context not in {"registration", "checkout", "first_use"}:
            raise DomainError("legal_context_invalid", "Проверьте действие подтверждения.", 400)
        supplied = validate_documents(documents)
        s.execute(select(func.pg_advisory_xact_lock(LEGAL_LOCK)))
        state = self.status_in(s, account)
        if not state["configured"]:
            raise DomainError("legal_not_configured", "Версии документов ещё не подтверждены.", 503)
        if supplied != state["documents"]:
            raise DomainError(
                "legal_versions_stale",
                "Документы обновились. Прочитайте текущие версии.",
                409,
                next_action="legal",
            )
        for document in supplied:
            if document["document_type"] in state["missing"]:
                s.add(
                    LegalConsent(
                        account_id=account.id,
                        original_account_id=account.id,
                        document_type=document["document_type"],
                        document_version=document["version"],
                        document_url=document["url"],
                        source=source,
                        context=context,
                    )
                )
        s.flush()
        return self.status_in(s, account)

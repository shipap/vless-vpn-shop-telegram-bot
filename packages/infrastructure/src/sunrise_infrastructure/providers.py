"""Контракт RollyPay проверен по официальной документации 07.10.2026.

Адаптер не подключается при импорте. Внешние вызовы требуют явного разрешённого host.
Разовый платёж всегда sandbox. Создание реального соглашения дополнительно запрещено
без отдельного допуска согласованного плательщика.
"""

from decimal import Decimal
from urllib.parse import urlencode, urlparse, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from sunrise_core.errors import DomainError

from .instance_config import public_url


def sandbox_checkout_url(value):
    return checkout_url(value, {"pay.rollypay.io"})


def checkout_url(value, allowed_hosts):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        return None
    try:
        url = urlparse(value)
        if (
            url.scheme == "https"
            and url.hostname in allowed_hosts
            and not url.username
            and not url.password
            and url.port in {None, 443}
            and not url.fragment
        ):
            return value
    except ValueError:
        pass
    return None


class RollyPay:
    def __init__(
        self,
        base_url,
        api_key,
        terminal,
        allowed_hosts,
        transport=None,
        recurring_authorized=False,
        approved_payer_ids=None,
        environment="sandbox",
        runtime_policy=None,
    ):
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed_hosts
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Платёжный адрес не разрешён.")
        self.client = httpx.Client(
            base_url=base_url, transport=transport, timeout=10, follow_redirects=False, trust_env=False
        )
        self.api_key, self.terminal = api_key, terminal
        if environment not in {"sandbox", "live", "scoped_live"}:
            raise ValueError("provider_environment_denied")
        self.environment, self.runtime_policy = environment, runtime_policy
        if environment == "live":
            if (
                not runtime_policy
                or not runtime_policy.production
                or runtime_policy.scope.rollypay.origin != base_url
                or runtime_policy.scope.rollypay.terminal != terminal
            ):
                raise ValueError("production_bank_scope_denied")
            runtime_policy.require()
        if environment == "scoped_live":
            if (
                not runtime_policy
                or not runtime_policy.scoped_payments
                or runtime_policy.production
                or runtime_policy.scope.rollypay.origin != base_url
                or runtime_policy.scope.rollypay.terminal != terminal
            ):
                raise ValueError("scoped_test_bank_scope_denied")
            runtime_policy.require()
        self.recurring_authorized = recurring_authorized
        self.approved_payer_ids = set(approved_payer_ids or [])

    @property
    def real_payments(self):
        return self.environment in {"live", "scoped_live"}

    def _request(self, method, path, body=None, key=None, *, array=False, cancel_confirmation=False):
        if self.environment == "scoped_live":
            self.runtime_policy.require()
            if method == "POST" and path == "/api/v1/payments" and isinstance(body, dict):
                self.runtime_policy.authorize_invoice(
                    body["order_id"],
                    body["customer_id"],
                    body["amount"],
                    body["payment_currency"],
                    body.get("payment_method"),
                )
            elif method == "GET" and path.startswith("/api/v1/payments/"):
                self.runtime_policy.authorize_payment_read(path.removeprefix("/api/v1/payments/"))
            else:
                raise DomainError("scoped_test_stage_denied", "Операция недоступна в тестовом контуре.", 403)
        if cancel_confirmation and (
            self.environment != "live"
            or method != "GET"
            or not path.startswith("/api/v1/subscriptions/")
            or "/" in path.removeprefix("/api/v1/subscriptions/")
        ):
            raise ValueError("provider_cancel_scope_denied")
        if self.environment == "live":
            capability = "payment_reconcile"
            if path.startswith("/api/v1/subscription"):
                capability = (
                    "existing_recurring_cancel"
                    if cancel_confirmation or method == "POST" and path.endswith("/stop")
                    else "existing_recurring_status"
                )
                if method == "POST" and not path.endswith("/stop"):
                    raise DomainError(
                        "live_enrollment_disabled", "Новые подключения автопродления отключены.", 403
                    )
            elif method == "POST":
                capability = "invoice_create"
            self.runtime_policy.require(capability)
        headers = {"X-API-Key": self.api_key, "X-Nonce": str(uuid4())}
        if key:
            headers["Idempotency-Key"] = key
        try:
            response = self.client.request(method, path, json=body, headers=headers)
            response.raise_for_status()
            result = response.json(parse_float=Decimal) if self.real_payments else response.json()
            if self.real_payments:
                self.runtime_policy.require()
            if (array and (not isinstance(result, list) or not all(isinstance(x, dict) for x in result))) or (
                not array and not isinstance(result, dict)
            ):
                raise ValueError("provider_response_unverified")
            return result
        except (httpx.HTTPError, ValueError):
            # No automatic money-changing retry. Caller retains persisted key and reconciles.
            raise DomainError(
                "provider_outcome_unknown",
                "Результат операции уточняется. Не оплачивайте повторно.",
                503,
                False,
                "check_status",
            ) from None

    def create_payment(
        self,
        order_id,
        amount,
        currency,
        account_id,
        payment_method=None,
        *,
        description=None,
        success_redirect_url=None,
        fail_redirect_url=None,
    ):
        redirects = {}
        for key, value in (
            ("success_redirect_url", success_redirect_url),
            ("fail_redirect_url", fail_redirect_url),
        ):
            if value is None:
                continue
            try:
                if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
                    raise ValueError("payment_return_invalid")
                parsed = urlsplit(value)
                if not public_url(urlunsplit(parsed._replace(fragment=""))):
                    raise ValueError("payment_return_invalid")
            except ValueError:
                raise DomainError(
                    "payment_return_unavailable", "Возврат после оплаты недоступен.", 409
                ) from None
            redirects[key] = value
        if (
            payment_method is not None
            and payment_method not in {"crypto", "xrocket", "sbp"}
            or self.real_payments
            and payment_method is None
        ):
            raise DomainError("payment_method_unavailable", "Выберите доступный способ оплаты.", 409)
        if currency not in {"RUB", "EUR"}:
            raise DomainError(
                "payment_currency_unsupported", "Валюта не поддерживается проверенным контрактом.", 409
            )
        if currency == "EUR" and Decimal(amount) < 1:
            raise DomainError("payment_amount_too_small", "Минимальная внешняя оплата — 1 EUR.", 409)
        return self._request(
            "POST",
            "/api/v1/payments",
            {
                "amount": amount,
                "payment_currency": currency,
                "order_id": order_id,
                "terminal_id": self.terminal,
                "customer_id": account_id,
                "test": not self.real_payments,
            }
            | ({"description": description} if description is not None else {})
            | redirects
            | (
                {"payment_method": payment_method}
                if payment_method
                else {"payment_method": "intl_card"}
                if currency == "EUR"
                else {}
            ),
        )

    def payment(self, payment_id):
        self._safe_id(payment_id)
        return self._request("GET", "/api/v1/payments/" + payment_id)

    def list_payments(self, order_id):
        if self.environment != "live":
            raise DomainError("payment_collection_denied", "Сверка терминала недоступна в этом окружении.", 403)
        self._safe_id(order_id)
        result = self._request(
            "GET", "/api/v1/payments?" + urlencode(
                {"terminal_id": self.terminal, "order_id": order_id, "limit": 100}
            )
        )
        items, total = result.get("items"), result.get("total")
        # Неполная выборка не доказывает уникальность найденного счёта.
        if (
            not isinstance(items, list)
            or not all(isinstance(item, dict) for item in items)
            or type(total) is not int
            or not 0 <= total <= 100
            or total != len(items)
            or any(item.get("order_id") != order_id for item in items)
        ):
            raise DomainError("payment_collection_review", "Список счетов требует отдельной сверки.", 409)
        return result

    def plans(self):
        self._require_recurring_transport()
        return self._request("GET", "/api/v1/subscription-plans?terminal_id=" + self.terminal)

    def _require_recurring_transport(self):
        if self.environment == "scoped_live":
            self.runtime_policy.require("existing_recurring_status")
        if self.environment == "live":
            self.runtime_policy.require("existing_recurring_status")
            return
        if not self.recurring_authorized:
            raise DomainError(
                "recurring_test_not_authorized",
                "Тестовый транспорт автопродления требует отдельного согласования.",
                403,
            )

    def create_agreement(self, terms, idempotency_key):
        if self.real_payments:
            raise DomainError("live_enrollment_disabled", "Новые подключения автопродления отключены.", 403)
        if not self.recurring_authorized:
            raise DomainError(
                "recurring_test_not_authorized",
                "Банковский тест автопродления требует отдельного согласования.",
                403,
            )
        if terms.get("payer_id") not in self.approved_payer_ids:
            raise DomainError("payer_denied", "Плательщик не включён в согласованный банковский тест.", 403)
        if not 1 <= len(idempotency_key.encode()) <= 128:
            raise ValueError("Некорректный ключ операции")
        return self._request(
            "POST", "/api/v1/subscriptions", terms | {"terminal_id": self.terminal}, idempotency_key
        )

    def agreement(self, agreement_id):
        self._require_recurring_transport()
        self._safe_id(agreement_id)
        return self._request("GET", "/api/v1/subscriptions/" + agreement_id)

    def stop_agreement(self, agreement_id):
        if self.environment != "live":
            self._require_recurring_transport()
        self._safe_id(agreement_id)
        self._request("POST", "/api/v1/subscriptions/" + agreement_id + "/stop")
        if self.environment == "live":
            return self._request("GET", "/api/v1/subscriptions/" + agreement_id, cancel_confirmation=True)
        return self.agreement(agreement_id)

    def charges(self, agreement_id):
        self._require_recurring_transport()
        self._safe_id(agreement_id)
        return self._request("GET", "/api/v1/subscriptions/" + agreement_id + "/charges", array=True)

    def list_agreements(self, page=1):
        self._require_recurring_transport()
        if not isinstance(page, int) or page < 1:
            raise ValueError("Неверная страница")
        return self._request(
            "GET", "/api/v1/subscriptions?terminal_id=" + self.terminal + "&page=" + str(page)
        )

    @staticmethod
    def _safe_id(value):
        if not value or len(value) > 128 or any(not (c.isalnum() or c in "-_") for c in value):
            raise ValueError("Некорректный идентификатор провайдера")


def configured_payments(config, policy=None):
    from .settings import runtime_policy

    policy = policy or runtime_policy(config)
    live = bool(policy and (policy.production or policy.scoped_payments))
    if not (live or config.sandbox_payments_enabled):
        return None
    if not config.rollypay_api_key or not config.rollypay_signing_secret or not config.rollypay_terminal:
        if live:
            raise ValueError("production_bank_credentials_missing")
        return None
    return RollyPay(
        config.rollypay_base_url,
        config.rollypay_api_key,
        config.rollypay_terminal,
        set(config.payment_allowed_hosts),
        environment=policy.payment_environment if live else "sandbox",
        runtime_policy=policy,
        recurring_authorized=config.recurring_test_authorized,
        approved_payer_ids=config.recurring_test_payer_ids,
    )

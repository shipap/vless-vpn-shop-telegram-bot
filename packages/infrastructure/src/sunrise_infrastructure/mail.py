"""Общий SMTP/SMTPS транспорт: только явно разрешённый тестовый сервер и адресаты."""

import re
import smtplib
import ssl
from datetime import UTC, datetime
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import format_datetime

from sunrise_core.errors import DomainError

from .broadcast_media import MediaLimits, attach_media, checked_email_bytes
from .mail_templates import render_mail


def valid_mail_address(value):
    return isinstance(value, str) and bool(
        re.fullmatch(r"[A-Za-z0-9.!#$%&'+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value)
    )


def synthetic_mail_address(value):
    return valid_mail_address(value) and value.rsplit("@", 1)[1].lower().endswith(".test")


class SMTPTestMail:
    def __init__(
        self,
        *,
        host,
        port,
        mode,
        sender,
        username=None,
        password=None,
        allowed_hosts=(),
        allowed_recipients=(),
        authorized=False,
        factory=None,
        tunnel_port=None,
        local_test=False,
        instance=None,
        media_store=None,
    ):
        if (
            not authorized
            or host not in set(allowed_hosts)
            or any("*" in x for x in allowed_hosts)
            or not isinstance(host, str)
            or not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host)
            or (mode, port) not in {("starttls", 587), ("smtps", 465)}
            or not valid_mail_address(sender)
            or tunnel_port is not None
            and (
                not local_test
                or mode != "starttls"
                or not isinstance(tunnel_port, int)
                or isinstance(tunnel_port, bool)
                or not 1024 <= tunnel_port <= 65535
            )
            or factory is None
            and (not username or not password)
        ):
            raise ValueError("mail_configuration_denied")
        self.host, self.port, self.mode, self.sender = host, port, mode, sender
        self.username, self._password = username, password
        self.recipients = set(allowed_recipients)
        if not self.recipients or any(
            not valid_mail_address(x) or factory is None and synthetic_mail_address(x)
            for x in self.recipients
        ):
            raise ValueError("mail_allowlist_required")
        self.factory = factory
        self.tunnel_port = tunnel_port
        self.media_store = media_store
        from .instance_config import InstanceConfig

        self.instance = instance or InstanceConfig()

    def _recipient_allowed(self, recipient):
        if synthetic_mail_address(recipient) and recipient not in self.recipients:
            raise DomainError("mail_unavailable", "Отправка почты временно недоступна.", 503, True)
        return recipient in self.recipients

    def deliver(self, recipient, subject, body, delivery_id, *, actions=None, media=None, before_send=None):
        if not self._recipient_allowed(recipient) or "\r" in subject or "\n" in subject:
            raise ValueError("mail_recipient_denied")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", delivery_id):
            raise ValueError("mail_delivery_id_invalid")
        message = EmailMessage(policy=policy.SMTP)
        rendered = render_mail(subject, body, self.sender, instance=self.instance, actions=actions)
        message["From"] = Address(display_name=self.instance.sender_name, addr_spec=self.sender)
        message["To"], message["Subject"] = recipient, rendered.subject
        message["Message-ID"] = "<" + delivery_id + "@" + self.sender.split("@", 1)[1] + ">"
        message["Date"] = format_datetime(datetime.now(UTC))
        message["Content-Language"] = "ru"
        message.set_content(rendered.plain, charset="utf-8", cte="quoted-printable")
        message.add_alternative(rendered.html, subtype="html", charset="utf-8", cte="quoted-printable")
        if media is not None:
            if self.media_store is None:
                raise DomainError("media_unavailable", "Хранилище вложений недоступно.", 503)
            attach_media(message, self.media_store, media)
        wire_limit = self.media_store.limits.email_bytes if self.media_store else MediaLimits().email_bytes
        checked_email_bytes(message, limit=wire_limit)
        context = ssl.create_default_context()
        kwargs = {
            "host": "127.0.0.1" if self.tunnel_port is not None else self.host,
            "port": self.tunnel_port if self.tunnel_port is not None else self.port,
            "timeout": 10,
        }
        factory = self.factory or (smtplib.SMTP_SSL if self.mode == "smtps" else smtplib.SMTP)
        if self.mode == "smtps":
            kwargs["context"] = context
        send_started = accepted = False
        try:
            with factory(**kwargs) as smtp:
                smtp.ehlo()
                if self.mode == "starttls":
                    if self.tunnel_port is not None:
                        # smtplib использует _host для SNI и проверки имени сертификата.
                        smtp._host = self.host
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if self.username:
                    smtp.login(self.username, self._password)
                checked_email_bytes(
                    message,
                    limit=wire_limit,
                    smtp_size=getattr(smtp, "esmtp_features", {}).get("size"),
                )
                if before_send is not None:
                    # Права могли измениться во время TLS/login; отказ должен предшествовать DATA.
                    before_send()
                send_started = True
                refused = smtp.send_message(message, from_addr=self.sender, to_addrs=[recipient])
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                accepted = True
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
            if accepted:
                return
            raise DomainError("mail_rejected", "Почтовый сервер отклонил сообщение.", 503) from None
        except (smtplib.SMTPException, OSError):
            if accepted:
                # Положительный ответ на DATA уже получен: сбой QUIT не отменяет приём.
                return
            if send_started:
                raise DomainError(
                    "mail_delivery_unknown",
                    "Результат отправки письма неизвестен. Требуется проверка.",
                    503,
                    next_action="review",
                ) from None
            raise DomainError("mail_unavailable", "Тестовая почта временно недоступна.", 503, True) from None


class SMTPProductionMail(SMTPTestMail):
    """Live recipient authority is supplied by a persisted purpose-bound caller."""

    def __init__(
        self, *, runtime_policy, username=None, password=None, factory=None, instance=None, media_store=None
    ):
        if not runtime_policy or not runtime_policy.production:
            raise ValueError("production_admission_missing")
        runtime_policy.require()
        scope = runtime_policy.scope.smtp
        self.runtime_policy = runtime_policy
        super().__init__(
            host=scope.host,
            port=scope.port,
            mode=scope.mode,
            sender=scope.sender,
            username=username,
            password=password,
            allowed_hosts={scope.host},
            allowed_recipients={scope.sender},
            authorized=True,
            factory=factory,
            instance=instance,
            media_store=media_store,
        )

    def _recipient_allowed(self, recipient):
        return valid_mail_address(recipient) and (
            self.factory is not None or not synthetic_mail_address(recipient)
        )

    def deliver(
        self, recipient, subject, body, delivery_id, *, before_send=None, capability="notice_email", **kwargs
    ):
        if not callable(before_send) or capability not in {"auth_mail", "notice_email"}:
            raise DomainError("mail_authorization_missing", "Текущий получатель письма не подтверждён.", 503)
        self.runtime_policy.require(capability)

        def authorize():
            self.runtime_policy.require(capability)
            before_send()

        return super().deliver(recipient, subject, body, delivery_id, before_send=authorize, **kwargs)


class SMTPScopedAuthMail(SMTPProductionMail):
    """Реальные коды входа на согласованном стенде; отправка уведомлений запрещена."""

    def __init__(
        self, *, runtime_policy, username=None, password=None, factory=None, instance=None, media_store=None
    ):
        if not runtime_policy or not getattr(runtime_policy, "scoped_payments", False):
            raise ValueError("scoped_mail_admission_missing")
        runtime_policy.require("auth_mail")
        scope = runtime_policy.scope.smtp
        if scope is None:
            raise ValueError("scoped_mail_admission_missing")
        self.runtime_policy = runtime_policy
        SMTPTestMail.__init__(
            self,
            host=scope.host,
            port=scope.port,
            mode=scope.mode,
            sender=scope.sender,
            username=username,
            password=password,
            allowed_hosts={scope.host},
            allowed_recipients={scope.sender},
            authorized=True,
            factory=factory,
            instance=instance,
            media_store=media_store,
        )

    def deliver(self, recipient, subject, body, delivery_id, *, capability="notice_email", **kwargs):
        if capability != "auth_mail":
            raise DomainError("scoped_mail_delivery_denied", "Отправка уведомлений на стенде выключена.", 403)
        return super().deliver(recipient, subject, body, delivery_id, capability=capability, **kwargs)


def configured_mail(config, factory=None, *, media_store=None):
    from .settings import runtime_policy

    policy = runtime_policy(config)
    if policy and getattr(policy, "scoped_payments", False) and policy.scope.allow_email_signup:
        from .instance_config import configured_instance

        return SMTPScopedAuthMail(
            runtime_policy=policy,
            username=config.smtp_username,
            password=config.smtp_password,
            factory=factory,
            instance=configured_instance(config),
            media_store=media_store,
        )
    if policy and policy.production:
        from .instance_config import configured_instance

        return SMTPProductionMail(
            runtime_policy=policy,
            username=config.smtp_username,
            password=config.smtp_password,
            factory=factory,
            instance=configured_instance(config),
            media_store=media_store,
        )
    if not config.smtp_test_authorized:
        return None
    recipients = [email for email in config.email_allowlist if not synthetic_mail_address(email)]
    if not recipients:
        return None
    from .instance_config import configured_instance

    return SMTPTestMail(
        host=config.smtp_host,
        port=config.smtp_port,
        mode=config.smtp_mode,
        sender=config.smtp_sender,
        username=config.smtp_username,
        password=config.smtp_password,
        allowed_hosts=config.smtp_allowed_hosts,
        allowed_recipients=recipients,
        authorized=True,
        factory=factory,
        tunnel_port=getattr(config, "smtp_tunnel_port", None),
        local_test=config.local_test,
        instance=configured_instance(config),
        media_store=media_store,
    )


def render_mail_text(nodes):
    from sunrise_core.content import validate_content

    validate_content(nodes)

    def render(items):
        parts = []
        for node in items:
            kind = node["type"]
            if kind == "text":
                parts.append(node["text"])
            elif kind == "emoji":
                parts.append(node["fallback"])
            elif kind == "br":
                parts.append("\n")
            elif kind == "list":
                parts.append("\n" + "\n".join("• " + render(item) for item in node["items"]) + "\n")
            elif kind == "table":
                parts.append(
                    "\n" + "\n".join(" | ".join(render(cell) for cell in row) for row in node["rows"]) + "\n"
                )
            else:
                value = render(node.get("children", []))
                if kind == "link":
                    value += " (" + node["href"] + ")"
                elif kind == "details":
                    value = node["title"] + "\n" + value
                parts.append(value + ("\n" if kind in {"heading", "paragraph", "quote", "details"} else ""))
        return "".join(parts)

    return render(nodes).strip()

"""Русские письма экземпляра: безопасный HTML и самостоятельная текстовая версия."""

import re
from dataclasses import dataclass
from html import escape
from urllib.parse import urlsplit


@dataclass(frozen=True)
class RenderedMail:
    subject: str
    plain: str
    html: str


def render_mail(subject: str, body: str, sender: str, *, instance=None, actions=None) -> RenderedMail:
    from .instance_config import InstanceConfig

    instance = instance or InstanceConfig()
    if not isinstance(subject, str) or not isinstance(body, str):
        raise ValueError("mail_content_invalid")
    otp = re.fullmatch(r"Код входа: ([0-9]{6})\r?\nСрок действия: ([1-9][0-9]{0,2}) минут\.", body)
    otp = otp if subject.startswith("Код входа") else None
    link_otp = (
        re.fullmatch(
            r"Код подтверждения привязки Telegram: ([0-9]{6})\r?\nСрок действия: ([1-9][0-9]{0,2}) минут\.",
            body,
        )
        if subject == "Подтверждение привязки Telegram"
        else None
    )
    is_link = link_otp is not None
    otp = link_otp or otp
    if otp:
        code, minutes = otp.groups()
        subject = "Подтверждение привязки Telegram" if is_link else "Код входа " + instance.app_name
        title = "Подтвердите привязку Telegram" if is_link else "Подтвердите вход"
        intro = (
            "Введите этот код в окне привязки Telegram в личном кабинете."
            if is_link
            else "Введите этот код на странице входа в аккаунт."
        )
        code_label = "Код подтверждения" if is_link else "Код входа"
        content = (
            '<div style="margin:24px 0;padding:22px 12px;border:1px solid #ead9ca;'
            'border-radius:12px;background-color:#fff5e9;text-align:center;">'
            '<code aria-label="' + code_label + "\" style=\"font-family:Consolas,'Courier New',monospace;"
            'font-size:34px;font-weight:700;letter-spacing:5px;color:#26201a;">' + code + "</code></div>"
            '<p style="margin:0 0 22px;font-size:15px;line-height:1.6;color:#555a62;">'
            "Срок действия: " + minutes + " минут.</p>"
        )
        note = "Никому не сообщайте код, в том числе представителям сервиса. " + (
            "Если вы не запрашивали привязку Telegram, игнорируйте это письмо."
            if is_link
            else "Если вы не запрашивали вход, игнорируйте это письмо."
        )
        plain = body + "\n\n" + note
    else:
        title = (
            "Оплата получена"
            if body == "Оплата получена. Состояние подписки доступно в личном кабинете."
            else subject
        )
        intro = ""
        content = "".join(
            '<p style="margin:0 0 18px;font-size:16px;line-height:1.7;color:#343941;overflow-wrap:anywhere;">'
            + escape(paragraph).replace("\n", "<br>")
            + "</p>"
            for paragraph in body.replace("\r\n", "\n").split("\n\n")
        )
        note = ""
        plain = body

    for action in (actions or [])[:5]:
        if not isinstance(action, dict):
            continue
        url, label = action.get("url"), action.get("label")
        if not isinstance(url, str) or not isinstance(label, str) or not 1 <= len(label) <= 100:
            continue
        try:
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or any(ord(c) < 33 for c in url)
                or "\\" in url
                or len(url) > 2048
                or parsed.scheme == "http"
                and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            ):
                continue
        except ValueError:
            continue
        content += (
            '<p style="margin:22px 0;"><a style="display:inline-block;padding:13px 20px;'
            'border-radius:10px;background:#ffb547;color:#191b20;text-decoration:none;font-weight:700;" href="'
            + escape(url, quote=True)
            + '">'
            + escape(label)
            + "</a></p>"
        )
        plain += "\n\n" + label + ": " + url

    intro_html = (
        '<p style="margin:12px 0 0;font-size:16px;line-height:1.7;color:#555a62;">' + escape(intro) + "</p>"
        if intro
        else ""
    )
    note_html = (
        '<p style="margin:0;padding-top:20px;border-top:1px solid #ece6e0;'
        'font-size:14px;line-height:1.7;color:#62666d;">' + escape(note) + "</p>"
        if note
        else ""
    )
    html = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + escape(subject) + "</title></head>"
        '<body style="margin:0;padding:0;background-color:#f3f0ec;color:#262b32;'
        'font-family:Arial,Helvetica,sans-serif;-webkit-text-size-adjust:100%;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;background-color:#f3f0ec;">'
        '<tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:600px;border:1px solid #e4ddd5;border-radius:16px;background-color:#ffffff;">'
        '<tr><td style="padding:28px 24px;background-color:#191b20;border-radius:16px 16px 0 0;">'
        '<p style="margin:0;font-size:25px;font-weight:700;letter-spacing:-0.5px;color:#ffffff;">'
        + escape(instance.app_name)
        + "</p></td></tr>"
        '<tr><td style="padding:30px 24px;">'
        '<h1 style="margin:0 0 20px;font-size:26px;line-height:1.3;font-weight:700;color:#262b32;">'
        + escape(title)
        + "</h1>"
        + intro_html
        + content
        + note_html
        + "</td></tr>"
        '<tr><td style="padding:20px 24px;border-top:1px solid #ece6e0;">'
        '<p style="margin:0;font-size:12px;line-height:1.7;color:#676b72;">'
        "Письмо от " + escape(instance.app_name) + "<br>Отправитель: " + escape(sender) + "</p></td></tr>"
        "</table></td></tr></table></body></html>"
    )
    return RenderedMail(
        subject, plain + "\n\nОтправитель: " + instance.sender_name + " <" + sender + ">", html
    )

"""Приглашение нового аккаунта без изменения существующей атрибуции или денег."""

import re
from urllib.parse import urlencode, urlsplit, urlunsplit

from sqlalchemy import inspect, select

from .db import Account, Alias


def referral_id(code):
    if not isinstance(code, str) or not re.fullmatch(r"ref_[1-9][0-9]{0,18}", code):
        return None
    value = int(code[4:])
    return value if value < 2**63 else None


def assign_new_referrer(session, account, code, *, allow_legacy_alias=False):
    """Вызывается после add, до flush; существующий аккаунт никогда не меняется."""
    if not inspect(account).pending or account.referrer_id is not None:
        return
    inviter_id = referral_id(code)
    legacy_alias = False
    if (
        inviter_id is None
        and allow_legacy_alias
        and isinstance(code, str)
        and re.fullmatch(r"ref_-[1-9][0-9]{0,18}", code)
    ):
        legacy_id = int(code[4:])
        if -(2**63) <= legacy_id < 0:
            inviter_id, legacy_alias = legacy_id, True
    if inviter_id is None:
        return
    with session.no_autoflush:
        alias = session.get(Alias, inviter_id)
        # Отрицательный legacy ID допустим лишь через сохранённый alias, не raw Account.
        if legacy_alias and (alias is None or alias.canonical_id <= 0):
            return
        target_id = alias.canonical_id if alias else inviter_id
        inviter = session.scalar(
            select(Account).where(Account.id == target_id, Account.id > 0).with_for_update()
        )
        if inviter is None:
            # Объединение могло завершиться, пока запрос ждал блокировку строки.
            alias = session.get(Alias, inviter_id, populate_existing=True)
            if alias:
                inviter = session.scalar(
                    select(Account).where(Account.id == alias.canonical_id, Account.id > 0).with_for_update()
                )
        if inviter and inviter is not account and inviter.id != account.id:
            account.referrer_id = inviter.id


def website_referral_url(account_id, settings=None, *, local_test=False):
    configured = getattr(settings, "landing_url", None)
    try:
        parsed = urlsplit(configured or "")
        safe = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not any(char.isspace() or ord(char) < 32 for char in (configured or ""))
            and "\\" not in (configured or "")
        )
        if local_test and (not safe or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}):
            parsed = urlsplit("http://127.0.0.1:3100/")
            safe = True
        if not safe or parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return None
        if parsed.port == 0:
            return None
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path or "/",
                urlencode({"ref": "ref_" + str(account_id)}),
                "",
            )
        )
    except ValueError:
        return None
